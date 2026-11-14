"""One test per bug found in review."""

from types import SimpleNamespace

import pytest

from autopilot.agent import Autopilot
from autopilot.llm.base import LLMError, PlanningContext, observations
from autopilot.llm.claude import ClaudePlanner
from autopilot.llm.heuristic import HeuristicPlanner
from autopilot.models import Exchange, FindingStatus, ScenarioResult, Step
from autopilot.oracles import check_exchange
from autopilot.replay import load_scenarios, replay, save_scenarios
from autopilot.spec import parse_spec

from .conftest import demo_client, execute, run, scenario


def explore(config, spec):
    async def go():
        async with demo_client() as client:
            return await Autopilot(config, spec, HeuristicPlanner(), client).run()

    return run(go())


def test_budget_starved_candidates_are_reported_not_dropped(config, spec):
    """Exploration used the whole budget, so nothing could be re-run. The candidates must
    still appear (as unconfirmed), never as confirmed and never silently missing."""
    config.budgets.max_requests = 25
    result = explore(config, spec)
    assert "budget" in result.stats.stop_reason.lower()
    assert result.findings, "candidates were dropped"
    assert {f.status for f in result.findings} == {FindingStatus.UNCONFIRMED}
    assert result.confirmed == []
    assert save_scenarios(result.findings, config.scenarios_dir) == []  # not frozen for CI


def test_replay_does_not_call_an_expired_token_a_fix(config, spec):
    result = explore(config, spec)
    save_scenarios(result.findings, config.scenarios_dir)
    frozen = [f for f in load_scenarios(config.scenarios_dir) if f.oracle == "spec.constraint"]
    assert frozen
    config.identities["alice"].value = "expired-token"  # server now answers 401

    async def go():
        async with demo_client() as client:
            return await replay(config, spec, client, frozen)

    assert {r.outcome for r in run(go())} == {"could_not_run"}


def _shared_schema_spec():
    """One schema for request and response, with a required readOnly id (very common)."""
    item = {
        "type": "object",
        "required": ["id", "name", "secret"],
        "properties": {
            "id": {"type": "integer", "readOnly": True},
            "name": {"type": "string"},
            "secret": {"type": "string", "writeOnly": True},
        },
    }
    content = {"application/json": {"schema": item}}
    return parse_spec(
        {
            "openapi": "3.0.3",
            "paths": {
                "/items": {
                    "post": {
                        "operationId": "createItem",
                        "requestBody": {"required": True, "content": content},
                        "responses": {"201": {"description": "ok", "content": content}},
                    }
                }
            },
        }
    )


def _exchange(**kw):
    base = dict(step_index=0, operation_id="createItem", identity="anonymous", method="POST", url="http://x/items")
    return Exchange(**{**base, **kw})


def test_readonly_and_writeonly_required_fields_are_not_false_findings():
    spec = _shared_schema_spec()
    ok = _exchange(request_body={"name": "a", "secret": "s"}, status=201, response_body={"id": 1, "name": "a"})
    assert check_exchange(spec, ok) == []
    # Real problems are still caught in both directions.
    bad_request = _exchange(request_body={"secret": "s"}, status=201, response_body={"id": 1, "name": "a"})
    assert [v.key for v in check_exchange(spec, bad_request)] == ["required:(root)"]
    bad_response = _exchange(request_body={"name": "a", "secret": "s"}, status=201, response_body={"name": "a"})
    assert [v.oracle for v in check_exchange(spec, bad_response)] == ["builtin.schema_conformance"]


def test_public_endpoint_is_not_a_cross_user_finding(config, spec):
    """alice's create also captures product_id; bob reading that public product is fine."""
    sc = scenario(
        Step(
            operation_id="createOrder",
            identity="alice",
            body={"product_id": 1, "quantity": 1},
            extract={"pid": "body.product_id"},
        ),
        Step(operation_id="getProduct", identity="bob", path_params={"product_id": "{{pid}}"}),
    )
    exchanges, violations = execute(config, spec, sc)
    assert [e.status for e in exchanges] == [201, 200]
    assert violations == []


def test_fingerprint_same_when_a_different_list_item_is_broken(spec):
    """The tracked schema error is picked by field, not list position.

    Sorted by position, the first listing below would be tracked as `*.total` and the
    second as `*.quantity`: one defect, two IDs.
    """
    order = {"id": 1, "product_id": 1, "quantity": 1, "total": 5, "status": "placed", "owner": "alice"}

    def listing(*orders):
        ex = Exchange(
            step_index=0, operation_id="listOrders", identity="alice", method="GET",
            url="http://x/orders", status=200, response_body=list(orders),
        )
        return {v.fingerprint() for v in check_exchange(spec, ex)}

    bad_total, bad_quantity = {**order, "total": -1}, {**order, "quantity": 0}
    assert listing(bad_total, bad_quantity) == listing(bad_quantity) != set()


def test_planner_never_sees_values_captured_from_responses(config, spec):
    sc = scenario(
        Step(operation_id="getMe", identity="alice", extract={"who": "body.username"}),
        Step(operation_id="createOrder", identity="alice", body={"product_id": 1, "quantity": 1, "note": "{{who}}"}),
    )
    exchanges, violations = execute(config, spec, sc)
    assert exchanges[1].request_body["note"] == "alice"  # really was substituted on the wire
    sent = observations([ScenarioResult(scenario=sc, exchanges=exchanges, violations=violations)])
    assert '"note":"{{who}}"' in sent
    assert '"alice"' not in sent.replace('"as":"alice"', "")  # only as the identity name


def test_missing_claude_credentials_is_a_clean_error(spec):
    class NoAuth:
        async def parse(self, **_):
            raise TypeError('"Could not resolve authentication method. Expected one of api_key..."')

    client = SimpleNamespace(beta=SimpleNamespace(messages=NoAuth()))
    planner = ClaudePlanner("claude-opus-5-5", "medium", max_calls=3, client=client)
    ctx = PlanningContext(spec=spec, identities=["alice"], allow_writes=True, allow_deletes=False)
    with pytest.raises(LLMError, match="No Claude credentials"):
        run(planner.plan(ctx))


def test_fallback_only_sent_to_models_that_support_it(spec):
    calls = []

    class Recorder:
        async def parse(self, **kw):
            calls.append(kw)
            raise TypeError("authentication")  # stop after recording

    client = SimpleNamespace(beta=SimpleNamespace(messages=Recorder()))
    ctx = PlanningContext(spec=spec, identities=["alice"], allow_writes=True, allow_deletes=False)
    for model in ("claude-opus-5-5", "claude-haiku-5-5"):
        with pytest.raises(LLMError):
            run(ClaudePlanner(model, "medium", max_calls=3, client=client).plan(ctx))
    assert calls[0]["fallbacks"] == "default"
    assert "fallbacks" not in calls[1] and "betas" not in calls[1]


def test_heuristic_planner_handles_non_object_request_bodies():
    spec = parse_spec(
        {
            "openapi": "3.0.3",
            "paths": {
                "/tags": {
                    "post": {
                        "operationId": "setTags",
                        "requestBody": {
                            "content": {"application/json": {"schema": {"type": "string", "required": ["x"]}}}
                        },
                        "responses": {"200": {"description": "ok"}},
                    }
                },
                "/empty": None,
            },
        }
    )
    ctx = PlanningContext(spec=spec, identities=["anonymous"], allow_writes=True, allow_deletes=False)
    run(HeuristicPlanner().plan(ctx))  # used to raise ValueError


def test_seen_before_survives_same_second_and_cut_short_runs(config, spec):
    """A finding confirmed earlier stays "seen before" even if a later run confirmed nothing."""
    from autopilot.store import previous_fingerprints, save_run

    db = config.state_dir / "autopilot.db"
    assert previous_fingerprints(db, "none") is None  # no history yet
    full = explore(config, spec)
    save_run(db, full)
    config.budgets.max_requests = 25
    starved = explore(config, spec)
    starved.finished_at = full.finished_at  # same second, as happens with quick runs
    save_run(db, starved)
    assert previous_fingerprints(db, "next-run") == {f.fingerprint for f in full.confirmed}


def test_observations_stay_under_budget_and_keep_violations(config, spec):
    """Follow-up prompts must fit small tokens-per-minute plans (a 413 from Groq)."""
    result = explore(config, spec)
    budget = 3000
    text = observations(result.results, budget=budget)
    assert len(text) <= budget
    data = __import__("json").loads(text)
    kept = [d for d in data if "scenario" in d]
    flagged = [r for r in result.results if r.violations]
    assert any("violations" in d for d in kept)
    assert data[-1]["omitted_scenarios"] == len(result.results) - len(kept)
    # Every kept scenario with a violation comes before any clean one.
    order = ["violations" in d for d in kept]
    assert order == sorted(order, reverse=True)
    assert len(flagged) and len(kept) < len(result.results)


def test_blocked_and_repeated_scenarios_are_skipped_before_running(config, spec):
    from autopilot.llm.schemas import LLMHypothesis, LLMParam, LLMStep

    def hyp(title, op, **kw):
        return LLMHypothesis(
            title=title, statement="s", expected_oracle="builtin.server_error",
            steps=[LLMStep(operation_id=op, identity="alice", **kw)],
        )

    async def go():
        async with demo_client() as client:
            pilot = Autopilot(config, spec, HeuristicPlanner(), client)
            kept = pilot._scenarios([
                hyp("delete", "deleteOrder", path_params=[LLMParam(name="order_id", value="1")]),
                hyp("me", "getMe"),
                hyp("me again", "getMe"),
            ])
            return kept, pilot.notes, pilot.policy.requests_sent

    kept, notes, sent = run(go())
    assert [s.title for s in kept] == ["me"]
    assert sent == 0
    assert any("allow_deletes" in n for n in notes) and any("repeats" in n for n in notes)
