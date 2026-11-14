"""Rule-based planner: no LLM, no API key.

Serves three purposes:
- lets the whole loop run offline and in tests,
- is the "rules only" baseline for the PRD's ablation (§13.5),
- demonstrates the adaptive contract: follow-ups depend on observations.

It emits the same LLMPlan / LLMFollowUp shapes as the Claude planner, so both
go through identical validation and execution.
"""

from __future__ import annotations

import json
from typing import Any

from ..models import Finding, ScenarioResult
from ..spec import Operation, is_binary
from .base import PlanningContext, Usage
from .schemas import LLMExtract, LLMFollowUp, LLMHypothesis, LLMInvestigation, LLMParam, LLMPlan, LLMStep

LIST_WALK = 5  # how many listed items to fetch individually


def example_value(schema: dict[str, Any]) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    if "example" in schema:
        return schema["example"]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "integer":
        return int(schema.get("minimum", 1))
    if t == "number":
        return float(schema.get("minimum", 1))
    if t == "boolean":
        return True
    if t == "array":
        return [example_value(schema.get("items", {}))] if schema.get("minItems") else []
    if t == "object" or "properties" in schema:
        return {k: example_value(v) for k, v in schema.get("properties", {}).items()}
    return "autopilot-test"


def constraint_breakers(schema: dict[str, Any]) -> list[tuple[str, str, Any]]:
    """(field, constraint, bad value) for each documented constraint on top-level fields."""
    out = []
    for name, prop in (schema.get("properties") or {}).items():
        if "minimum" in prop:
            out.append((name, f"minimum {prop['minimum']}", prop["minimum"] - 1))
            if prop["minimum"] >= 1:
                out.append((name, f"minimum {prop['minimum']} (negative)", -5))
        if "maximum" in prop:
            out.append((name, f"maximum {prop['maximum']}", prop["maximum"] + 1))
        if "maxLength" in prop:
            out.append((name, f"maxLength {prop['maxLength']}", "x" * (prop["maxLength"] + 1)))
        if "minLength" in prop and prop["minLength"] > 0:
            out.append((name, f"minLength {prop['minLength']}", ""))
        if "enum" in prop:
            out.append((name, "enum", "autopilot-not-an-option"))
    for name in schema.get("required", []):
        out.append((name, "required", _MISSING))
    return out


_MISSING = object()


def _step(op: Operation, identity: str, **kw: Any) -> LLMStep:
    return LLMStep(operation_id=op.operation_id, identity=identity, **kw)


def _params(**values: str) -> list[LLMParam]:
    return [LLMParam(name=k, value=v) for k, v in values.items()]


class HeuristicPlanner:
    name = "heuristic"

    def __init__(self) -> None:
        self.usage = Usage()
        self._followed_up: set[str] = set()

    # -- helpers ---------------------------------------------------------------

    def _ops(self, ctx: PlanningContext) -> list[Operation]:
        return list(ctx.spec.operations.values())

    def _allowed(self, ctx: PlanningContext, op: Operation) -> bool:
        if op.method == "DELETE":
            return ctx.allow_deletes
        return ctx.allow_writes or not op.is_write

    def _who(self, ctx: PlanningContext, op: Operation, preferred: str) -> str:
        return preferred if op.requires_auth else "anonymous"

    def _collection(self, op: Operation, param: str) -> str:
        return op.path.split("/{" + param + "}")[0]

    def _creator(self, ctx: PlanningContext, path: str) -> Operation | None:
        for op in self._ops(ctx):
            if op.method == "POST" and op.path == path and not op.path_param_names:
                schema = next(
                    (s for code, s in op.responses.items() if code.startswith("2") and s), None
                )
                if schema and "id" in (schema.get("properties") or {}) and self._allowed(ctx, op):
                    return op
        return None

    def _lister(self, ctx: PlanningContext, path: str) -> Operation | None:
        for op in self._ops(ctx):
            if op.method == "GET" and op.path == path and not op.path_param_names:
                return op
        return None

    def _getter(self, ctx: PlanningContext, collection: str) -> Operation | None:
        for op in self._ops(ctx):
            if op.method == "GET" and len(op.path_param_names) == 1 and self._collection(
                op, op.path_param_names[0]
            ) == collection and op.path.endswith("}"):
                return op
        return None

    def _setup(self, ctx: PlanningContext, op: Operation, owner: str) -> tuple[list[LLMStep], bool] | None:
        """Steps that produce a value for every path param of `op`; bool = owned resource."""
        if not op.path_param_names:
            return [], False
        if len(op.path_param_names) > 1:
            return None
        param = op.path_param_names[0]
        collection = self._collection(op, param)
        creator = self._creator(ctx, collection)
        if creator and creator.request_schema is not None:
            body = example_value(creator.request_schema)
            return [
                _step(
                    creator,
                    owner,
                    body_json=json.dumps(body),
                    extract=[LLMExtract(var=param, source="body.id")],
                )
            ], True
        lister = self._lister(ctx, collection)
        if lister:
            return [
                _step(
                    lister,
                    self._who(ctx, lister, owner),
                    extract=[LLMExtract(var=param, source="body.0.id")],
                )
            ], False
        return None

    def _call(self, ctx: PlanningContext, op: Operation, identity: str) -> LLMStep:
        body = None
        if op.request_schema is not None:
            body = json.dumps(example_value(op.request_schema))
        return _step(
            op,
            identity,
            path_params=_params(**{p: "{{" + p + "}}" for p in op.path_param_names}),
            body_json=body,
        )

    # -- planner interface -----------------------------------------------------

    async def plan(self, ctx: PlanningContext) -> LLMPlan:
        named = [i for i in ctx.identities if i != "anonymous"]
        a = named[0] if named else "anonymous"
        b = named[1] if len(named) > 1 else None
        hyps: list[LLMHypothesis] = []

        for op in self._ops(ctx):
            if not self._allowed(ctx, op):
                continue
            setup = self._setup(ctx, op, a)
            if setup is None:
                continue
            steps, owned = setup

            # Cross-user access to a resource another identity created.
            if b and owned and op.requires_auth:
                hyps.append(
                    LLMHypothesis(
                        title=f"{b} uses {a}'s resource via {op.operation_id}",
                        statement=f"{op.operation_id} may not check that the caller owns the resource.",
                        expected_oracle="builtin.auth_differential",
                        steps=[*steps, self._call(ctx, op, b)],
                    )
                )

            # Missing authentication.
            if op.requires_auth:
                hyps.append(
                    LLMHypothesis(
                        title=f"Anonymous call to {op.operation_id}",
                        statement=f"{op.operation_id} is documented as protected; check it rejects anonymous callers.",
                        expected_oracle="builtin.auth_required",
                        steps=[*steps, self._call(ctx, op, "anonymous")],
                    )
                )

            # Repeating a state-changing action on the same resource.
            if op.method == "POST" and op.path_param_names and owned:
                hyps.append(
                    LLMHypothesis(
                        title=f"Repeat {op.operation_id} on the same resource",
                        statement="Repeating a state transition should be rejected cleanly, not crash.",
                        expected_oracle="builtin.server_error",
                        steps=[*steps, self._call(ctx, op, a), self._call(ctx, op, a)],
                    )
                )

            # Documented request constraints.
            if op.is_write and op.request_schema and not op.path_param_names:
                valid = example_value(op.request_schema)
                breakers = constraint_breakers(op.request_schema) if isinstance(valid, dict) else []
                for field, constraint, bad in breakers:
                    body = dict(valid)
                    if bad is _MISSING and op.request_format == "multipart" and is_binary(
                        (op.request_schema.get("properties") or {}).get(field)
                    ):
                        body[field] = None  # an absent file field would get a sample file attached
                    elif bad is _MISSING:
                        body.pop(field, None)
                    else:
                        body[field] = bad
                    hyps.append(
                        LLMHypothesis(
                            title=f"{op.operation_id}: {field} breaks {constraint}",
                            statement=f"The spec documents {constraint} on {field}; the server may not enforce it.",
                            expected_oracle="spec.constraint",
                            steps=[_step(op, self._who(ctx, op, a), body_json=json.dumps(body))],
                        )
                    )

            # Read-only checks: list endpoints, and each listed item individually.
            if op.method == "GET" and not op.path_param_names:
                getter = self._getter(ctx, op.path)
                if getter and not self._creator(ctx, op.path):
                    param = getter.path_param_names[0]
                    walk = [
                        _step(
                            op,
                            self._who(ctx, op, a),
                            extract=[LLMExtract(var=f"{param}_{i}", source=f"body.{i}.id") for i in range(LIST_WALK)],
                        )
                    ]
                    walk += [
                        _step(
                            getter,
                            self._who(ctx, getter, a),
                            path_params=_params(**{param: "{{" + f"{param}_{i}" + "}}"}),
                        )
                        for i in range(LIST_WALK)
                    ]
                    hyps.append(
                        LLMHypothesis(
                            title=f"Fetch each item listed by {op.operation_id}",
                            statement="Every listed item should match the documented schema when fetched.",
                            expected_oracle="builtin.schema_conformance",
                            steps=walk,
                        )
                    )
                else:
                    hyps.append(
                        LLMHypothesis(
                            title=f"Call {op.operation_id}",
                            statement="Response should match the documented schema.",
                            expected_oracle="builtin.schema_conformance",
                            steps=[_step(op, self._who(ctx, op, a))],
                        )
                    )

        return LLMPlan(reasoning="Rule-based coverage of auth, ownership, constraints and state.", hypotheses=hyps)

    async def follow_up(self, ctx: PlanningContext, results: list[ScenarioResult]) -> LLMFollowUp:
        """If a create accepted invalid input, check what that bad record does downstream."""
        hyps = []
        for r in results:
            for v in r.violations:
                if v.oracle != "spec.constraint" or v.fingerprint() in self._followed_up:
                    continue
                step = r.scenario.steps[v.step_index]
                op = ctx.spec.get(step.operation_id)
                getter = self._getter(ctx, op.path)
                if op.method != "POST" or getter is None:
                    continue
                self._followed_up.add(v.fingerprint())
                param = getter.path_param_names[0]
                create = _step(
                    op,
                    step.identity,
                    body_json=json.dumps(step.body),
                    extract=[LLMExtract(var=param, source="body.id")],
                )
                read_back = _step(getter, step.identity, path_params=_params(**{param: "{{" + param + "}}"}))
                steps = [create, read_back]
                lister = self._lister(ctx, op.path)
                if lister:
                    steps.append(_step(lister, step.identity))
                hyps.append(
                    LLMHypothesis(
                        title=f"Read back the invalid record accepted by {op.operation_id} ({v.key})",
                        statement="The invalid record was stored; reading it back shows whether it "
                        "corrupts data other endpoints return.",
                        expected_oracle="builtin.schema_conformance",
                        steps=steps,
                    )
                )
        reasoning = (
            f"{len(hyps)} constraint violation(s) accepted; checking their downstream effect."
            if hyps
            else "Nothing new to follow up."
        )
        return LLMFollowUp(reasoning=reasoning, hypotheses=hyps)

    async def investigate(self, ctx: PlanningContext, finding: Finding) -> LLMInvestigation:
        op = ctx.spec.get(finding.operation_id)
        text = {
            "builtin.auth_differential": f"{op.method} {op.path} does not check that the caller owns "
            "the resource, so one user can reach another user's data (broken object-level authorization).",
            "builtin.auth_required": f"{op.method} {op.path} is documented as protected but answers "
            "anonymous callers.",
            "builtin.server_error": f"{op.method} {op.path} crashes with a server error instead of "
            "returning a documented error response.",
            "spec.constraint": f"{op.method} {op.path} accepts input the spec says is invalid, so bad "
            "data can be stored.",
            "builtin.schema_conformance": f"{op.method} {op.path} returns a body that doesn't match "
            "the documented schema, which can break clients that rely on it.",
            "builtin.undocumented_status": f"{op.method} {op.path} returns a status code the spec "
            "doesn't list.",
            "builtin.error_leak": f"{op.method} {op.path} returns internal details (such as SQL, stack "
            "traces or class names) in its response, which helps attackers map the system.",
        }.get(finding.oracle, finding.message)
        likely = "unknown" if finding.oracle in {"builtin.schema_conformance", "builtin.undocumented_status"} else "code"
        return LLMInvestigation(explanation=text, likely_wrong=likely)
