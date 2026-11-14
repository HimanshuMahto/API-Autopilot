"""Execution worker: runs scenarios over HTTP (PRD §9.2).

Responsibilities:
- substitute `{{variables}}` captured by earlier steps,
- attach identity credentials (the LLM never sees them),
- enforce policy and the request budget on every request,
- keep a resource ledger of who created what, for the cross-user oracle.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from .config import Config
from .models import Exchange, Scenario, Step
from .policy import Policy, PolicyViolation
from .spec import ApiSpec

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
        exchange = base.model_copy(
            update={"url": url, "request_body": body, "resource_owners": owners}
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
                json=body if body is not None else None,
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
