"""Deterministic oracles (PRD §6.7, Appendix C).

An oracle decides whether an exchange is a defect. Oracles never call an LLM, so
the same exchange always gets the same verdict, which is what makes replay and
CI pass/fail trustworthy.

MVP oracles:
  builtin.server_error        any 5xx
  builtin.undocumented_status status code not listed in the spec
  builtin.schema_conformance  response body doesn't match the documented schema
  spec.constraint             request broke a documented constraint, yet got 2xx
  builtin.auth_required       protected operation answered an anonymous caller
  builtin.auth_differential   one identity read/changed a resource another created
  builtin.error_leak          a response exposes SQL, stack traces, internal class
                              names or server file paths
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from jsonschema import Draft4Validator, Draft202012Validator
from jsonschema.exceptions import ValidationError

from .models import Exchange, ExpectationSource, Severity, Violation
from .spec import ApiSpec, Operation, is_binary


# Endpoints that are public by nature. Specs with one global security requirement
# mark them as protected too, so "answered an anonymous caller" isn't a finding here.
_PUBLIC_BY_NATURE = re.compile(
    r"/(login|log-in|signin|sign-in|signup|sign-up|register|token|refresh|forgot-password|"
    r"reset-password|password-reset|verify|health|healthz|ping|status)/?$",
    re.IGNORECASE,
)


def _to_json_schema(schema: dict[str, Any], openapi: str, mode: str) -> dict[str, Any]:
    """Adapt an OpenAPI schema for JSON Schema validation.

    - OpenAPI 3.0's `nullable` becomes a JSON Schema type list.
    - `required` ignores readOnly properties in requests and writeOnly properties in
      responses, as OpenAPI defines. Without this, an API that shares one schema for
      both directions (e.g. a required, readOnly `id`) gets false findings.
    """
    skip = "readOnly" if mode == "request" else "writeOnly"
    is_30 = not openapi.startswith("3.1")

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            if is_30 and node.pop("nullable", False) and "type" in node:
                node["type"] = [node["type"], "null"]
            props, required = node.get("properties"), node.get("required")
            if isinstance(props, dict) and isinstance(required, list):
                kept = [
                    name
                    for name in required
                    if not (isinstance(props.get(name), dict) and props[name].get(skip))
                ]
                if kept:
                    node["required"] = kept
                else:
                    del node["required"]  # Draft 4 rejects an empty `required` list
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(schema))


def _errors(schema: dict[str, Any], instance: Any, openapi: str, mode: str) -> list[ValidationError]:
    validator_cls = Draft202012Validator if openapi.startswith("3.1") else Draft4Validator
    validator = validator_cls(_to_json_schema(schema, openapi, mode))
    # Sort by the index-free path first, so the error a fingerprint is built from doesn't
    # change when the same defect appears at a different position in a list.
    return sorted(
        validator.iter_errors(instance),
        key=lambda e: (_key_path(e), str(e.validator), [str(p) for p in e.absolute_path]),
    )


def _where(err: ValidationError) -> str:
    return ".".join(str(p) for p in err.absolute_path) or "(root)"


def _key_path(err: ValidationError) -> str:
    """Like _where, but list indices become `*` so fingerprints don't depend on data order."""
    return ".".join("*" if isinstance(p, int) else str(p) for p in err.absolute_path) or "(root)"


def _violation(
    oracle: str,
    source: ExpectationSource,
    severity: Severity,
    ex: Exchange,
    message: str,
    key: str = "",
) -> Violation:
    return Violation(
        oracle=oracle,
        source=source,
        severity=severity,
        step_index=ex.step_index,
        operation_id=ex.operation_id,
        message=message,
        key=key,
    )


def _without_files(schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The schema minus file fields (checked separately), and the required file fields."""
    props = schema.get("properties") or {}
    files = [name for name, prop in props.items() if is_binary(prop)]
    if not files:
        return schema, []
    trimmed = dict(schema)
    trimmed["properties"] = {k: v for k, v in props.items() if k not in files}
    required = [r for r in schema.get("required", []) if r not in files]
    if required:
        trimmed["required"] = required
    else:
        trimmed.pop("required", None)
    return trimmed, [r for r in schema.get("required", []) if r in files]


def _request_constraint_errors(op: Operation, ex: Exchange, openapi: str) -> list[str]:
    """Return a stable description of each documented constraint the request broke."""
    problems: list[str] = []
    if op.request_schema is not None and op.request_format == "multipart":
        schema, required_files = _without_files(op.request_schema)
        body = ex.request_body if isinstance(ex.request_body, dict) else {}
        problems += [f"required:{name}" for name in required_files if body.get(name) is None]
        fields = {k: v for k, v in body.items() if k in (schema.get("properties") or {}) or k not in required_files}
        fields = {k: v for k, v in fields.items() if not (isinstance(v, str) and v.startswith("<file "))}
        problems += [f"{err.validator}:{_key_path(err)}" for err in _errors(schema, fields, openapi, "request")]
        return problems
    if op.request_schema is not None:
        if ex.request_body is None:
            if op.request_required:
                problems.append("required:(request body)")
        else:
            for err in _errors(op.request_schema, ex.request_body, openapi, "request"):
                problems.append(f"{err.validator}:{_key_path(err)}")
    return problems


# Internal details that should never reach a client. Each kind is checked separately
# so a finding names what leaked, e.g. "sql" or "stack_trace".
_LEAKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sql", re.compile(
        r"\b(INSERT\s+INTO\s+\w+|SELECT\s+[\w*.,\s]+\s+FROM\s+\w+|UPDATE\s+\w+\s+SET\s|DELETE\s+FROM\s+\w+)"
        r"|SQLSTATE|JDBC exception|syntax error at or near|psycopg2?\.|ORA-\d{5}|SQLITE_\w+|ER_\w+_ERROR",
        re.IGNORECASE)),
    ("stack_trace", re.compile(
        r"\bat [\w$.]+\([\w$]+\.(java|kt|scala|groovy):\d+\)|Traceback \(most recent call last\)"
        r"|File \"[^\"]+\.py\", line \d+|\bat [\w$.<>]+ \([^()\s]+\.(js|ts|mjs|cjs):\d+:\d+\)"
        r"|\.cs:line \d+|goroutine \d+ \[")),
    ("internal_class", re.compile(
        r"\b(java|javax|jakarta|org\.springframework|org\.hibernate|com\.fasterxml|kotlin)\.[\w.$]+"
        r"|\bpublic [\w.<>?,\s]+ [\w.]+\.\w+\(|\b[\w.]+\.(?:[A-Z]\w*)?(?:Exception|Error)\b(?=[:\s])")),
    ("file_path", re.compile(r"(/Users/[\w.-]+/|/home/[\w.-]+/|[A-Z]:\\\\(?:Users|inetpub)\\\\|/var/www/[\w.-]+|/usr/src/app/)")),
)
_LEAK_SCAN_CHARS = 20_000


def _leaks(body: Any) -> list[tuple[str, str]]:
    """(kind, snippet) for each kind of internal detail found in a response body."""
    if body is None:
        return []
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    text = text[:_LEAK_SCAN_CHARS]
    found = []
    for kind, pattern in _LEAKS:
        match = pattern.search(text)
        if match:
            start = max(0, match.start() - 30)
            snippet = text[start:match.end() + 50].replace("\\n", " ").replace("\n", " ")
            found.append((kind, snippet))
    return found


def check_exchange(spec: ApiSpec, ex: Exchange) -> list[Violation]:
    if ex.blocked or ex.status is None:
        return []
    op = spec.operations[ex.operation_id]
    status = ex.status
    ok = 200 <= status < 300
    found: list[Violation] = []

    if status >= 500:
        found.append(
            _violation(
                "builtin.server_error",
                ExpectationSource.BUILTIN,
                Severity.HIGH,
                ex,
                f"{op.method} {op.path} returned {status}.",
                key=str(status),
            )
        )

    leaks = _leaks(ex.response_body)
    if leaks:
        kinds = sorted({kind for kind, _ in leaks})
        found.append(
            _violation(
                "builtin.error_leak",
                ExpectationSource.BUILTIN,
                Severity.MEDIUM,
                ex,
                f"Response {status} exposes internal details ({', '.join(kinds)}): …{leaks[0][1]}…",
                key="|".join(kinds),
            )
        )

    # A 5xx is already reported by server_error; don't report the same crash twice.
    if status < 500 and not op.documents_status(status):
        found.append(
            _violation(
                "builtin.undocumented_status",
                ExpectationSource.SPEC,
                Severity.LOW,
                ex,
                f"Status {status} is not documented for {op.operation_id}.",
                key=str(status),
            )
        )

    schema = op.response_schema(status)
    if schema and ex.response_body is not None and status < 500:
        errors = _errors(schema, ex.response_body, spec.openapi, "response")
        if errors:
            first = errors[0]
            found.append(
                _violation(
                    "builtin.schema_conformance",
                    ExpectationSource.SPEC,
                    Severity.MEDIUM if ok else Severity.LOW,
                    ex,
                    f"Response {status} body does not match the documented schema at "
                    f"`{_where(first)}`: {first.message}"
                    + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""),
                    key=f"{status}:{first.validator}:{_key_path(first)}",
                )
            )

    if ok:
        broken = _request_constraint_errors(op, ex, spec.openapi)
        if broken:
            found.append(
                _violation(
                    "spec.constraint",
                    ExpectationSource.SPEC,
                    Severity.MEDIUM,
                    ex,
                    f"The request broke documented constraint(s) {', '.join(broken)} "
                    f"but the server accepted it with {status}.",
                    key="|".join(sorted(broken)),
                )
            )

        if op.requires_auth and ex.identity == "anonymous" and not _PUBLIC_BY_NATURE.search(op.path):
            found.append(
                _violation(
                    "builtin.auth_required",
                    ExpectationSource.SPEC,
                    Severity.CRITICAL,
                    ex,
                    f"{op.method} {op.path} requires authentication but returned {status} "
                    "to an anonymous request.",
                )
            )

        # Only protected operations have owners; a public endpoint answering anyone is fine.
        if ex.identity != "anonymous" and op.requires_auth:
            for var, owner in ex.resource_owners.items():
                if owner != ex.identity:
                    found.append(
                        _violation(
                            "builtin.auth_differential",
                            ExpectationSource.BUILTIN,
                            Severity.CRITICAL if op.is_write else Severity.HIGH,
                            ex,
                            f"`{ex.identity}` got {status} from {op.method} {op.path} using "
                            f"`{var}`, a resource created by `{owner}`.",
                        )
                    )
                    break
    return found


def check_all(spec: ApiSpec, exchanges: list[Exchange]) -> list[Violation]:
    return [v for ex in exchanges for v in check_exchange(spec, ex)]
