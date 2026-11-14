"""Execution worker: runs scenarios over HTTP (PRD §9.2).

Responsibilities:
- substitute `{{variables}}` captured by earlier steps,
- attach identity credentials (the LLM never sees them),
- enforce policy and the request budget on every request,
- keep a resource ledger of who created what, for the cross-user oracle.
"""

from __future__ import annotations

import json
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config
from .models import Exchange, Scenario, Step
from .policy import Policy, PolicyViolation
from .spec import ApiSpec, Operation, is_binary

_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
MAX_BODY_CHARS = 4000


class UnresolvedVariable(Exception):
    pass


@dataclass
class OwnedResource:
    value: Any
    owner: str
    operation_id: str


@dataclass
class RunContext:
    """State for one execution of one scenario."""

    variables: dict[str, Any] = field(default_factory=dict)
    # Variable name -> the resource it holds and who created it.
    ledger: dict[str, OwnedResource] = field(default_factory=dict)


def referenced_vars(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_VAR.findall(value))
    if isinstance(value, dict):
        return set().union(*(referenced_vars(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(referenced_vars(v) for v in value)) if value else set()
    return set()


def substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        whole = _VAR.fullmatch(value.strip())
        if whole:  # keep the original type, e.g. an int ID stays an int
            name = whole.group(1)
            if name not in variables:
                raise UnresolvedVariable(name)
            return variables[name]

        def repl(m: re.Match[str]) -> str:
            if m.group(1) not in variables:
                raise UnresolvedVariable(m.group(1))
            return str(variables[m.group(1)])

        return _VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def extract(path: str, status: int, body: Any, headers: dict[str, str]) -> Any:
    """Read `status`, `header.<name>` or `body.a.0.b` from a response."""
    if path == "status":
        return status
    if path.startswith("header."):
        return headers.get(path.removeprefix("header.").lower())
    if path != "body" and not path.startswith("body."):
        raise KeyError(path)
    current = body
    for part in path.split(".")[1:]:
        if isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def _png(width: int = 16, height: int = 16) -> bytes:
    """A small valid PNG (a grey square), so image endpoints accept the upload."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


SAMPLE_FILES: dict[str, tuple[bytes, str]] = {
    ".png": (_png(), "image/png"),
    ".txt": (b"API Autopilot sample file\n", "text/plain"),
    ".json": (b'{"sample": true}\n', "application/json"),
    ".pdf": (b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n", "application/pdf"),
    ".bin": (bytes(range(256)), "application/octet-stream"),
}


def sample_file(value: Any) -> tuple[str, bytes, str]:
    """The file to upload for a file field: "notes.txt" picks a text file, anything else a PNG."""
    name = value if isinstance(value, str) and "." in value else "sample.png"
    content, content_type = SAMPLE_FILES.get("." + name.rsplit(".", 1)[-1].lower(), SAMPLE_FILES[".png"])
    return name, content, content_type


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def build_multipart(schema: dict[str, Any], body: Any) -> tuple[list[tuple[str, tuple[Any, ...]]], dict[str, Any]]:
    """Multipart parts for a body, plus the body as recorded in evidence.

    File fields get a sample file unless the step sets them to null (to test a
    missing file) or names a file ("notes.txt") to choose its type. Objects and
    lists go as application/json parts, as Spring's @RequestPart expects.
    """
    props = (schema or {}).get("properties") or {}
    values = body if isinstance(body, dict) else {}
    parts: list[tuple[str, tuple[Any, ...]]] = []
    recorded: dict[str, Any] = {}
    for name in [*props, *(k for k in values if k not in props)]:
        prop = props.get(name, {})
        if is_binary(prop):
            if name in values and values[name] is None:
                recorded[name] = None  # deliberately left out
                continue
            filename, content, content_type = sample_file(values.get(name))
            parts.append((name, (filename, content, content_type)))
            recorded[name] = f"<file {filename}, {content_type}, {len(content)} bytes>"
        elif name in values and values[name] is not None:
            value = values[name]
            if isinstance(value, (dict, list)):
                parts.append((name, (None, json.dumps(value).encode(), "application/json")))
            else:
                parts.append((name, (None, _text(value).encode())))
            recorded[name] = value
    return parts, recorded


def request_kwargs(op: Operation, body: Any) -> tuple[dict[str, Any], Any]:
    """httpx arguments for the request body, and the body as recorded in evidence."""
    if op.request_format == "multipart":
        parts, recorded = build_multipart(op.request_schema or {}, body)
        return ({"files": parts} if parts else {}), recorded
    if body is None:
        return {}, None
    if op.request_format == "form" and isinstance(body, dict):
        return {"data": {k: _text(v) for k, v in body.items() if v is not None}}, body
    return {"json": body}, body


def _parse_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text[:MAX_BODY_CHARS]


class Executor:
    def __init__(self, config: Config, spec: ApiSpec, policy: Policy, client: httpx.AsyncClient):
        self.config = config
        self.spec = spec
        self.policy = policy
        self.client = client

    def _headers(self, identity: str) -> dict[str, str]:
        # Connection: close gives every request its own connection. Servers often drop the
        # connection after a 5xx, and reusing it would fail the *next* request with a
        # ReadError, making deterministic bugs look flaky. Retrying isn't safe for POSTs.
        headers = {
            "Accept": "application/json",
            "User-Agent": "api-autopilot/0.1",
            "Connection": "close",
        }
        if identity != "anonymous":
            ident = self.config.identities[identity]
            headers[ident.header] = ident.header_value(identity)
        return headers

    async def run_step(self, index: int, step: Step, ctx: RunContext) -> Exchange:
        op = self.spec.operations.get(step.operation_id)
        base = Exchange(
            step_index=index,
            operation_id=step.operation_id,
            identity=step.identity,
            method=op.method if op else "?",
            url=op.path if op else "?",
        )
        try:
            self.policy.check_step(step)
            assert op is not None
            path_params = substitute(step.path_params, ctx.variables)
            query = substitute(step.query, ctx.variables)
            body = substitute(step.body, ctx.variables)
        except (PolicyViolation, UnresolvedVariable) as exc:
            reason = f"unresolved variable {exc}" if isinstance(exc, UnresolvedVariable) else str(exc)
            return base.model_copy(update={"blocked": True, "error": reason})

        path = op.path
        for name, value in path_params.items():
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        url = self.config.target.base_url.rstrip("/") + path
        owners = {
            name: ctx.ledger[name].owner
            for name in referenced_vars(step.path_params)
            if name in ctx.ledger
        }
        body_kwargs, recorded_body = request_kwargs(op, body)
        exchange = base.model_copy(
            update={"url": url, "request_body": recorded_body, "resource_owners": owners}
        )
        try:
            self.policy.check_url(url)
        except PolicyViolation as exc:
            return exchange.model_copy(update={"blocked": True, "error": str(exc)})
        self.policy.spend_request()  # raises BudgetExhausted, which ends the run

        started = time.perf_counter()
        try:
            response = await self.client.request(
                op.method,
                url,
                params={k: v for k, v in query.items() if v is not None} or None,
                **body_kwargs,
                headers=self._headers(step.identity),
                timeout=self.config.budgets.request_timeout_s,
            )
        except httpx.HTTPError as exc:
            return exchange.model_copy(
                update={"error": f"{type(exc).__name__}: {exc}", "elapsed_ms": _ms(started)}
            )

        resp_body = _parse_body(response)
        resp_headers = {k.lower(): v for k, v in response.headers.items()}
        exchange = exchange.model_copy(
            update={
                "status": response.status_code,
                "response_body": resp_body,
                "response_headers": resp_headers,
                "elapsed_ms": _ms(started),
            }
        )

        for var, source in step.extract.items():
            try:
                value = extract(source, response.status_code, resp_body, resp_headers)
            except KeyError:
                continue
            ctx.variables[var] = value
            if op.is_write and 200 <= response.status_code < 300 and step.identity != "anonymous":
                ctx.ledger[var] = OwnedResource(value, step.identity, op.operation_id)
            else:
                ctx.ledger.pop(var, None)  # re-captured by a read: no longer an owned resource
        return exchange

    async def run(self, scenario: Scenario) -> tuple[list[Exchange], RunContext]:
        """Run every step in order. Raises BudgetExhausted if the budget runs out."""
        ctx = RunContext()
        exchanges = [await self.run_step(i, step, ctx) for i, step in enumerate(scenario.steps)]
        return exchanges, ctx


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
