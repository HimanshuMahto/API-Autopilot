"""Groq planner. Each call is stateless and returns validated JSON.

Models that support strict structured outputs get the schema enforced by Groq
itself. Other models run in JSON mode with the schema in the prompt; their
answer is validated here and retried once with the validation error.
"""

from __future__ import annotations

import os
import re
from typing import Any

import groq
from pydantic import BaseModel

from .base import LLMBudgetExhausted, LLMError, PromptTooLarge, Usage
from .json_chat import BadOutput, JsonChatPlanner, short, strict_schema

__all__ = ["GroqPlanner", "strict_schema"]

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


class GroqPlanner(JsonChatPlanner):
    name = "groq"
    label = "Groq"

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
            raise LLMError(f"Rate limited by Groq: {short(exc.message)}") from None
        except groq.BadRequestError as exc:
            # Strict mode: Groq refuses to return output that breaks the schema.
            error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
            if isinstance(error, dict) and error.get("code") == "json_validate_failed":
                raise BadOutput(
                    str(error.get("failed_generation") or ""), short(str(error.get("message")))
                ) from None
            raise LLMError(f"Groq API error 400: {short(exc.message)}") from None
        except groq.APIStatusError as exc:
            if exc.status_code == 413:
                found = re.search(r"Limit (\d+), Requested (\d+)", exc.message or "")
                sizes = f" ({found.group(2)} tokens requested, limit {found.group(1)})" if found else ""
                raise PromptTooLarge(
                    f"This request is larger than your Groq plan's tokens-per-minute limit{sizes}. "
                    "Try a model with a higher limit (llm.model) or a larger Groq tier.",
                    requested=int(found.group(2)) if found else None,
                    limit=int(found.group(1)) if found else None,
                ) from None
            raise LLMError(f"Groq API error {exc.status_code}: {short(exc.message)}") from None
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

    def _schema_enforced(self) -> bool:
        return self.model in STRICT_MODELS
