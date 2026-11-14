"""Core data contracts shared by every part of the engine.

The LLM never produces these directly. It produces the looser shapes in
`autopilot.llm.schemas`, which the engine converts and validates into these.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FindingStatus(StrEnum):
    CONFIRMED = "confirmed"  # reproduced on every confirmation attempt
    FLAKY = "flaky"  # reproduced on some attempts only
    NOT_REPRODUCED = "not_reproduced"
    # Seen while exploring, but the request budget ran out before the clean re-runs.
    UNCONFIRMED = "unconfirmed"


class ExpectationSource(StrEnum):
    """Where the expected behaviour came from (PRD §6.7)."""

    SPEC = "spec"  # stated in the OpenAPI document
    BUILTIN = "builtin"  # generic oracle that applies to any API
    USER_RULE = "user_rule"  # invariant written by the user (Phase 2)
    AI_INFERRED = "ai_inferred"  # guessed by the model; never auto-confirmed


class Step(BaseModel):
    """One HTTP request inside a scenario."""

    operation_id: str
    identity: str = "anonymous"  # name of a configured identity, or "anonymous"
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    # Variables to capture from the response, e.g. {"order_id": "body.id"}.
    extract: dict[str, str] = Field(default_factory=dict)


class Scenario(BaseModel):
    """An ordered list of steps that tests one hypothesis from a clean start."""

    id: str
    title: str
    hypothesis: str = ""
    steps: list[Step]


class Exchange(BaseModel):
    """A request/response pair as actually sent, after variable substitution."""

    step_index: int
    operation_id: str
    identity: str
    method: str
    url: str
    request_body: Any = None
    status: int | None = None
    response_body: Any = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    elapsed_ms: float = 0.0
    error: str | None = None  # transport error, or the policy reason a step was blocked
    blocked: bool = False
    # Path variables this request used that point at a resource some identity created,
    # mapped to that creator. Lets the cross-user oracle work without guessing.
    resource_owners: dict[str, str] = Field(default_factory=dict)


class Violation(BaseModel):
    """A single oracle failure on a single exchange."""

    oracle: str
    source: ExpectationSource
    severity: Severity
    step_index: int
    operation_id: str
    message: str
    # Stable detail used for de-duplication, e.g. the constraint keyword + field.
    key: str = ""

    def fingerprint(self) -> str:
        raw = f"{self.oracle}|{self.operation_id}|{self.key}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


class ScenarioResult(BaseModel):
    scenario: Scenario
    exchanges: list[Exchange]
    violations: list[Violation]


class Finding(BaseModel):
    fingerprint: str
    title: str
    oracle: str
    source: ExpectationSource
    severity: Severity
    status: FindingStatus
    operation_id: str
    message: str
    key: str = ""
    reproduced: int  # how many confirmation attempts reproduced it
    attempts: int
    scenario: Scenario
    evidence: list[Exchange]
    explanation: str = ""
    likely_wrong: str = "unknown"  # "code", "spec" or "unknown"
