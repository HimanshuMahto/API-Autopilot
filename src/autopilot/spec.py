"""OpenAPI 3.x loading and normalisation."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class SpecError(ValueError):
    pass


@dataclass
class Parameter:
    name: str
    location: str  # path | query | header | cookie
    required: bool
    schema: dict[str, Any]


@dataclass
class Operation:
    operation_id: str
    method: str  # upper-case
    path: str
    summary: str
    parameters: list[Parameter]
    request_schema: dict[str, Any] | None
    request_required: bool
    responses: dict[str, dict[str, Any] | None]  # status -> JSON schema (None if no body)
    requires_auth: bool

    @property
    def is_write(self) -> bool:
        return self.method in WRITE_METHODS

    @property
    def path_param_names(self) -> list[str]:
        return [p.name for p in self.parameters if p.location == "path"]

    def documents_status(self, status: int) -> bool:
        s = str(status)
        return s in self.responses or f"{s[0]}XX" in self.responses or "default" in self.responses

    def response_schema(self, status: int) -> dict[str, Any] | None:
        s = str(status)
        for key in (s, f"{s[0]}XX", "default"):
            if key in self.responses:
                return self.responses[key]
        return None


@dataclass
class AuthHint:
    """How the spec says clients authenticate, used to pre-fill identities."""

    header: str
    prefix: str
    description: str


@dataclass
class ApiSpec:
    title: str
    version: str
    description: str
    servers: list[str]
    openapi: str = "3.0.3"  # "2.0" for Swagger documents
    operations: dict[str, Operation] = field(default_factory=dict)
    auth: AuthHint | None = None
    # Parts of the spec Autopilot can't exercise yet, shown by `autopilot inspect`.
    notes: list[str] = field(default_factory=list)

    @property
    def is_swagger(self) -> bool:
        return self.openapi.startswith("2.")

    def get(self, operation_id: str) -> Operation:
        try:
            return self.operations[operation_id]
        except KeyError:
            raise SpecError(f"Unknown operation: {operation_id}") from None


def _resolve_refs(node: Any, root: dict[str, Any], seen: tuple[str, ...] = ()) -> Any:
    """Inline local `$ref`s. Recursive schemas are cut off with an empty schema."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                raise SpecError(f"Only local $refs are supported, got {ref!r}")
            if ref in seen:
                return {}
            target: Any = root
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                try:
                    target = target[part]
                except (KeyError, TypeError):
                    raise SpecError(f"Unresolvable $ref {ref!r}") from None
            return _resolve_refs(copy.deepcopy(target), root, (*seen, ref))
        return {k: _resolve_refs(v, root, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(v, root, seen) for v in node]
    return node


def _json_schema(content: dict[str, Any] | None) -> dict[str, Any] | None:
    if not content:
        return None
    for media_type, body in content.items():
        if "json" in media_type:
            return (body or {}).get("schema", {})
    return None


def _fallback_operation_id(method: str, path: str) -> str:
    cleaned = re.sub(r"[{}]", "", path).strip("/").replace("/", "_") or "root"
    return f"{method.lower()}_{cleaned}"


def _auth_hint(schemes: dict[str, Any], security: list[dict[str, Any]]) -> tuple[AuthHint | None, list[str]]:
    """Pick the scheme the spec uses (the first one named in `security`, else the first defined)."""
    if not schemes:
        return None, []
    preferred = [name for req in security for name in req if name in schemes]
    name = preferred[0] if preferred else next(iter(schemes))
    scheme = schemes[name] or {}
    kind = str(scheme.get("type", "")).lower()
    http_scheme = str(scheme.get("scheme", "")).lower()
    if kind == "apikey":
        if scheme.get("in") == "header":
            header = scheme.get("name", "X-API-Key")
            return AuthHint(header, "", f"API key in the {header} header"), []
        return None, [
            f"Auth scheme {name!r} sends an API key in the {scheme.get('in')}; "
            "identities currently support headers only."
        ]
    if kind == "basic" or (kind == "http" and http_scheme == "basic"):
        return AuthHint("Authorization", "Basic ", "HTTP basic auth (put base64 user:password in the token)"), []
    # http bearer, oauth2 and openIdConnect all end up as a bearer token.
    return AuthHint("Authorization", "Bearer ", "bearer token"), []


def _add_operation(
    spec: ApiSpec,
    path: str,
    method: str,
    raw: dict[str, Any],
    parameters: list[Parameter],
    request_schema: dict[str, Any] | None,
    request_required: bool,
    responses: dict[str, dict[str, Any] | None],
    security: list[dict[str, Any]],
) -> None:
    op_id = raw.get("operationId") or _fallback_operation_id(method, path)
    if op_id in spec.operations:
        raise SpecError(f"Duplicate operationId {op_id!r}")
    spec.operations[op_id] = Operation(
        operation_id=op_id,
        method=method.upper(),
        path=path,
        summary=(raw.get("summary") or raw.get("description") or "").strip(),
        parameters=parameters,
        request_schema=request_schema,
        request_required=request_required,
        responses=responses,
        # An empty requirement object ({}) means anonymous access is allowed.
        requires_auth=bool(security) and all(bool(req) for req in security),
    )


def _merge_params(shared: list[dict[str, Any]], own: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Operation-level parameters override path-level ones with the same name and location."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for p in [*shared, *own]:
        merged[(p["name"], p["in"])] = p
    return list(merged.values())


def _parse_openapi3(doc: dict[str, Any], version: str) -> ApiSpec:
    info = doc.get("info", {})
    global_security = doc.get("security") or []
    spec = ApiSpec(
        title=info.get("title", "Untitled API"),
        version=str(info.get("version", "")),
        description=(info.get("description") or "").strip(),
        servers=[s.get("url", "") for s in doc.get("servers", [])],
        openapi=version,
    )
    spec.auth, notes = _auth_hint((doc.get("components") or {}).get("securitySchemes") or {}, global_security)
    spec.notes += notes
    non_json = []
    for path, item in (doc.get("paths") or {}).items():
        item = item or {}
        for method in HTTP_METHODS:
            raw = item.get(method)
            if raw is None:
                continue
            params = [
                Parameter(
                    name=p["name"],
                    location=p["in"],
                    required=bool(p.get("required", p["in"] == "path")),
                    schema=p.get("schema", {}),
                )
                for p in _merge_params(item.get("parameters", []), raw.get("parameters", []))
            ]
            body = raw.get("requestBody") or {}
            request_schema = _json_schema(body.get("content"))
            if body.get("content") and request_schema is None:
                non_json.append(f"{method.upper()} {path}")
            responses = {
                str(code): _json_schema((resp or {}).get("content"))
                for code, resp in (raw.get("responses") or {}).items()
            }
            _add_operation(
                spec, path, method, raw, params, request_schema,
                bool(body.get("required", False)), responses, raw.get("security", global_security),
            )
    if non_json:
        spec.notes.append(
            f"{len(non_json)} operation(s) take non-JSON bodies (e.g. form uploads), which "
            f"Autopilot can't send yet: {', '.join(non_json[:5])}{' ...' if len(non_json) > 5 else ''}"
        )
    return spec


# Swagger 2.0 keeps a non-body parameter's schema keywords on the parameter itself.
_SWAGGER_PARAM_KEYS = (
    "type", "format", "items", "enum", "default", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "pattern", "minItems", "maxItems",
    "uniqueItems", "multipleOf",
)


def _x_nullable(node: Any) -> Any:
    """Swagger's vendor `x-nullable` means the same as OpenAPI 3.0's `nullable`."""
    if isinstance(node, dict):
        out = {k: _x_nullable(v) for k, v in node.items() if k != "x-nullable"}
        if node.get("x-nullable") is True:
            out["nullable"] = True
        return out
    if isinstance(node, list):
        return [_x_nullable(v) for v in node]
    return node


def _parse_swagger2(doc: dict[str, Any]) -> ApiSpec:
    doc = _x_nullable(doc)
    info = doc.get("info", {})
    host, base_path = doc.get("host"), doc.get("basePath", "") or ""
    schemes = doc.get("schemes") or ["https"]
    if host:
        servers = [f"{schemes[0]}://{host}{base_path}"]
    else:
        servers = [base_path] if base_path else []
    global_security = doc.get("security") or []
    spec = ApiSpec(
        title=info.get("title", "Untitled API"),
        version=str(info.get("version", "")),
        description=(info.get("description") or "").strip(),
        servers=servers,
        openapi="2.0",
    )
    spec.auth, notes = _auth_hint(doc.get("securityDefinitions") or {}, global_security)
    spec.notes += notes
    form_ops = []
    for path, item in (doc.get("paths") or {}).items():
        item = item or {}
        for method in HTTP_METHODS:
            raw = item.get(method)
            if raw is None:
                continue
            params: list[Parameter] = []
            request_schema, request_required = None, False
            for p in _merge_params(item.get("parameters", []), raw.get("parameters", [])):
                location = p["in"]
                if location == "body":
                    request_schema, request_required = p.get("schema", {}), bool(p.get("required", False))
                elif location == "formData":
                    if f"{method.upper()} {path}" not in form_ops:
                        form_ops.append(f"{method.upper()} {path}")
                else:
                    params.append(
                        Parameter(
                            name=p["name"],
                            location=location,
                            required=bool(p.get("required", location == "path")),
                            schema={k: p[k] for k in _SWAGGER_PARAM_KEYS if k in p},
                        )
                    )
            responses = {
                str(code): (resp or {}).get("schema")
                for code, resp in (raw.get("responses") or {}).items()
            }
            _add_operation(
                spec, path, method, raw, params, request_schema,
                request_required, responses, raw.get("security", global_security),
            )
    if form_ops:
        spec.notes.append(
            f"{len(form_ops)} operation(s) take form data, which Autopilot can't send yet: "
            f"{', '.join(form_ops[:5])}{' ...' if len(form_ops) > 5 else ''}"
        )
    return spec


def parse_spec(document: dict[str, Any]) -> ApiSpec:
    """Parse an OpenAPI 3.x or Swagger 2.0 document into one normalised model."""
    swagger = str(document.get("swagger", ""))
    version = str(document.get("openapi", ""))
    if swagger.startswith("2."):
        spec = _parse_swagger2(_resolve_refs(document, document))
    elif version.startswith("3."):
        spec = _parse_openapi3(_resolve_refs(document, document), version)
    else:
        found = f"swagger: {swagger}" if swagger else f"openapi: {version or 'missing'}"
        raise SpecError(f"Only OpenAPI 3.x and Swagger 2.0 are supported (got {found})")
    if not spec.operations:
        raise SpecError("The spec defines no operations")
    return spec


def is_url(source: str | Path) -> bool:
    return str(source).startswith(("http://", "https://"))


def fetch_spec_text(url: str, timeout: float = 30.0) -> str:
    """Download a spec. Raises SpecError with a readable message on failure."""
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SpecError(f"Could not download the spec: {url} returned {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise SpecError(f"Could not download the spec from {url}: {exc}") from None
    return response.text


def parse_spec_text(text: str, source: str) -> ApiSpec:
    try:
        document = yaml.safe_load(text)  # YAML is a superset of JSON, so this reads both
    except yaml.YAMLError as exc:
        raise SpecError(f"Could not parse {source}: {exc}") from None
    if not isinstance(document, dict):
        raise SpecError(f"{source} is not an OpenAPI or Swagger document")
    return parse_spec(document)


def load_spec(source: str | Path) -> ApiSpec:
    """Load a spec from a file path or an http(s) URL."""
    if is_url(source):
        return parse_spec_text(fetch_spec_text(str(source)), str(source))
    p = Path(source)
    try:
        text = p.read_text()
    except FileNotFoundError:
        raise SpecError(f"Spec file not found: {p}") from None
    return parse_spec_text(text, str(p))
