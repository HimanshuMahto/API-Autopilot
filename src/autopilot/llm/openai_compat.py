"""Planner for any OpenAI-compatible chat API: OpenAI, Gemini, and others.

OpenAI and Gemini are built in (Gemini through Google's OpenAI-compatible
endpoint). With `llm.base_url`, the same adapter talks to any other service that
speaks this API, such as OpenRouter or a local model server.

It asks for strict structured output and reasoning effort first. Providers and
models differ in what they accept, so if one rejects either option, the adapter
drops it for the rest of the run and puts the schema in the prompt instead. The
answer is always validated here, so a looser mode never lets bad output through.
Plain HTTP via httpx: no provider SDK to install.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from .base import LLMBudgetExhausted, LLMError, PromptTooLarge, Usage
from .json_chat import BadOutput, JsonChatPlanner, schema_instructions, short, strict_schema


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str


PROVIDERS = {
    "openai": Provider("OpenAI", "https://api.openai.com/v1"),
    "gemini": Provider("Gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
}

# USD per million tokens (input, output). Unknown models report cost as unknown.
PRICES = {
    "gpt-5.4-mini": (0.75, 4.50),  # developers.openai.com/api/docs/models/gpt-5.4-mini
}

# Our effort levels -> the API's reasoning_effort.
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
MAX_RETRY_WAIT_S = 60.0
REQUEST_TIMEOUT_S = 180.0  # reasoning models can think for a while
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _error_text(response: httpx.Response) -> tuple[str, str]:
    """(message, code) from an error response, in OpenAI's or Gemini's format."""
    try:
        body = response.json()
    except ValueError:
        return short(response.text or response.reason_phrase), ""
    if isinstance(body, list) and body:  # Gemini sometimes wraps the error in a list
        body = body[0]
    error = body.get("error", body) if isinstance(body, dict) else {}
    if isinstance(error, str):
        return short(error), ""
    if not isinstance(error, dict):
        return short(str(body)), ""
    return short(str(error.get("message") or body)), str(error.get("code") or error.get("status") or "")


class OpenAICompatPlanner(JsonChatPlanner):
    def __init__(
        self,
        provider: str,
        model: str,
        effort: str,
        max_calls: int,
        api_key_env: str,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        known = PROVIDERS.get(provider)
        self.name = provider
        self.base_url = (base_url or (known.base_url if known else "")).rstrip("/")
        if not self.base_url:
            raise LLMError(f"llm.base_url is required for provider {provider!r}.")
        host = urlparse(self.base_url).hostname or self.base_url
        self.label = known.label if known and not base_url else host
        self.model = model
        self.effort = effort
        self.max_calls = max_calls
        self.api_key_env = api_key_env
        self.usage = Usage()
        self.transport = transport
        self.key = os.environ.get(api_key_env, "")
        if not self.key and host not in LOCAL_HOSTS:  # a local model server needs no key
            raise LLMError(
                f"${api_key_env} is not set. Put your {self.label} API key in that environment "
                "variable (or in a .env file next to autopilot.yaml), or use `--planner heuristic`."
            )
        # Switched off for the rest of the run if the provider or model rejects them.
        self.structured = True
        self.reasoning = True

    def _schema_enforced(self) -> bool:
        return self.structured

    def _body(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if self.structured:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": strict_schema(schema), "strict": True},
            }
        if self.reasoning:
            body["reasoning_effort"] = EFFORT.get(self.effort, "medium")
        return body

    def _drop_unsupported(self, message: str, messages: list[dict[str, str]], schema: type[BaseModel]) -> bool:
        """After a 400, turn off an option the provider says it doesn't support.

        Returns True if something was turned off (so the request is worth resending).
        """
        text = message.lower()
        if self.reasoning and "reasoning" in text:
            self.reasoning = False
            return True
        if self.structured and any(word in text for word in ("response_format", "json_schema", "schema")):
            self.structured = False
            # The schema is no longer enforced, so spell it out in the prompt.
            messages[0] = {**messages[0], "content": messages[0]["content"] + schema_instructions(schema)}
            return True
        return False

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        async with httpx.AsyncClient(transport=self.transport, timeout=REQUEST_TIMEOUT_S) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
                except httpx.HTTPError:
                    raise LLMError(f"Could not reach {self.label} at {self.base_url} (network error).") from None
                if response.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                    return response
                message, code = _error_text(response)
                if response.status_code == 429 and (
                    code == "insufficient_quota" or "Requested" in message or "quota" in message.lower()
                ):
                    return response  # out of credit, or a request too big to ever fit: waiting won't help
                await asyncio.sleep(self._wait(response, attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _wait(response: httpx.Response, attempt: int) -> float:
        try:
            wait = float(response.headers.get("retry-after", ""))
        except ValueError:
            wait = 2.0 ** attempt
        return min(max(wait, 0.0), MAX_RETRY_WAIT_S)

    async def _send(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> str:
        if self.usage.calls >= self.max_calls:
            raise LLMBudgetExhausted(f"LLM call budget of {self.max_calls} used up")
        self.usage.calls += 1
        messages = list(messages)
        while True:
            response = await self._post(self._body(messages, schema))
            if response.status_code == 400:
                message, _ = _error_text(response)
                if self._drop_unsupported(message, messages, schema):
                    continue  # same call, without the option this provider rejects
            break
        if response.status_code >= 400:
            self._raise(response)

        data = response.json()
        usage = data.get("usage") or {}
        tokens_in, tokens_out = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        self.usage.input_tokens += tokens_in
        self.usage.output_tokens += tokens_out
        price_in, price_out = PRICES.get(self.model, (0.0, 0.0))
        self.usage.cost_usd += (tokens_in * price_in + tokens_out * price_out) / 1_000_000

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.label} returned no answer.")
        choice = choices[0]
        message = choice.get("message") or {}
        if message.get("refusal"):
            raise LLMError(f"{self.label} declined this request: {short(str(message['refusal']))}")
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.label}'s answer was cut off at the token limit.")
        content = message.get("content") or ""
        if not content.strip():
            raise BadOutput("", "the reply was empty")
        return content

    def _raise(self, response: httpx.Response) -> None:
        status = response.status_code
        message, code = _error_text(response)
        if status in (401, 403):
            raise LLMError(f"{self.label} rejected the API key. Check ${self.api_key_env}.")
        sizes = re.search(r"Limit:? (\d+), Requested:? (\d+)", message)
        if status == 413 or (status == 429 and sizes):
            raise PromptTooLarge(
                f"This request is larger than your {self.label} plan allows: {message}",
                requested=int(sizes.group(2)) if sizes else None,
                limit=int(sizes.group(1)) if sizes else None,
            )
        if status == 429:
            if code == "insufficient_quota" or "quota" in message.lower():
                raise LLMError(f"{self.label} says the account is out of quota or credit: {message}")
            raise LLMError(f"Rate limited by {self.label}: {message}")
        if status == 404:
            raise LLMError(f"{self.label} error 404: {message} Check llm.model ({self.model}).")
        raise LLMError(f"{self.label} API error {status}: {message}")
