"""Swagger 2.0 support, loading specs from URLs, and `autopilot init --spec`."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from autopilot import spec as spec_module
from autopilot.cli import app
from autopilot.config import Config, load_config
from autopilot.models import Exchange
from autopilot.oracles import check_exchange
from autopilot.spec import SpecError, load_spec, parse_spec

SWAGGER = {
    "swagger": "2.0",
    "info": {"title": "Pet API", "version": "1.0"},
    "host": "staging.pets.example.com",
    "basePath": "/v1",
    "schemes": ["https"],
    "securityDefinitions": {"key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
    "security": [{"key": []}],
    "definitions": {
        "NewPet": {
            "type": "object",
            "required": ["name", "age"],
            "properties": {"name": {"type": "string", "maxLength": 20}, "age": {"type": "integer", "minimum": 0}},
        },
        "Pet": {
            "type": "object",
            "required": ["id", "name"],
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "tag": {"type": "string", "x-nullable": True},
            },
        },
    },
    "paths": {
        "/pets": {
            "post": {
                "operationId": "addPet",
                "parameters": [{"in": "body", "name": "pet", "required": True, "schema": {"$ref": "#/definitions/NewPet"}}],
                "responses": {"201": {"description": "ok", "schema": {"$ref": "#/definitions/Pet"}}},
            },
            "get": {
                "operationId": "listPets",
                "security": [],
                "parameters": [{"in": "query", "name": "limit", "type": "integer", "maximum": 50}],
                "responses": {"200": {"description": "ok", "schema": {"type": "array", "items": {"$ref": "#/definitions/Pet"}}}},
            },
        },
        "/pets/{petId}": {
            "parameters": [{"in": "path", "name": "petId", "required": True, "type": "integer"}],
            "get": {
                "operationId": "getPet",
                "responses": {"200": {"description": "ok", "schema": {"$ref": "#/definitions/Pet"}}, "404": {"description": "missing"}},
            },
        },
        "/pets/{petId}/photo": {
            "post": {
                "operationId": "uploadPhoto",
                "consumes": ["multipart/form-data"],
                "parameters": [
                    {"in": "path", "name": "petId", "required": True, "type": "integer"},
                    {"in": "formData", "name": "file", "type": "file"},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
    },
}


# --- Swagger 2.0 parsing -------------------------------------------------------


def test_swagger_operations_and_servers():
    spec = parse_spec(SWAGGER)
    assert spec.is_swagger and spec.openapi == "2.0"
    assert spec.servers == ["https://staging.pets.example.com/v1"]
    assert set(spec.operations) == {"addPet", "listPets", "getPet", "uploadPhoto"}


def test_swagger_body_param_becomes_request_schema():
    add = parse_spec(SWAGGER).get("addPet")
    assert add.request_required
    assert add.request_schema["properties"]["age"]["minimum"] == 0  # $ref to #/definitions resolved
    assert add.responses["201"]["required"] == ["id", "name"]


def test_swagger_parameter_keywords_become_a_schema():
    spec = parse_spec(SWAGGER)
    limit = spec.get("listPets").parameters[0]
    assert (limit.location, limit.schema) == ("query", {"type": "integer", "maximum": 50})
    get_pet = spec.get("getPet")
    assert get_pet.path_param_names == ["petId"]  # path-level parameter inherited
    assert get_pet.parameters[0].schema == {"type": "integer"}


def test_swagger_auth_and_security_overrides():
    spec = parse_spec(SWAGGER)
    assert (spec.auth.header, spec.auth.prefix) == ("X-API-Key", "")
    assert spec.get("addPet").requires_auth
    assert not spec.get("listPets").requires_auth  # `security: []` turns auth off


def test_swagger_form_data_is_reported_not_sent():
    spec = parse_spec(SWAGGER)
    upload = spec.get("uploadPhoto")
    assert upload.request_schema is None
    assert upload.path_param_names == ["petId"]
    assert any("form data" in n and "uploadPhoto" not in n and "/pets/{petId}/photo" in n for n in spec.notes)


def _exchange(op, **kw):
    base = dict(step_index=0, operation_id=op, identity="anonymous", method="POST", url="https://x/v1/pets")
    return Exchange(**{**base, **kw})


def test_oracles_work_on_swagger_specs():
    spec = parse_spec(SWAGGER)
    # Request breaks `minimum: 0` but is accepted.
    accepted = _exchange("addPet", request_body={"name": "Rex", "age": -1}, status=201, response_body={"id": 1, "name": "Rex"})
    assert [v.key for v in check_exchange(spec, accepted) if v.oracle == "spec.constraint"] == ["minimum:age"]
    # x-nullable is honoured: a null tag is fine, a numeric one is not.
    ok = _exchange("getPet", method="GET", identity="user_a", status=200, response_body={"id": 1, "name": "Rex", "tag": None})
    assert check_exchange(spec, ok) == []
    bad = _exchange("getPet", method="GET", identity="user_a", status=200, response_body={"id": 1, "name": "Rex", "tag": 5})
    assert [v.key for v in check_exchange(spec, bad)] == ["200:type:tag"]


# --- OpenAPI 3 details ---------------------------------------------------------


def test_openapi3_auth_hint_and_non_json_note():
    spec = parse_spec(
        {
            "openapi": "3.0.3",
            "components": {"securitySchemes": {"jwt": {"type": "http", "scheme": "bearer"}}},
            "security": [{"jwt": []}],
            "paths": {
                "/upload": {
                    "post": {
                        "requestBody": {"content": {"multipart/form-data": {"schema": {"type": "object"}}}},
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            },
        }
    )
    assert (spec.auth.header, spec.auth.prefix) == ("Authorization", "Bearer ")
    assert any("non-JSON" in n for n in spec.notes)


def test_query_api_key_is_flagged():
    doc = {**SWAGGER, "securityDefinitions": {"key": {"type": "apiKey", "in": "query", "name": "api_key"}}}
    spec = parse_spec(doc)
    assert spec.auth is None
    assert any("headers only" in n for n in spec.notes)


# --- Loading from a URL --------------------------------------------------------


def _fake_get(text, status=200):
    def get(url, **_):
        request = httpx.Request("GET", url)
        return httpx.Response(status, text=text, request=request)

    return get


def test_load_spec_from_url(monkeypatch):
    monkeypatch.setattr(spec_module.httpx, "get", _fake_get(json.dumps(SWAGGER)))
    assert load_spec("https://staging.pets.example.com/swagger.json").title == "Pet API"


def test_load_spec_url_errors_are_readable(monkeypatch):
    monkeypatch.setattr(spec_module.httpx, "get", _fake_get("nope", status=404))
    with pytest.raises(SpecError, match="returned 404"):
        load_spec("https://staging.pets.example.com/swagger.json")


def test_config_spec_can_be_a_url(tmp_path):
    cfg = Config.model_validate({"spec": "https://x.example.com/openapi.json", "target": {"base_url": "https://x.example.com"}})
    assert cfg.spec_source == "https://x.example.com/openapi.json"
    cfg2 = Config.model_validate({"spec": "api.yaml", "target": {"base_url": "https://x.example.com"}})
    cfg2.root = tmp_path
    assert cfg2.spec_source == (tmp_path / "api.yaml").resolve()


# --- autopilot init --------------------------------------------------------------

runner = CliRunner()


def test_init_without_spec_writes_the_demo_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "demo/openapi.yaml" in Path("autopilot.yaml").read_text()


def test_init_from_a_swagger_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("swagger.yaml").write_text(yaml.safe_dump(SWAGGER))
    result = runner.invoke(app, ["init", "--spec", "swagger.yaml"])
    assert result.exit_code == 0, result.output
    assert "Swagger 2.0" in result.output and "form data" in result.output

    config = load_config("autopilot.yaml")  # the generated file is valid config
    assert config.target.base_url == "https://staging.pets.example.com/v1"
    assert config.target.allowed_hosts == ["staging.pets.example.com"]
    assert not config.target.allow_writes and not config.target.allow_deletes  # read-only start
    assert config.identities["user_a"].header == "X-API-Key"
    assert config.identities["user_a"].prefix == ""
    assert config.identities["user_b"].value_env == "USER_B_TOKEN"
    assert config.llm.provider == "groq"
    assert load_spec(config.spec_source).title == "Pet API"


def test_init_from_a_url_downloads_the_spec_and_resolves_relative_servers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Shop", "version": "1"},
        "servers": [{"url": "/api/v2"}],
        "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    monkeypatch.setattr(spec_module.httpx, "get", _fake_get(json.dumps(doc)))
    result = runner.invoke(app, ["init", "--spec", "https://staging.shop.example.com/docs/openapi.json"])
    assert result.exit_code == 0, result.output
    assert json.loads(Path("openapi.json").read_text())["info"]["title"] == "Shop"
    config = load_config("autopilot.yaml")
    assert config.spec == "openapi.json"  # a fixed local copy, not the live URL
    assert config.target.base_url == "https://staging.shop.example.com/api/v2"


def test_init_needs_a_base_url_when_the_spec_has_no_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    doc = {k: v for k, v in SWAGGER.items() if k not in ("host", "basePath")}
    Path("swagger.json").write_text(json.dumps(doc))
    result = runner.invoke(app, ["init", "--spec", "swagger.json"])
    assert result.exit_code == 2 and "--base-url" in result.output
    assert not Path("autopilot.yaml").exists()
    result = runner.invoke(app, ["init", "--spec", "swagger.json", "--base-url", "http://localhost:9000/"])
    assert result.exit_code == 0, result.output
    assert load_config("autopilot.yaml").target.base_url == "http://localhost:9000"


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("autopilot.yaml").write_text("keep me")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1
    assert Path("autopilot.yaml").read_text() == "keep me"


def test_init_reports_a_bad_spec_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("bad.json").write_text('{"swagger": "1.2"}')
    result = runner.invoke(app, ["init", "--spec", "bad.json"])
    assert result.exit_code == 2 and "Swagger 2.0 are supported" in result.output
    result = runner.invoke(app, ["init", "--spec", "missing.json"])
    assert result.exit_code == 2 and "not found" in result.output
