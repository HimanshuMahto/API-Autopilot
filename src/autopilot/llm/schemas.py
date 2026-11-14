"""Shapes the LLM produces, and their conversion into engine models.

These are kept flat and string-typed so they work with strict structured
outputs (no free-form dicts). The engine converts and validates them; anything
that doesn't convert is dropped, never "fixed up" into something that runs.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from ..models import Scenario, Step

ORACLE_IDS = Literal[
    "builtin.server_error",
    "builtin.undocumented_status",
    "builtin.schema_conformance",
    "spec.constraint",
    "builtin.auth_required",
    "builtin.auth_differential",
]


class LLMParam(BaseModel):
    name: str
    value: str = Field(description='Literal value, or "{{var}}" to use a captured variable.')


class LLMExtract(BaseModel):
    var: str = Field(description="Variable name to store the value in, e.g. order_id.")
    source: str = Field(description='Where to read it: "body.id", "body.0.id", "status".')


class LLMStep(BaseModel):
    operation_id: str
    identity: str = Field(description='A configured identity name, or "anonymous".')
    path_params: list[LLMParam] = Field(default_factory=list)
    query: list[LLMParam] = Field(default_factory=list)
    body_json: str | None = Field(
        default=None,
        description='Request body as JSON text. A string value "{{var}}" is replaced by '
        "the captured value, keeping its type.",
    )
    extract: list[LLMExtract] = Field(default_factory=list)


class LLMHypothesis(BaseModel):
    title: str = Field(description="Short name, e.g. 'Bob can read Alice's order'.")
    statement: str = Field(description="What you suspect is wrong and why.")
    expected_oracle: ORACLE_IDS = Field(description="The oracle that would flag the defect.")
    steps: list[LLMStep] = Field(description="Self-contained steps, including any setup.")


class LLMPlan(BaseModel):
    reasoning: str = Field(description="One or two sentences on the strategy.")
    hypotheses: list[LLMHypothesis]


class LLMFollowUp(BaseModel):
    reasoning: str = Field(description="What the observations suggest and what to try next.")
    hypotheses: list[LLMHypothesis] = Field(
        description="New experiments. Empty when nothing is worth following up."
    )


class LLMInvestigation(BaseModel):
    explanation: str = Field(description="2-4 sentences: what is wrong and its likely impact.")
    likely_wrong: Literal["code", "spec", "unknown"]


def _coerce(value: str) -> str | int | float:
    """Path/query values arrive as text; keep numbers numeric for nicer evidence."""
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def to_scenario(h: LLMHypothesis, scenario_id: str) -> Scenario | None:
    """Convert an LLM hypothesis into a Scenario, or None if it is malformed."""
    steps = []
    for s in h.steps:
        try:
            body = json.loads(s.body_json) if s.body_json not in (None, "") else None
        except json.JSONDecodeError:
            return None
        steps.append(
            Step(
                operation_id=s.operation_id,
                identity=s.identity,
                path_params={p.name: _coerce(p.value) for p in s.path_params},
                query={p.name: _coerce(p.value) for p in s.query},
                body=body,
                extract={e.var: e.source for e in s.extract},
            )
        )
    if not steps:
        return None
    return Scenario(id=scenario_id, title=h.title, hypothesis=h.statement, steps=steps)
