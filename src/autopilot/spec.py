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
    request_format: str | None = None  # "json", "multipart", "form" or None (no body we can send)

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
class LoginHint:
    """A login request found in the spec, used by `init` to pre-fill identities."""

    method: str
    path: str
    format: str  # "json" or "form"
    username_field: str
    password_field: str
    token_path: str
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
    login: LoginHint | None = None
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
    """The schema of the JSON content, or of a wildcard type if there's no JSON one.

    springdoc (Spring Boot) labels most responses "*/*" rather than
    "application/json", so the wildcard fallback matters for every Spring app.
    """
    if not content:
        return None
    for media_type, body in content.items():
        if "json" in media_type:
            return (body or {}).get("schema", {})
    for media_type in ("*/*", "application/*"):
        if media_type in content:
            return (content[media_type] or {}).get("schema", {})
    return None


def _request_body(content: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    """(schema, format) for a request body we know how to send: JSON, multipart or form."""
    if not content:
        return None, None
    for media_type, body in content.items():
        if "json" in media_type:
            return (body or {}).get("schema", {}), "json"
    if "multipart/form-data" in content:
        return (content["multipart/form-data"] or {}).get("schema", {}), "multipart"
    if "application/x-www-form-urlencoded" in content:
        return (content["application/x-www-form-urlencoded"] or {}).get("schema", {}), "form"
    return None, None


def is_binary(schema: dict[str, Any] | None) -> bool:
    """A file field in a multipart body."""
    schema = schema or {}
    return schema.get("type") == "file" or (
        schema.get("type") == "string" and schema.get("format") in ("binary", "base64")
    )


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


_LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sessions?|token|authenticate)/?$", re.IGNORECASE)
_USER_FIELDS = ("username", "email", "login", "user", "user_name", "userName")
_PASSWORD_FIELDS = ("password", "passwd", "pass")
_TOKEN_FIELDS = ("access_token", "accessToken", "token", "jwt", "id_token", "idToken", "auth_token", "authToken")


def _token_path(schema: dict[str, Any] | None) -> str | None:
    props = (schema or {}).get("properties") or {}
    for name in _TOKEN_FIELDS:
        if name in props:
            return f"body.{name}"
    for wrapper in ("data", "result"):  # {"data": {"token": ...}}
        inner = (props.get(wrapper) or {}).get("properties") or {}
        for name in _TOKEN_FIELDS:
            if name in inner:
                return f"body.{wrapper}.{name}"
    return None


def _detect_login(spec: ApiSpec, schemes: dict[str, Any]) -> LoginHint | None:
    # 1. OAuth2 "password" flow: a form post of username and password to tokenUrl.
    for scheme in schemes.values():
        scheme = scheme or {}
        if str(scheme.get("type", "")).lower() != "oauth2":
            continue
        token_url = ((scheme.get("flows") or {}).get("password") or {}).get("tokenUrl")
        if not token_url and scheme.get("flow") == "password":  # Swagger 2.0
            token_url = scheme.get("tokenUrl")
        if token_url:
            return LoginHint("POST", token_url, "form", "username", "password", "body.access_token",
                             f"OAuth2 password flow at {token_url}")
    # 2. A JSON login endpoint that takes a username (or email) and a password. Specs
    # often mark it as protected by mistake (a global security requirement applies to
    # every operation), but logging in can't require being logged in, so that's ignored.
    for op in spec.operations.values():
        if op.method != "POST" or not _LOGIN_PATH.search(op.path):
            continue
        props = (op.request_schema or {}).get("properties") or {}
        user = next((f for f in _USER_FIELDS if f in props), None)
        password = next((f for f in _PASSWORD_FIELDS if f in props), None)
        if not (user and password):
            continue
        success = next((s for code, s in op.responses.items() if code.startswith("2")), None)
        token = _token_path(success)
        note = "" if token else " (token field not documented; check token_path)"
        return LoginHint("POST", op.path, "json", user, password, token or "body.access_token",
                         f"{op.operation_id} ({op.method} {op.path}){note}")
    return None


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
    request_format: str | None = None,
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
        request_format=request_format if request_schema is not None else None,
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
            request_schema, request_format = _request_body(body.get("content"))
            if body.get("content") and request_schema is None:
                non_json.append(f"{method.upper()} {path}")
            responses = {
                str(code): _json_schema((resp or {}).get("content"))
                for code, resp in (raw.get("responses") or {}).items()
            }
            _add_operation(
                spec, path, method, raw, params, request_schema,
                bool(body.get("required", False)), responses, raw.get("security", global_security),
                request_format,
            )
    spec.login = _detect_login(spec, (doc.get("components") or {}).get("securitySchemes") or {})
    if non_json:
        spec.notes.append(
            f"{len(non_json)} operation(s) take bodies Autopilot can't send yet (only JSON, "
            f"multipart and form bodies are supported): {', '.join(non_json[:5])}{' ...' if len(non_json) > 5 else ''}"
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
    for path, item in (doc.get("paths") or {}).items():
        item = item or {}
        for method in HTTP_METHODS:
            raw = item.get(method)
            if raw is None:
                continue
            params: list[Parameter] = []
            request_schema, request_required, request_format = None, False, None
            form_props: dict[str, Any] = {}
            form_required: list[str] = []
            for p in _merge_params(item.get("parameters", []), raw.get("parameters", [])):
                location = p["in"]
                if location == "body":
                    request_schema, request_required = p.get("schema", {}), bool(p.get("required", False))
                    request_format = "json"
                elif location == "formData":
                    prop = {k: p[k] for k in _SWAGGER_PARAM_KEYS if k in p}
                    if p.get("type") == "file":
                        prop = {"type": "string", "format": "binary"}
                    form_props[p["name"]] = prop
                    if p.get("required"):
                        form_required.append(p["name"])
                else:
                    params.append(
                        Parameter(
                            name=p["name"],
                            location=location,
                            required=bool(p.get("required", location == "path")),
                            schema={k: p[k] for k in _SWAGGER_PARAM_KEYS if k in p},
                        )
                    )
            if form_props and request_schema is None:
                request_schema = {"type": "object", "properties": form_props}
                if form_required:
                    request_schema["required"] = form_required
                has_file = any(is_binary(v) for v in form_props.values())
                consumes = raw.get("consumes") or doc.get("consumes") or []
                request_format = "multipart" if has_file or "multipart/form-data" in consumes else "form"
                request_required = bool(form_required)
            responses = {
                str(code): (resp or {}).get("schema")
                for code, resp in (raw.get("responses") or {}).items()
            }
            _add_operation(
                spec, path, method, raw, params, request_schema,
                request_required, responses, raw.get("security", global_security),
                request_format,
            )
    spec.login = _detect_login(spec, doc.get("securityDefinitions") or {})
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
