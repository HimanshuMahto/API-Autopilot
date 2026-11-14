"""LLM-free replay of frozen scenarios (PRD §8.5).

Confirmed findings are frozen as YAML in .autopilot/scenarios/. Replay runs
them with the executor and oracles only. This module must never import
autopilot.llm; tests/test_replay.py enforces that.

A scenario counts as "fixed" only if it actually ran end to end and the
fingerprint no longer appears ("resolved requires re-execution", PRD §6.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .config import Config
from .executor import Executor
from .models import Finding, FindingStatus, Scenario
from .oracles import check_all
from .policy import BudgetExhausted, Policy
from .spec import ApiSpec


@dataclass
class FrozenScenario:
    fingerprint: str
    title: str
    oracle: str
    operation_id: str
    severity: str
    flaky: bool
    scenario: Scenario


@dataclass
class ReplayResult:
    frozen: FrozenScenario
    outcome: str  # "still_failing" | "fixed" | "could_not_run"
    detail: str


def save_scenarios(findings: list[Finding], directory: Path) -> list[Path]:
    """Freeze confirmed and flaky findings. Existing files with the same fingerprint are replaced."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for f in findings:
        if f.status not in (FindingStatus.CONFIRMED, FindingStatus.FLAKY):
            continue
        data = {
            "fingerprint": f.fingerprint,
            "title": f.title,
            "oracle": f.oracle,
            "operation_id": f.operation_id,
            "severity": str(f.severity),
            "flaky": f.status == FindingStatus.FLAKY,
            "scenario": f.scenario.model_dump(mode="json", exclude_defaults=True),
        }
        path = directory / f"{f.fingerprint}.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        written.append(path)
    return written


def load_scenarios(directory: Path) -> list[FrozenScenario]:
    frozen = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        frozen.append(
            FrozenScenario(
                fingerprint=data["fingerprint"],
                title=data["title"],
                oracle=data["oracle"],
                operation_id=data["operation_id"],
                severity=data.get("severity", "medium"),
                flaky=bool(data.get("flaky", False)),
                scenario=Scenario.model_validate(data["scenario"]),
            )
        )
    return frozen


async def replay(
    config: Config, spec: ApiSpec, client: httpx.AsyncClient, frozen: list[FrozenScenario]
) -> list[ReplayResult]:
    policy = Policy(config, spec)
    executor = Executor(config, spec, policy, client)
    results = []
    for item in frozen:
        try:
            exchanges, _ = await executor.run(item.scenario)
        except BudgetExhausted as exc:
            results.append(ReplayResult(item, "could_not_run", str(exc)))
            continue
        problems = [e for e in exchanges if e.blocked or e.status is None]
        # An expired token would otherwise look like a fix (the bad request is "rejected").
        rejected = [e for e in exchanges if e.status == 401 and e.identity != "anonymous"]
        violations = check_all(spec, exchanges)
        if any(v.fingerprint() == item.fingerprint for v in violations):
            match = next(v for v in violations if v.fingerprint() == item.fingerprint)
            results.append(ReplayResult(item, "still_failing", match.message))
        elif problems:
            p = problems[0]
            results.append(
                ReplayResult(item, "could_not_run", f"step {p.step_index} ({p.operation_id}): {p.error}")
            )
        elif rejected:
            r = rejected[0]
            results.append(
                ReplayResult(
                    item,
                    "could_not_run",
                    f"step {r.step_index} ({r.operation_id}): 401 for identity `{r.identity}`. Its "
                    "credentials were rejected, so this run says nothing about the defect.",
                )
            )
        else:
            results.append(ReplayResult(item, "fixed", "Ran end to end; the defect no longer appears."))
    return results
