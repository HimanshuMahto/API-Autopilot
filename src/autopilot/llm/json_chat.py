"""Shared base for chat-completion planners that return JSON (Groq, OpenAI, Gemini, ...).

The provider adapter sends one request and returns the raw text. This base asks
for the right shape, validates the answer with pydantic, and retries with a
short note when the answer doesn't match. Our validation is the real gate:
whatever a provider's own structured-output mode promises, nothing reaches the
engine unless it parses into the expected model.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ValidationError

from ..spec import _resolve_refs
from .base import LLMError
from .prompts import SYSTEM, PromptPlanner, T

MAX_ERROR_CHARS = 300
MAX_FORMAT_RETRIES = 2  # extra attempts when an answer comes back in the wrong shape


class BadOutput(Exception):
    """The model answered, but not in the required shape."""

    def __init__(self, content: str, detail: str):
        super().__init__(detail)
        self.content = content
        self.detail = detail


def short(text: str) -> str:
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS] + "…"


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> the subset strict structured-output modes accept.

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


def schema_instructions(schema: type[BaseModel]) -> str:
    """For providers without enforced schemas: spell out the shape in the prompt."""
    return "\n\nReply with a single JSON object matching this JSON Schema:\n" + json.dumps(strict_schema(schema))


class JsonChatPlanner(PromptPlanner):
    """Adapters implement `_send`; `label` names the provider in error messages."""

    label = "The AI provider"

    def _schema_enforced(self) -> bool:
        """True when the provider enforces the schema itself (no need to put it in the prompt)."""
        return True

    async def _send(self, messages: list[dict[str, str]], schema: type[BaseModel]) -> str:  # pragma: no cover
        raise NotImplementedError

    async def _call(self, prompt: str, schema: type[T]) -> T:
        detail = ""
        for _ in range(1 + MAX_FORMAT_RETRIES):
            system = SYSTEM if self._schema_enforced() else SYSTEM + schema_instructions(schema)
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
            except BadOutput as bad:
                detail = bad.detail
        raise LLMError(
            f"{self.label}'s answer did not match the expected format after {MAX_FORMAT_RETRIES} retries: {detail}"
        )

    async def _attempt(self, messages: list[dict[str, str]], schema: type[T]) -> T:
        try:
            content = await self._send(messages, schema)
        except BadOutput as rejected:
            # A provider's strict mode occasionally rejects output that is in fact
            # valid. Our own validation decides, so check before giving up on it.
            try:
                return schema.model_validate_json(rejected.content)
            except ValidationError:
                raise rejected from None
        try:
            return schema.model_validate_json(_strip_fences(content))
        except ValidationError as exc:
            raise BadOutput(content, short(str(exc))) from None


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in ```json fences when the schema isn't enforced."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3]
        if stripped.startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()

