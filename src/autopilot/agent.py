"""Bounded explore loop: plan -> execute -> judge -> follow up -> confirm (PRD §7.3).

The planner (an AI model or the rule-based heuristic) only proposes scenarios. Everything that
touches the target goes through the executor and policy; every verdict comes
from the deterministic oracles.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from .config import Config
from .executor import Executor
from .llm.base import LLMBudgetExhausted, LLMError, Planner, PlanningContext
from .llm.schemas import LLMHypothesis, to_scenario
from .models import Finding, FindingStatus, Scenario, ScenarioResult, Violation
from .oracles import check_all
from .policy import BudgetExhausted, Policy, PolicyViolation
from .spec import ApiSpec

ORACLE_TITLES = {
    "builtin.auth_differential": "Cross-user access",
    "builtin.auth_required": "Missing authentication",
    "builtin.server_error": "Server error",
    "spec.constraint": "Documented constraint not enforced",
    "builtin.schema_conformance": "Response doesn't match schema",
    "builtin.undocumented_status": "Undocumented status code",
    "builtin.error_leak": "Internal details exposed",
}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class RunStats:
    planner: str
    requests: int = 0
    scenarios: int = 0
    rounds: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str = "completed"


@dataclass
class RunResult:
    run_id: str
    target: str
    started_at: str
    finished_at: str
    stats: RunStats
    findings: list[Finding]
    results: list[ScenarioResult]
    notes: list[str] = field(default_factory=list)

    @property
    def confirmed(self) -> list[Finding]:
        return [f for f in self.findings if f.status == FindingStatus.CONFIRMED]


def _title(v: Violation) -> str:
    title = f"{ORACLE_TITLES.get(v.oracle, v.oracle)} in {v.operation_id}"
    # Constraint and schema findings on one operation differ only by their key.
    if v.oracle in {"spec.constraint", "builtin.schema_conformance"} and v.key:
        title += f" ({v.key})"
    return title


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Autopilot:
    def __init__(
        self,
        config: Config,
        spec: ApiSpec,
        planner: Planner,
        client: httpx.AsyncClient,
        on_event: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.spec = spec
        self.planner = planner
        self.policy = Policy(config, spec)
        self.executor = Executor(config, spec, self.policy, client)
        self.emit = on_event or (lambda _msg: None)
        self.ctx = PlanningContext(
            spec=spec,
            identities=config.identity_names,
            allow_writes=self.policy.allow_writes,
            allow_deletes=self.policy.allow_deletes,
        )
        self._next_id = 0
        self._signatures: set[str] = set()
        self.notes: list[str] = []

    def _scenarios(self, hypotheses: list[LLMHypothesis]) -> list[Scenario]:
        out = []
        for h in hypotheses:
            self._next_id += 1
            scenario = to_scenario(h, f"s{self._next_id}")
            if scenario is None:
                self.notes.append(f"Dropped malformed hypothesis: {h.title}")
                continue
            # Don't spend requests on a scenario the policy would block anyway.
            try:
                for step in scenario.steps:
                    self.policy.check_step(step)
            except PolicyViolation as exc:
                self.notes.append(f"Skipped '{h.title}': {exc}")
                continue
            # Exact repeats of an earlier scenario add nothing.
            signature = json.dumps([st.model_dump(mode="json") for st in scenario.steps], sort_keys=True)
            if signature in self._signatures:
                self.notes.append(f"Skipped '{h.title}': repeats an earlier scenario")
                continue
            self._signatures.add(signature)
            out.append(scenario)
        return out

    async def _execute(self, scenario: Scenario) -> ScenarioResult:
        exchanges, _ = await self.executor.run(scenario)
        result = ScenarioResult(
            scenario=scenario, exchanges=exchanges, violations=check_all(self.spec, exchanges)
        )
        flags = ", ".join(sorted({v.oracle for v in result.violations})) or "clean"
        statuses = " ".join(
            str(e.status) if e.status else "blocked" if e.blocked else "error" for e in exchanges
        )
        self.emit(f"  {scenario.title}  [{statuses}]  {flags}")
        return result

    async def run(self) -> RunResult:
        started = _now()
        stats = RunStats(planner=self.planner.name)
        results: list[ScenarioResult] = []

        try:
            kind = "rule-based planner (no AI)" if self.planner.name == "heuristic" else "AI planner"
            self.emit(f"Planning with the {kind}...")
            plan = await self.planner.plan(self.ctx)
            self.emit(f"Plan: {plan.reasoning} ({len(plan.hypotheses)} hypotheses)")
            queue = self._scenarios(plan.hypotheses)
            for round_no in range(self.config.budgets.max_iterations + 1):
                stats.rounds = round_no + 1
                self.emit(f"Round {round_no + 1}: running {len(queue)} scenario(s)")
                for scenario in queue:
                    results.append(await self._execute(scenario))
                if round_no == self.config.budgets.max_iterations:
                    stats.stop_reason = "follow-up round limit reached"
                    break
                follow = await self.planner.follow_up(self.ctx, results)
                queue = self._scenarios(follow.hypotheses)
                self.emit(f"Follow-up: {follow.reasoning}")
                if not queue:
                    stats.stop_reason = "planner had nothing further to test"
                    break
        except BudgetExhausted as exc:
            stats.stop_reason = str(exc)
        except LLMBudgetExhausted as exc:
            stats.stop_reason = str(exc)
        except LLMError as exc:
            stats.stop_reason = f"planner error: {exc}"
            self.emit(f"Planner error: {exc}")

        findings = await self._confirm(results)
        await self._investigate(findings)

        stats.requests = self.policy.requests_sent
        stats.scenarios = len(results)
        usage = self.planner.usage
        stats.llm_calls, stats.input_tokens = usage.calls, usage.input_tokens
        stats.output_tokens, stats.cost_usd = usage.output_tokens, round(usage.cost_usd, 4)
        findings.sort(key=lambda f: (f.status != FindingStatus.CONFIRMED, SEVERITY_ORDER[f.severity]))
        return RunResult(
            run_id=uuid.uuid4().hex[:8],
            target=self.config.target.base_url,
            started_at=started,
            finished_at=_now(),
            stats=stats,
            findings=findings,
            results=results,
            notes=self.notes,
        )

    async def _confirm(self, results: list[ScenarioResult]) -> list[Finding]:
        """Re-run each candidate from a clean start; N/N reproductions = confirmed."""
        # One candidate per fingerprint, cut down to the steps up to the failing one.
        candidates: dict[str, tuple[Violation, Scenario, ScenarioResult]] = {}
        for r in results:
            for v in r.violations:
                if v.fingerprint() not in candidates:
                    minimal = r.scenario.model_copy(
                        update={"steps": r.scenario.steps[: v.step_index + 1]}
                    )
                    candidates[v.fingerprint()] = (v, minimal, r)
        if not candidates:
            return []

        attempts = self.config.confirmation_attempts
        self.emit(f"Confirming {len(candidates)} candidate(s), {attempts} clean re-run(s) each")
        findings = []
        for fp, (violation, scenario, original) in candidates.items():
            reproduced, evidence = 0, original.exchanges[: violation.step_index + 1]
            starved = False
            try:
                for _ in range(attempts):
                    exchanges, _ = await self.executor.run(scenario)
                    if any(v.fingerprint() == fp for v in check_all(self.spec, exchanges)):
                        reproduced += 1
                        evidence = exchanges
            except BudgetExhausted:
                starved = True
            status = (
                FindingStatus.UNCONFIRMED
                if starved
                else FindingStatus.CONFIRMED
                if reproduced == attempts
                else FindingStatus.FLAKY
                if reproduced
                else FindingStatus.NOT_REPRODUCED
            )
            self.emit(f"  {status.value:15} {_title(violation)} ({reproduced}/{attempts})")
            findings.append(
                Finding(
                    fingerprint=fp,
                    title=_title(violation),
                    oracle=violation.oracle,
                    source=violation.source,
                    severity=violation.severity,
                    status=status,
                    operation_id=violation.operation_id,
                    message=violation.message,
                    key=violation.key,
                    reproduced=reproduced,
                    attempts=attempts,
                    scenario=scenario,
                    evidence=evidence,
                )
            )
        if any(f.status == FindingStatus.UNCONFIRMED for f in findings):
            self.notes.append(
                "The request budget ran out before every candidate could be re-run. Those are "
                "listed as unconfirmed; raise budgets.max_requests to confirm them."
            )
        return findings

    async def _investigate(self, findings: list[Finding]) -> None:
        for finding in findings:
            if finding.status not in (FindingStatus.CONFIRMED, FindingStatus.FLAKY):
                continue
            try:
                result = await self.planner.investigate(self.ctx, finding)
            except LLMError as exc:
                self.notes.append(f"No explanation for {finding.title}: {exc}")
                if isinstance(exc, LLMBudgetExhausted):
                    return
                continue
            finding.explanation = result.explanation
            finding.likely_wrong = result.likely_wrong
