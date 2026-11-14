"""Claude planner. Each call is stateless: spec summary + observations in,
validated structured output back. The model never sends a request itself."""

from __future__ import annotations

import anthropic
from pydantic import ValidationError

from .base import LLMBudgetExhausted, LLMError, Usage
from .prompts import SYSTEM, PromptPlanner, T

# USD per million tokens (input, output). Unknown models report cost as 0.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}

# Models that accept the server-side refusal fallback.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
NO_CREDENTIALS = (
    "No Claude credentials found. Set ANTHROPIC_API_KEY (or run `ant auth login`), "
    "or use `--planner heuristic`."
)

class ClaudePlanner(PromptPlanner):
    name = "claude"

    def __init__(self, model: str, effort: str, max_calls: int, client: anthropic.AsyncAnthropic | None = None):
        self.model = model
        self.effort = effort
        self.max_calls = max_calls
        self.client = client or anthropic.AsyncAnthropic()
        self.usage = Usage()

    async def _call(self, prompt: str, schema: type[T]) -> T:
        if self.usage.calls >= self.max_calls:
            raise LLMBudgetExhausted(f"LLM call budget of {self.max_calls} used up")
        self.usage.calls += 1
        extra = {}
        if self.model in FALLBACK_MODELS:
            # On a safety decline, let the API retry on its recommended fallback model.
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            response = await self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort},
                output_format=schema,
                **extra,
            )
        except TypeError as exc:
            # The SDK reports missing credentials as a TypeError at request time.
            if "authentication" not in str(exc):
                raise
            raise LLMError(NO_CREDENTIALS) from None
        except ValidationError:
            raise LLMError("Claude's answer did not match the expected format.") from None
        except anthropic.AuthenticationError:
            raise LLMError(
                "Claude rejected the credentials. Set ANTHROPIC_API_KEY (or run `ant auth login`), "
                "or use `--planner heuristic`."
            ) from None
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Rate limited by the Claude API: {exc.message}") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from None
        except anthropic.APIConnectionError:
            raise LLMError("Could not reach the Claude API (network error).") from None
        except anthropic.AnthropicError as exc:
            raise LLMError(f"Claude SDK error: {exc}") from None

        self.usage.input_tokens += response.usage.input_tokens
        self.usage.output_tokens += response.usage.output_tokens
        price_in, price_out = PRICES.get(self.model, (0.0, 0.0))
        self.usage.cost_usd += (
            response.usage.input_tokens * price_in + response.usage.output_tokens * price_out
        ) / 1_000_000

        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request (stop_reason=refusal).")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude's answer was cut off at max_tokens.")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("Claude returned no parseable output.")
        return parsed
