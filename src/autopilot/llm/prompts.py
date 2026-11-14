"""Prompts shared by every LLM-backed planner.

Each provider adapter only implements `_call(prompt, schema)`. Keeping the
prompts here means Groq and Claude see exactly the same instructions and
context, which keeps their results comparable.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from ..models import Finding, ScenarioResult
from .base import OBSERVATIONS_CHAR_BUDGET, PlanningContext, PromptTooLarge, Usage, observations
from .schemas import LLMFollowUp, LLMInvestigation, LLMPlan

T = TypeVar("T", bound=BaseModel)

SYSTEM = """\
You are API Autopilot, an expert API tester. You look for real defects in an \
HTTP API described by an OpenAPI spec: broken business rules, users reaching \
each other's data, missing auth, documented constraints that are not enforced, \
invalid state transitions, crashes and responses that don't match the spec.

You do not send requests yourself. You propose experiments as scenarios; a \
deterministic engine checks them against policy, runs them, and judges the \
responses with fixed oracles. Write each scenario so it is self-contained: \
include its own setup steps (e.g. create the order before reading it), because \
scenarios run independently and are re-run later to confirm findings.

Rules for steps:
- Use only operation_ids from the spec and identities from the provided list. \
"anonymous" sends no credentials.
- Capture values with `extract` (source "body.id", "body.0.id", "status") and \
reuse them as "{{var}}" in later path params, query params or body values.
- body_json must be valid JSON text, or null when the request has no body.
- For operations whose request_body_format is "multipart" (file uploads), body_json
holds the form fields as a JSON object. File fields get a small sample PNG
automatically; set a file field to null to leave it out, or to a name such as
"notes.txt" to upload a text file instead (to test what the API accepts).
- Never use an operation marked "allowed": false; the engine blocks it.
- Prefer few, sharp experiments over many shallow ones. Each hypothesis should \
name the oracle that would flag the defect.

Oracles: builtin.server_error (5xx), builtin.undocumented_status, \
builtin.schema_conformance (response vs documented schema), spec.constraint \
(request broke a documented constraint but got 2xx), builtin.auth_required \
(protected operation answered anonymous), builtin.auth_differential (one \
identity reached a resource another identity created), builtin.error_leak (a \
response exposes SQL, stack traces, internal class names or server paths)."""


def plan_prompt(ctx: PlanningContext) -> str:
    return (
        f"API spec (normalised):\n{ctx.spec_summary()}\n\n"
        f"Identities: {ctx.identities}\n"
        f"Writes allowed: {ctx.allow_writes}. Deletes allowed: {ctx.allow_deletes}.\n\n"
        f"Propose up to {ctx.max_hypotheses} hypotheses about where this API is most "
        "likely to be broken, each with a scenario that would expose it. Cover "
        "cross-user access, auth, documented constraints, state transitions and "
        "response conformance."
    )


def follow_up_prompt(ctx: PlanningContext, results: list[ScenarioResult], budget: int = OBSERVATIONS_CHAR_BUDGET) -> str:
    return (
        "API spec (normalised; response schemas left out, the experiments below show "
        f"real response shapes):\n{ctx.spec_summary(compact=True)}\n\n"
        f"Identities: {ctx.identities}\n\n"
        "Experiments run so far. Response values are hidden; only their shapes, "
        "status codes and oracle verdicts are shown:\n"
        f"{observations(results, budget=budget)}\n\n"
        "Follow up on anything odd: dig into each violation (what else does it "
        "break? does it affect other operations or other users?) and on surprising "
        "statuses. Don't repeat experiments that already ran. Propose at most "
        f"{ctx.max_hypotheses} new hypotheses, or none if nothing is worth chasing."
    )


def investigate_prompt(ctx: PlanningContext, finding: Finding) -> str:
    steps = [
        {
            "operation_id": ex.operation_id,
            "identity": ex.identity,
            "method": ex.method,
            # The template body, so values captured from responses aren't sent.
            "request_body": finding.scenario.steps[ex.step_index].body,
            "status": ex.status,
        }
        for ex in finding.evidence
    ]
    op = ctx.spec.get(finding.operation_id)
    return (
        f"A defect was confirmed {finding.reproduced}/{finding.attempts} times.\n"
        f"Oracle: {finding.oracle} (expectation source: {finding.source})\n"
        f"Operation: {op.method} {op.path} - {op.summary}\n"
        f"Detail: {finding.key or finding.title}\n"
        f"Steps: {steps}\n\n"
        "Explain the defect for a developer and say whether the code or the spec "
        "is more likely wrong."
    )


class PromptPlanner:
    """Planner whose provider adapter only has to implement `_call`."""

    name = "llm"
    usage: Usage

    async def _call(self, prompt: str, schema: type[T]) -> T:  # pragma: no cover - abstract
        raise NotImplementedError

    async def plan(self, ctx: PlanningContext) -> LLMPlan:
        return await self._call(plan_prompt(ctx), LLMPlan)

    async def follow_up(self, ctx: PlanningContext, results: list[ScenarioResult]) -> LLMFollowUp:
        """Ask for follow-ups. If the provider says the request is too large (a low
        tokens-per-minute plan, a big spec), retry once with fewer past results."""
        try:
            return await self._call(follow_up_prompt(ctx, results), LLMFollowUp)
        except PromptTooLarge as exc:
            over = (exc.requested - exc.limit) if exc.requested and exc.limit else 1500
            # Cut from what was actually sent (often less than the budget). JSON is
            # about 3 characters per token; add a margin for the next call.
            sent = len(observations(results))
            smaller = sent - (over + 600) * 3
            if smaller < 1000:
                raise
            return await self._call(follow_up_prompt(ctx, results, budget=smaller), LLMFollowUp)

    async def investigate(self, ctx: PlanningContext, finding: Finding) -> LLMInvestigation:
        return await self._call(investigate_prompt(ctx, finding), LLMInvestigation)
