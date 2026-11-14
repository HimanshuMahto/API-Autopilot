"""Groq adapter tests with a fake client: no network, no API key."""

import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
import groq

from autopilot.config import Config, load_dotenv
from autopilot.llm.base import LLMBudgetExhausted, LLMError, PlanningContext
from autopilot.llm.groq_planner import GroqPlanner, strict_schema
from autopilot.llm.schemas import LLMPlan

from .conftest import ROOT, run

PLAN_JSON = '{"reasoning": "r", "hypotheses": []}'


class FakeCompletions:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content, finish = reply if isinstance(reply, tuple) else (reply, "stop")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000),
        )


def planner(*replies, model="openai/gpt-oss-120b", max_calls=5):
    completions = FakeCompletions(*replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return GroqPlanner(model, "medium", max_calls=max_calls, client=client), completions


@pytest.fixture
def ctx(spec):
    return PlanningContext(spec=spec, identities=["alice", "bob", "anonymous"], allow_writes=True, allow_deletes=False)


def test_strict_model_gets_enforced_schema(ctx):
    p, calls = planner(PLAN_JSON)
    assert run(p.plan(ctx)) == LLMPlan(reasoning="r", hypotheses=[])
    call = calls.calls[0]
    assert call["model"] == "openai/gpt-oss-120b"
    assert call["reasoning_effort"] == "medium"
    fmt = call["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == strict_schema(LLMPlan)
    prompt = "".join(m["content"] for m in call["messages"])
    assert "alice-token" not in prompt and "bob-token" not in prompt  # secrets never sent
    assert p.usage.cost_usd == pytest.approx(0.15 + 0.60)


def test_other_models_use_json_mode_with_schema_in_prompt(ctx):
    p, calls = planner(PLAN_JSON, model="llama-3.3-70b-versatile")
    run(p.plan(ctx))
    call = calls.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in call
    assert '"hypotheses"' in call["messages"][0]["content"]
    assert p.usage.cost_usd == 0  # unknown price: reported as unknown, not guessed


def test_invalid_reply_is_retried_with_a_short_note(ctx):
    p, calls = planner('{"reasoning": "r"}', PLAN_JSON)  # first reply misses "hypotheses"
    assert run(p.plan(ctx)).hypotheses == []
    first, retry = calls.calls[0]["messages"], calls.calls[1]["messages"]
    assert len(retry) == len(first) == 2  # the rejected answer isn't sent back
    assert "previous reply was rejected" in retry[-1]["content"]
    assert "hypotheses" in retry[-1]["content"]  # says what was wrong
    assert p.usage.calls == 2


def test_strict_schema_keeps_every_field_the_validator_needs():
    """Regression: the `title` keyword stripper also deleted the `title` *field*."""
    from autopilot.llm.schemas import LLMFollowUp, LLMHypothesis, LLMInvestigation, LLMStep

    for model in (LLMPlan, LLMFollowUp, LLMHypothesis, LLMStep, LLMInvestigation):
        schema = strict_schema(model)
        assert sorted(schema["required"]) == sorted(model.model_fields), model.__name__
    hypothesis = strict_schema(LLMPlan)["properties"]["hypotheses"]["items"]
    assert "title" in hypothesis["properties"] and "title" in hypothesis["required"]


def _rejection(generation):
    """Groq's strict-mode 400 for output that failed its schema check."""
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    body = {"error": {"code": "json_validate_failed", "message": "bad shape", "failed_generation": generation}}
    return groq.BadRequestError("bad", response=httpx.Response(400, request=request), body=body)


def test_groq_schema_rejection_is_retried(ctx):
    p, calls = planner(_rejection('{"x": 1}'), PLAN_JSON)
    assert run(p.plan(ctx)).hypotheses == []
    assert "bad shape" in calls.calls[1]["messages"][-1]["content"]


def test_valid_output_that_groq_rejected_is_used(ctx):
    """Groq's strict check sometimes rejects output that is actually valid."""
    p, calls = planner(_rejection(PLAN_JSON))
    assert run(p.plan(ctx)) == LLMPlan(reasoning="r", hypotheses=[])
    assert len(calls.calls) == 1


def test_gives_up_after_two_retries(ctx):
    p, calls = planner("not json", "still not json", "nope")
    with pytest.raises(LLMError, match="after 2 retries"):
        run(p.plan(ctx))
    assert len(calls.calls) == 3


def test_truncated_reply_is_an_error(ctx):
    p, _ = planner((PLAN_JSON[:10], "length"))
    with pytest.raises(LLMError, match="cut off"):
        run(p.plan(ctx))


def test_bad_key_is_a_clean_error(ctx):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    error = groq.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
    p, _ = planner(error)
    with pytest.raises(LLMError, match="rejected the API key"):
        run(p.plan(ctx))


def test_call_budget_enforced(ctx):
    p, calls = planner(PLAN_JSON, max_calls=1)
    run(p.plan(ctx))
    with pytest.raises(LLMBudgetExhausted):
        run(p.plan(ctx))
    assert len(calls.calls) == 1


def test_missing_key_is_a_clean_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMError, match=r"\$GROQ_API_KEY is not set"):
        GroqPlanner("openai/gpt-oss-120b", "medium", max_calls=1)


def test_loading_the_cli_never_loads_anthropic():
    code = (
        "import sys, autopilot.cli, autopilot.agent, autopilot.llm.groq_planner, autopilot.llm.openai_compat;"
        "assert not [m for m in sys.modules if m.startswith('anthropic')]"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_config_model_only_applies_to_its_provider():
    cfg = Config.model_validate(
        {"spec": "x", "target": {"base_url": "http://localhost"}, "llm": {"provider": "groq", "model": "openai/gpt-oss-20b"}}
    )
    assert cfg.llm.model_for("groq") == "openai/gpt-oss-20b"
    assert cfg.llm.model_for("claude") == "claude-opus-5-5"  # not the Groq model name


def test_dotenv_loads_keys_without_overriding_the_shell(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# keys\nexport AP_TEST_A="quoted value"\nAP_TEST_B=plain\nAP_TEST_C=from-file\n')
    monkeypatch.delenv("AP_TEST_A", raising=False)
    monkeypatch.delenv("AP_TEST_B", raising=False)
    monkeypatch.setenv("AP_TEST_C", "from-shell")
    load_dotenv(env)
    import os

    assert os.environ["AP_TEST_A"] == "quoted value"
    assert os.environ["AP_TEST_B"] == "plain"
    assert os.environ["AP_TEST_C"] == "from-shell"
    for k in ("AP_TEST_A", "AP_TEST_B"):
        monkeypatch.delenv(k)


def test_too_large_follow_up_is_retried_with_fewer_results(ctx, config, spec):
    """A low tokens-per-minute plan rejects a big follow-up; it retries smaller, once."""
    from autopilot.llm.schemas import LLMFollowUp
    from .test_agent_replay import explore

    results = explore(config, spec).results
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    too_big = groq.APIStatusError(
        "Request too large: Limit 8000, Requested 8305",
        response=httpx.Response(413, request=request), body=None,
    )
    p, calls = planner(too_big, '{"reasoning": "r", "hypotheses": []}')
    assert run(p.follow_up(ctx, results)) == LLMFollowUp(reasoning="r", hypotheses=[])
    first, second = (c["messages"][1]["content"] for c in calls.calls)
    assert len(second) < len(first) - 2500
    assert "omitted_scenarios" in second
