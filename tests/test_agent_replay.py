"""End-to-end: explore with the heuristic planner, confirm, freeze, replay."""

import subprocess
import sys

from autopilot.agent import Autopilot
from autopilot.llm.heuristic import HeuristicPlanner
from autopilot.llm.schemas import LLMHypothesis, LLMStep, to_scenario
from autopilot.models import FindingStatus, Step
from autopilot.replay import load_scenarios, replay, save_scenarios
from demo import shop_api

from .conftest import ROOT, demo_client, run, scenario


def explore(config, spec):
    async def go():
        async with demo_client() as client:
            return await Autopilot(config, spec, HeuristicPlanner(), client).run()

    return run(go())


def test_heuristic_run_finds_all_planted_defects(config, spec):
    result = explore(config, spec)
    confirmed = {(f.oracle, f.operation_id) for f in result.confirmed}
    assert ("spec.constraint", "createOrder") in confirmed  # D1
    assert ("builtin.auth_differential", "getOrder") in confirmed  # D2
    assert ("builtin.server_error", "cancelOrder") in confirmed  # D3
    assert ("builtin.schema_conformance", "getProduct") in confirmed  # D4
    # Known-good behaviour produced no findings.
    flagged_ops = {f.operation_id for f in result.findings}
    assert not {"getMe", "deleteOrder"} & flagged_ops
    assert not any(f.oracle in {"builtin.auth_required"} for f in result.findings)
    assert all(f.status == FindingStatus.CONFIRMED for f in result.findings)


def test_follow_up_round_depends_on_observations(config, spec):
    result = explore(config, spec)
    assert result.stats.rounds >= 2
    assert any(r.scenario.title.startswith("Read back the invalid record") for r in result.results)


def test_confirmed_scenario_is_minimal(config, spec):
    result = explore(config, spec)
    d3 = next(f for f in result.findings if f.oracle == "builtin.server_error")
    assert len(d3.scenario.steps) == 3  # create, cancel, cancel; nothing after the failure


def test_replay_reports_still_failing_then_fixed(config, spec, monkeypatch):
    result = explore(config, spec)
    save_scenarios(result.findings, config.scenarios_dir)
    frozen = load_scenarios(config.scenarios_dir)
    assert len(frozen) == len(result.confirmed)

    async def go():
        async with demo_client() as client:
            return await replay(config, spec, client, frozen)

    shop_api.reset()
    outcomes = {r.frozen.fingerprint: r.outcome for r in run(go())}
    assert set(outcomes.values()) == {"still_failing"}

    # Fix D4 (product 3's price) and replay again: only that finding turns "fixed".
    shop_api.reset()
    original_reset = shop_api._fresh_state

    def fixed_state():
        state = original_reset()
        state["products"][3]["price"] = 199.0
        return state

    monkeypatch.setattr(shop_api, "_fresh_state", fixed_state)
    shop_api.reset()
    after = {r.frozen.title: r.outcome for r in run(go())}
    price_titles = [t for t in after if "price" in t]
    assert price_titles and all(after[t] == "fixed" for t in price_titles)
    assert after["Cross-user access in getOrder"] == "still_failing"


def test_replay_marks_unrunnable_scenarios(config, spec):
    from autopilot.models import Finding

    bad = scenario(Step(operation_id="deleteOrder", identity="alice", path_params={"order_id": 1}))
    finding = Finding(
        fingerprint="deadbeef0000", title="t", oracle="builtin.server_error", source="builtin",
        severity="high", status="confirmed", operation_id="deleteOrder", message="m",
        reproduced=2, attempts=2, scenario=bad, evidence=[],
    )
    save_scenarios([finding], config.scenarios_dir)

    async def go():
        async with demo_client() as client:
            return await replay(config, spec, client, load_scenarios(config.scenarios_dir))

    [result] = run(go())
    assert result.outcome == "could_not_run"  # blocked by policy is never "fixed"


def test_replay_never_imports_llm_code():
    # Exactly what `autopilot replay` loads: the CLI module plus autopilot.replay.
    code = (
        "import sys, autopilot.cli, autopilot.replay;"
        "bad=[m for m in sys.modules if m.startswith('autopilot.llm') or m.startswith('anthropic')];"
        "assert not bad, bad"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_malformed_llm_hypothesis_is_dropped():
    h = LLMHypothesis(
        title="t", statement="s", expected_oracle="spec.constraint",
        steps=[LLMStep(operation_id="createOrder", identity="alice", body_json="{not json")],
    )
    assert to_scenario(h, "x") is None
