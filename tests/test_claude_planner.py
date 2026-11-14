"""Claude adapter tests with a fake client: no network, no API key."""

from types import SimpleNamespace

import pytest

from autopilot.llm.base import LLMBudgetExhausted, LLMError, PlanningContext
from autopilot.llm.claude import ClaudePlanner
from autopilot.llm.schemas import LLMPlan

from .conftest import run


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def fake_client(*responses):
    messages = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def response(parsed, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000),
    )


@pytest.fixture
def ctx(spec):
    return PlanningContext(spec=spec, identities=["alice", "bob", "anonymous"], allow_writes=True, allow_deletes=False)


def test_plan_sends_metadata_only_and_tracks_cost(ctx):
    plan = LLMPlan(reasoning="r", hypotheses=[])
    client, messages = fake_client(response(plan))
    planner = ClaudePlanner("claude-opus-5-5", "medium", max_calls=5, client=client)

    assert run(planner.plan(ctx)) == plan
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is LLMPlan
    assert call["output_config"] == {"effort": "medium"}
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    prompt = call["messages"][0]["content"]
    assert "alice-token" not in prompt and "bob-token" not in prompt  # secrets never sent
    assert planner.usage.cost_usd == pytest.approx(4.0 + 2.0)  # $4/M in, $20/M out


def test_refusal_raises(ctx):
    client, _ = fake_client(response(None, stop_reason="refusal"))
    planner = ClaudePlanner("claude-opus-5-5", "medium", max_calls=5, client=client)
    with pytest.raises(LLMError, match="declined"):
        run(planner.plan(ctx))


def test_call_budget_enforced(ctx):
    plan = LLMPlan(reasoning="r", hypotheses=[])
    client, messages = fake_client(response(plan))
    planner = ClaudePlanner("claude-opus-5-5", "medium", max_calls=1, client=client)
    run(planner.plan(ctx))
    with pytest.raises(LLMBudgetExhausted):
        run(planner.plan(ctx))
    assert len(messages.calls) == 1
