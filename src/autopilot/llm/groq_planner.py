"""Groq planner (the default). Each call is stateless and returns validated JSON.

Models that support strict structured outputs get the schema enforced by Groq
itself. Other models run in JSON mode with the schema in the prompt; their
answer is validated here and retried once with the validation error.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import groq
from pydantic import BaseModel, ValidationError

from ..spec import _resolve_refs
from .base import LLMBudgetExhausted, LLMError, Usage
from .prompts import SYSTEM, PromptPlanner, T

DEFAULT_MODEL = "openai/gpt-oss-120b"

# Models with strict `json_schema` support (console.groq.com/docs/structured-outputs).
STRICT_MODELS = {"openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"}

# USD per million tokens (input, output), from console.groq.com/docs/models.
# Unknown models report cost as 0.
PRICES = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "qwen/qwen3.8-27b": (0.80, 4.00),
}

# Our effort levels -> Groq's reasoning_effort, for the reasoning models that take it.
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

MAX_COMPLETION_TOKENS = 16384
MAX_ERROR_CHARS = 300
MAX_FORMAT_RETRIES = 2  # extra attempts when an answer comes back in the wrong shape


class _BadOutput(Exception):
    """The model answered, but not in the required shape."""

    def __init__(self, content: str, detail: str):
        super().__init__(detail)
        self.content = content
        self.detail = detail


def _short(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "…"


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> the subset Groq's strict mode accepts.

    Strict mode needs every object to list all its properties as required and set
    additionalProperties to false. Optional fields stay optional by allowing null.
    $refs are inlined so nothing depends on $defs support.
    """
    schema = model.model_json_schema()
    inlined = _resolve_refs(schema, schema)
    inlined.pop("$defs", None)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            # Strip the `title`/`default` keywords, but a *property* may be called
            # "title" too, so the names under `properties` are kept as they are.
            node = {
                k: ({name: walk(sub) for name, sub in v.items()} if k == "properties" else walk(v))
                for k, v in node.items()
                if k not in ("default", "title")
            }
            if node.get("type") == "object" or "properties" in node:
                node["type"] = "object"
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(inlined)


class GroqPlanner(PromptPlanner):
    name = "groq"

    def __init__(
        self,
        model: str,
        effort: str,
        max_calls: int,
        api_key_env: str = "GROQ_API_KEY",
        client: Any | None = None,
    ):
        self.model = model
        self.effort = effort
        self.max_calls = max_calls
        self.usage = Usage()
        if client is None:
            key = os.environ.get(api_key_env)
            if not key:
                raise LLMError(
                    f"${api_key_env} is not set. Put your Groq key in that environment "
                    "variable (or in a .env file next to autopilot.yaml), or use "
                    "`--planner heuristic`."
                )
            # Low tiers allow few tokens per minute, so back-to-back calls can hit 429.
            # The SDK waits as long as Groq's retry-after header asks before retrying.
            client = groq.AsyncGroq(api_key=key, max_retries=5)
        self.client = client

    def _request(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        }
        if self.model in STRICT_MODELS:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": strict_schema(schema), "strict": True},
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}
        if self.model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = EFFORT.get(self.effort, "medium")
        return kwargs

    async def _send(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> str:
        if self.usage.calls >= self.max_calls:
            raise LLMBudgetExhausted(f"LLM call budget of {self.max_calls} used up")
        self.usage.calls += 1
        try:
            response = await self.client.chat.completions.create(**self._request(messages, schema))
        except groq.AuthenticationError:
            raise LLMError("Groq rejected the API key. Check GROQ_API_KEY.") from None
        except groq.RateLimitError as exc:
            raise LLMError(f"Rate limited by Groq: {_short(exc.message)}") from None
        except groq.BadRequestError as exc:
            # Strict mode: Groq refuses to return output that breaks the schema.
            error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
            if isinstance(error, dict) and error.get("code") == "json_validate_failed":
                raise _BadOutput(
                    str(error.get("failed_generation") or ""), _short(str(error.get("message")))
                ) from None
            raise LLMError(f"Groq API error 400: {_short(exc.message)}") from None
        except groq.APIStatusError as exc:
            if exc.status_code == 413:
                found = re.search(r"Limit (\d+), Requested (\d+)", exc.message or "")
                sizes = f" ({found.group(2)} tokens requested, limit {found.group(1)})" if found else ""
                raise LLMError(
                    f"This request is larger than your Groq plan's tokens-per-minute limit{sizes}. "
                    "Try a model with a higher limit (llm.model) or a larger Groq tier."
                ) from None
            raise LLMError(f"Groq API error {exc.status_code}: {_short(exc.message)}") from None
        except groq.APIConnectionError:
            raise LLMError("Could not reach the Groq API (network error).") from None
        except groq.GroqError as exc:
            raise LLMError(f"Groq SDK error: {exc}") from None

        if response.usage is not None:
            self.usage.input_tokens += response.usage.prompt_tokens
            self.usage.output_tokens += response.usage.completion_tokens
            price_in, price_out = PRICES.get(self.model, (0.0, 0.0))
            self.usage.cost_usd += (
                response.usage.prompt_tokens * price_in + response.usage.completion_tokens * price_out
            ) / 1_000_000

        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("Groq's answer was cut off at the token limit.")
        return choice.message.content or ""

    async def _call(self, prompt: str, schema: type[T]) -> T:
        system = SYSTEM
        if self.model not in STRICT_MODELS:
            # JSON mode only guarantees valid JSON, so spell out the shape.
            system += (
                "\n\nReply with a single JSON object matching this JSON Schema:\n"
                + json.dumps(strict_schema(schema))
            )
        detail = ""
        for _ in range(1 + MAX_FORMAT_RETRIES):
            user = prompt
            if detail:
                # A short note rather than the whole rejected answer: retries must stay
                # as small as the original request to fit tokens-per-minute limits.
                user += (
                    f"\n\nYour previous reply was rejected: {detail}\n"
                    "Reply with only a JSON object that matches the schema exactly."
                )
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            try:
                return await self._attempt(messages, schema)
            except _BadOutput as bad:
                detail = bad.detail
        raise LLMError(
            f"Groq's answer did not match the expected format after {MAX_FORMAT_RETRIES} retries: {detail}"
        )

    async def _attempt(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        try:
            content = await self._send(messages, schema)
        except _BadOutput as rejected:
            # Groq's strict mode occasionally rejects output that is in fact valid.
            # Our own validation is the real gate, so check before giving up on it.
            try:
                return schema.model_validate_json(rejected.content)
            except ValidationError:
                raise rejected from None
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise _BadOutput(content, _short(str(exc))) from None
