"""Planner interface plus the metadata-only context the planner is allowed to see."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import Finding, ScenarioResult
from ..spec import ApiSpec
from .schemas import LLMFollowUp, LLMInvestigation, LLMPlan


class LLMError(RuntimeError):
    pass


class LLMBudgetExhausted(LLMError):
    pass


class PromptTooLarge(LLMError):
    """The provider refused a request as too large. Carries the sizes when known."""

    def __init__(self, message: str, requested: int | None = None, limit: int | None = None):
        super().__init__(message)
        self.requested = requested
        self.limit = limit


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class Planner(Protocol):
    name: str
    usage: Usage

    async def plan(self, ctx: PlanningContext) -> LLMPlan: ...

    async def follow_up(self, ctx: PlanningContext, results: list[ScenarioResult]) -> LLMFollowUp: ...

    async def investigate(self, ctx: PlanningContext, finding: Finding) -> LLMInvestigation: ...


@dataclass
class PlanningContext:
    spec: ApiSpec
    identities: list[str]  # names only; never tokens
    allow_writes: bool
    allow_deletes: bool
    max_hypotheses: int = 8
    notes: list[str] = field(default_factory=list)

    def spec_summary(self, compact: bool = False) -> str:
        """Compact, token-frugal view of the spec.

        Full schemas are kept for request bodies and success responses; error
        responses are listed by status code only, since they mostly repeat one
        generic error schema. With compact=True (used for follow-ups, which also
        carry the real response shapes) success schemas are dropped too.
        """
        ops = []
        for op in self.spec.operations.values():
            allowed = self.allow_deletes if op.method == "DELETE" else self.allow_writes if op.is_write else True
            entry: dict[str, Any] = {
                "operation_id": op.operation_id,
                "method": op.method,
                "path": op.path,
                "requires_auth": op.requires_auth,
            }
            if not allowed:
                entry["allowed"] = False  # policy will block it; don't propose it
            if op.summary:
                entry["summary"] = op.summary
            if op.parameters:
                entry["parameters"] = [
                    {"name": p.name, "in": p.location, "required": p.required, "schema": p.schema}
                    for p in op.parameters
                ]
            if op.request_schema is not None:
                entry["request_body_schema"] = op.request_schema
                if op.request_format != "json":
                    entry["request_body_format"] = op.request_format
            if compact:
                entry["statuses"] = list(op.responses)
            else:
                entry["success_responses"] = {
                    code: schema for code, schema in op.responses.items() if code.startswith("2")
                }
                errors = [code for code in op.responses if not code.startswith("2")]
                if errors:
                    entry["error_statuses"] = errors
            ops.append(entry)
        doc = {"title": self.spec.title, "description": self.spec.description, "operations": ops}
        return _compact(doc)


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def shape(value: Any, depth: int = 0) -> Any:
    """Describe a response body by structure only (metadata_only data policy).

    Values are replaced by their types so no response data reaches the LLM.
    """
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [f"<{len(value)} items>", shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


# Rough cap on the observations text (about 3,000 tokens). Keeps follow-up prompts
# small enough for low tokens-per-minute plans; see observations().
OBSERVATIONS_CHAR_BUDGET = 12_000


def observations(results: list[ScenarioResult], budget: int = OBSERVATIONS_CHAR_BUDGET) -> str:
    """Summarise executed scenarios for the planner without leaking response values.

    Stays under `budget` characters: scenarios with violations come first, then the
    newest clean ones, and a note says how many were left out. Each response shape
    is described only the first time its operation and status appear.
    """
    seen_shapes: set[tuple[str, int | None]] = set()

    def summarise(r: ScenarioResult) -> dict[str, Any]:
        exchanges = []
        for ex in r.exchanges:
            item: dict[str, Any] = {"op": ex.operation_id, "as": ex.identity, "status": ex.status}
            # The body as the planner wrote it ({{vars}} not filled in), so values
            # captured from responses are never sent back to the LLM.
            body = r.scenario.steps[ex.step_index].body
            if body is not None:
                item["body"] = body
            if ex.error:
                item["error"] = ex.error
            if ex.response_body is not None and (ex.operation_id, ex.status) not in seen_shapes:
                seen_shapes.add((ex.operation_id, ex.status))
                item["shape"] = shape(ex.response_body)
            exchanges.append(item)
        out: dict[str, Any] = {"scenario": r.scenario.title, "steps": exchanges}
        if r.violations:
            # The key (e.g. "200:type:price") says what broke without quoting
            # response values the way the full message can.
            out["violations"] = [
                {"oracle": v.oracle, "step": v.step_index, "detail": v.key} for v in r.violations
            ]
        return out

    def note(omitted: int) -> dict[str, Any]:
        return {"omitted_scenarios": omitted, "reason": "older clean results left out to save space"}

    ordered = [r for r in results if r.violations] + [r for r in reversed(results) if not r.violations]
    kept: list[dict[str, Any]] = []
    # Brackets, plus room for the "omitted" note at its largest possible size.
    used = 2 + len(_compact(note(len(results)))) + 1
    for r in ordered:
        text = _compact(summarise(r))
        if used + len(text) + 1 > budget:
            continue
        kept.append(json.loads(text))
        used += len(text) + 1
    omitted = len(results) - len(kept)
    return _compact(kept + ([note(omitted)] if omitted else []))
