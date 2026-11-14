"""OpenAI, Gemini and other OpenAI-compatible APIs, against a simulated server.

No network and no real keys: httpx.MockTransport plays the provider, so these
check what we send and how we handle each kind of answer and error.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from autopilot.cli import app
from autopilot.config import Config
from autopilot.llm.base import LLMBudgetExhausted, LLMError, PlanningContext, PromptTooLarge
from autopilot.llm.json_chat import strict_schema
from autopilot.llm.openai_compat import OpenAICompatPlanner
from autopilot.llm.schemas import LLMPlan

from .conftest import run

PLAN = {"reasoning": "r", "hypotheses": []}
AI_KEYS = ("OPENAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY")


def answer(content, finish="stop", **extra):
    message = {"role": "assistant", "content": content, **extra}
    return httpx.Response(200, json={
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
    })


def error(status, message, code=None, headers=None):
    return httpx.Response(status, json={"error": {"message": message, "code": code}}, headers=headers)


class FakeProvider:
    """Replies in order and records every request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0)

    def body(self, i=0):
        return json.loads(self.requests[i].content)


def planner(*replies, provider="openai", model="gpt-5.4-mini", base_url=None, max_calls=5):
    fake = FakeProvider(*replies)
    p = OpenAICompatPlanner(provider, model, "medium", max_calls, "TEST_AI_KEY", base_url=base_url,
                            transport=httpx.MockTransport(fake))
    return p, fake


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv("TEST_AI_KEY", "test-key-123")
    monkeypatch.setattr("autopilot.llm.openai_compat.asyncio.sleep", _no_sleep)


async def _no_sleep(_):
    return None


@pytest.fixture
def ctx(spec):
    return PlanningContext(spec=spec, identities=["alice", "bob", "anonymous"], allow_writes=True, allow_deletes=False)


def test_openai_request_has_strict_schema_effort_and_key(ctx):
    p, fake = planner(answer(json.dumps(PLAN)))
    assert run(p.plan(ctx)) == LLMPlan(**PLAN)
    req = fake.requests[0]
    assert str(req.url) == "https://api.openai.com/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key-123"
    body = fake.body()
    assert body["model"] == "gpt-5.4-mini" and body["reasoning_effort"] == "medium"
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"] == strict_schema(LLMPlan)
    prompt = "".join(m["content"] for m in body["messages"])
    assert "alice-token" not in prompt and "bob-token" not in prompt  # secrets never sent
    assert p.usage.cost_usd == pytest.approx(0.75 + 4.50)


def test_gemini_uses_googles_openai_compatible_endpoint(ctx):
    p, fake = planner(answer(json.dumps(PLAN)), provider="gemini", model="gemini-flash-latest")
    run(p.plan(ctx))
    assert str(fake.requests[0].url) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert p.usage.cost_usd == 0  # unknown price: reported as unknown, not guessed


def test_custom_base_url_reaches_any_compatible_service(ctx, monkeypatch):
    p, fake = planner(answer(json.dumps(PLAN)), base_url="https://openrouter.ai/api/v1/", model="some/model")
    run(p.plan(ctx))
    assert str(fake.requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert p.label == "openrouter.ai"


def test_local_model_server_needs_no_key(ctx, monkeypatch):
    monkeypatch.delenv("TEST_AI_KEY")
    p, fake = planner(answer(json.dumps(PLAN)), base_url="http://localhost:11434/v1", model="llama3")
    run(p.plan(ctx))
    assert "authorization" not in fake.requests[0].headers


def test_missing_key_is_a_clean_error(monkeypatch):
    monkeypatch.delenv("TEST_AI_KEY")
    with pytest.raises(LLMError, match=r"\$TEST_AI_KEY is not set. Put your OpenAI API key"):
        OpenAICompatPlanner("openai", "gpt-5.4-mini", "medium", 1, "TEST_AI_KEY")


def test_model_without_reasoning_effort_drops_it_and_resends(ctx):
    p, fake = planner(
        error(400, "Unrecognized request argument supplied: reasoning_effort"),
        answer(json.dumps(PLAN)), answer(json.dumps(PLAN)),
    )
    run(p.plan(ctx))
    assert "reasoning_effort" not in fake.body(1)
    assert p.usage.calls == 1  # the resend is the same call, not a new one
    run(p.plan(ctx))
    assert "reasoning_effort" not in fake.body(2)  # stays off for the rest of the run


def test_provider_without_strict_schemas_gets_the_schema_in_the_prompt(ctx):
    p, fake = planner(
        error(400, "Invalid value for response_format: json_schema is not supported"),
        answer("```json\n" + json.dumps(PLAN) + "\n```"),  # fenced JSON is accepted
    )
    assert run(p.plan(ctx)) == LLMPlan(**PLAN)
    retry = fake.body(1)
    assert "response_format" not in retry
    assert '"hypotheses"' in retry["messages"][0]["content"]  # schema now in the prompt


def test_other_400s_are_errors_not_retried(ctx):
    p, fake = planner(error(400, "messages: too many messages"))
    with pytest.raises(LLMError, match="OpenAI API error 400: messages: too many"):
        run(p.plan(ctx))
    assert len(fake.requests) == 1


def test_invalid_answer_is_retried_with_a_short_note(ctx):
    p, fake = planner(answer('{"reasoning": "r"}'), answer(json.dumps(PLAN)))
    assert run(p.plan(ctx)).hypotheses == []
    assert "previous reply was rejected" in fake.body(1)["messages"][-1]["content"]
    assert p.usage.calls == 2


def test_rate_limit_waits_and_retries(ctx):
    p, fake = planner(error(429, "slow down", headers={"retry-after": "2"}), answer(json.dumps(PLAN)))
    run(p.plan(ctx))
    assert len(fake.requests) == 2


def test_out_of_quota_is_reported_without_retrying(ctx):
    p, fake = planner(error(429, "You exceeded your current quota.", code="insufficient_quota"))
    with pytest.raises(LLMError, match="out of quota or credit"):
        run(p.plan(ctx))
    assert len(fake.requests) == 1


def test_request_too_large_feeds_the_smaller_retry(ctx):
    p, _ = planner(error(429, "Request too large for gpt-5.4-mini: Limit 30000, Requested 35000."))
    with pytest.raises(PromptTooLarge) as exc:
        run(p.plan(ctx))
    assert (exc.value.limit, exc.value.requested) == (30000, 35000)


@pytest.mark.parametrize("status", [401, 403])
def test_bad_key_is_a_clean_error(ctx, status):
    p, _ = planner(error(status, "invalid api key"))
    with pytest.raises(LLMError, match=r"OpenAI rejected the API key. Check \$TEST_AI_KEY"):
        run(p.plan(ctx))


def test_gemini_list_shaped_errors_are_read(ctx):
    response = httpx.Response(400, json=[{"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}}])
    p, _ = planner(response, provider="gemini", model="gemini-flash-latest")
    with pytest.raises(LLMError, match="Gemini API error 400: API key not valid"):
        run(p.plan(ctx))


def test_unknown_model_names_the_setting(ctx):
    p, _ = planner(error(404, "The model `gpt-9` does not exist."))
    with pytest.raises(LLMError, match=r"Check llm.model \(gpt-5.4-mini\)"):
        run(p.plan(ctx))


def test_truncated_refused_and_empty_answers(ctx):
    p, _ = planner(answer(json.dumps(PLAN)[:10], finish="length"))
    with pytest.raises(LLMError, match="cut off"):
        run(p.plan(ctx))
    p, _ = planner(answer(None, refusal="I can't help with that."))
    with pytest.raises(LLMError, match="declined"):
        run(p.plan(ctx))
    p, fake = planner(answer(""), answer(""), answer(""))
    with pytest.raises(LLMError, match="the reply was empty"):
        run(p.plan(ctx))
    assert len(fake.requests) == 3


def test_call_budget_enforced(ctx):
    p, fake = planner(answer(json.dumps(PLAN)), max_calls=1)
    run(p.plan(ctx))
    with pytest.raises(LLMBudgetExhausted):
        run(p.plan(ctx))
    assert len(fake.requests) == 1


def test_network_error_is_clean(ctx):
    def down(request):
        raise httpx.ConnectError("refused")

    p = OpenAICompatPlanner("openai", "m", "medium", 1, "TEST_AI_KEY", transport=httpx.MockTransport(down))
    with pytest.raises(LLMError, match="Could not reach OpenAI"):
        run(p.plan(ctx))


# --- Choosing a provider --------------------------------------------------------------


def llm_config(**llm):
    return Config.model_validate({"spec": "x", "target": {"base_url": "http://localhost"}, "llm": llm}).llm


def test_auto_picks_whichever_key_is_set_with_claude_last(monkeypatch):
    for key in AI_KEYS:
        monkeypatch.delenv(key, raising=False)
    cfg = llm_config()
    assert cfg.provider == "auto" and cfg.resolve() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert cfg.resolve() == "claude"
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert cfg.resolve() == "gemini"  # another key present: Claude isn't picked
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert cfg.resolve() == "openai"
    assert cfg.resolve("groq") == "groq"  # --planner always wins


def test_named_provider_is_never_overridden_by_auto(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert llm_config(provider="groq").resolve() == "groq"


def test_defaults_per_provider():
    cfg = llm_config()
    assert cfg.model_for("openai") == "gpt-5.4-mini" and cfg.key_env_for("openai") == "OPENAI_API_KEY"
    assert cfg.model_for("gemini") == "gemini-flash-latest" and cfg.key_env_for("gemini") == "GEMINI_API_KEY"
    custom = llm_config(provider="openai", base_url="https://openrouter.ai/api/v1", model="x/y",
                        api_key_env="OPENROUTER_API_KEY")
    assert custom.base_url_for("openai") == "https://openrouter.ai/api/v1"
    assert custom.base_url_for("gemini") is None  # only applies to the configured provider
    assert custom.key_env_for("openai") == "OPENROUTER_API_KEY"


def test_run_without_any_ai_key_explains_the_options(tmp_path, monkeypatch):
    for key in AI_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0  # demo config, provider auto
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2
    assert "No AI key found" in result.output and "--planner heuristic" in result.output


def test_auto_says_which_key_it_used(tmp_path, monkeypatch):
    for key in AI_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.chdir(tmp_path)
    from autopilot.cli import _make_planner
    from autopilot.config import load_config

    CliRunner().invoke(app, ["init"])
    p = _make_planner(load_config("autopilot.yaml"), None)
    assert isinstance(p, OpenAICompatPlanner) and p.name == "gemini"
    assert p.model == "gemini-flash-latest"
