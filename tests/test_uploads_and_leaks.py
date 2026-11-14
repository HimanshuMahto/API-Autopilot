"""File uploads (multipart), cross-user checks on uploaded files, and the error-leak oracle."""

from __future__ import annotations

import itertools

import httpx
import yaml
from fastapi import FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

from autopilot.agent import Autopilot
from autopilot.config import Config
from autopilot.executor import Executor, build_multipart
from autopilot.llm.heuristic import HeuristicPlanner
from autopilot.models import Exchange, Scenario, Step
from autopilot.oracles import check_all, check_exchange
from autopilot.policy import Policy
from autopilot.spec import parse_spec

from .conftest import run

# --- A small upload API with two planted bugs ---------------------------------------
#   1. GET /files/{id} has no ownership check (cross-user access to uploaded files).
#   2. Uploading without a file crashes and leaks a Java-style SQL error.

TOKENS = {"a-token": "alice", "b-token": "bob"}
FILES: dict[int, dict] = {}
IDS = itertools.count(1)
upload_app = FastAPI()


def _user(authorization: str | None) -> str | None:
    return TOKENS.get((authorization or "").removeprefix("Bearer ").strip())


@upload_app.post("/files", status_code=201)
async def upload(file: UploadFile | None = File(None), caption: str | None = Form(None),
                 authorization: str | None = Header(None)):
    user = _user(authorization)
    if not user:
        return JSONResponse({"detail": "login required"}, 401)
    if file is None:
        return JSONResponse({"error": "JDBC exception executing SQL [INSERT INTO files (owner) VALUES (?)]"}, 500)
    content = await file.read()
    record = {"id": next(IDS), "owner": user, "name": file.filename, "size": len(content),
              "type": file.content_type, "caption": caption}
    FILES[record["id"]] = record
    return record


@upload_app.get("/files/{file_id}")
def get_file(file_id: int, authorization: str | None = Header(None)):
    user = _user(authorization)
    if not user:
        return JSONResponse({"detail": "login required"}, 401)
    record = FILES.get(file_id)  # bug: no `record["owner"] == user` check
    if not record:
        return JSONResponse({"detail": "not found"}, 404)
    return record


FILE = {"type": "object", "required": ["id", "owner", "name", "size"], "properties": {
    "id": {"type": "integer"}, "owner": {"type": "string"}, "name": {"type": "string"},
    "size": {"type": "integer"}, "type": {"type": "string"}, "caption": {"type": ["string", "null"]}}}
ERR = {"type": "object"}
UPLOAD_SPEC = {
    "openapi": "3.1.0",
    "security": [{"bearer": []}],
    "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    "paths": {
        "/files": {"post": {
            "operationId": "uploadFile",
            "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
                "type": "object", "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}, "caption": {"type": "string"}}}}}},
            "responses": {"201": {"description": "ok", "content": {"*/*": {"schema": FILE}}},
                          "401": {"description": "no", "content": {"*/*": {"schema": ERR}}}},
        }},
        "/files/{file_id}": {"get": {
            "operationId": "getFile",
            "parameters": [{"name": "file_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "ok", "content": {"*/*": {"schema": FILE}}},
                          "401": {"description": "no", "content": {"*/*": {"schema": ERR}}},
                          "404": {"description": "no", "content": {"*/*": {"schema": ERR}}}},
        }},
    },
}


def upload_config(tmp_path) -> Config:
    cfg = Config.model_validate(yaml.safe_load('''
spec: unused
target: {base_url: "http://files.test", allow_writes: true}
identities:
  alice: {value: a-token}
  bob: {value: b-token}
'''))
    cfg.root = tmp_path
    return cfg


def upload_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_app, raise_app_exceptions=False))


# --- Parsing and building multipart requests ----------------------------------------


def test_multipart_upload_is_parsed_and_sendable():
    op = parse_spec(UPLOAD_SPEC).get("uploadFile")
    assert op.request_format == "multipart" and op.request_schema["required"] == ["file"]


def test_build_multipart_attaches_sample_files_and_json_parts():
    schema = {"properties": {"file": {"type": "string", "format": "binary"},
                             "metadata": {"type": "object", "properties": {"isPublic": {"type": "boolean"}}}}}
    parts, recorded = build_multipart(schema, {"metadata": {"isPublic": True}})
    names = {name: part for name, part in parts}
    filename, content, ctype = names["file"]
    assert (filename, ctype) == ("sample.png", "image/png") and content.startswith(b"\x89PNG")
    assert names["metadata"] == (None, b'{"isPublic": true}', "application/json")  # Spring @RequestPart
    assert recorded["file"].startswith("<file sample.png")
    # null leaves the file out; a name picks the type
    parts, recorded = build_multipart(schema, {"file": None})
    assert parts == [] and recorded == {"file": None}
    parts, _ = build_multipart(schema, {"file": "notes.txt"})
    assert parts[0][1][0] == "notes.txt" and parts[0][1][2] == "text/plain"


def test_upload_reaches_the_server_as_real_multipart(tmp_path):
    spec = parse_spec(UPLOAD_SPEC)
    cfg = upload_config(tmp_path)
    sc = Scenario(id="s", title="t", steps=[Step(operation_id="uploadFile", identity="alice", body={"caption": "hi"})])

    async def go():
        async with upload_client() as client:
            exchanges, _ = await Executor(cfg, spec, Policy(cfg, spec), client).run(sc)
            return exchanges

    [ex] = run(go())
    assert ex.status == 201
    assert ex.response_body["name"] == "sample.png" and ex.response_body["type"] == "image/png"
    assert ex.response_body["caption"] == "hi" and ex.response_body["size"] > 50


# --- Oracles ------------------------------------------------------------------------


def _ex(**kw):
    base = dict(step_index=0, operation_id="uploadFile", identity="alice", method="POST", url="http://files.test/files")
    return Exchange(**{**base, **kw})


def test_missing_required_file_accepted_is_a_constraint_finding():
    spec = parse_spec(UPLOAD_SPEC)
    accepted = _ex(request_body={"file": None}, status=201,
                   response_body={"id": 1, "owner": "alice", "name": "x", "size": 0})
    assert [v.key for v in check_exchange(spec, accepted)] == ["required:file"]
    normal = _ex(request_body={"file": "<file sample.png, image/png, 77 bytes>"}, status=201,
                 response_body={"id": 1, "owner": "alice", "name": "x", "size": 0})
    assert check_exchange(spec, normal) == []


def test_error_leak_is_reported_with_what_leaked():
    spec = parse_spec(UPLOAD_SPEC)
    leaky = _ex(request_body={"file": None}, status=500,
                response_body={"error": "JDBC exception executing SQL [INSERT INTO files (owner) VALUES (?)]"})
    found = {v.oracle: v for v in check_exchange(spec, leaky)}
    assert set(found) == {"builtin.server_error", "builtin.error_leak"}
    assert found["builtin.error_leak"].key == "sql"
    assert "INSERT INTO files" in found["builtin.error_leak"].message


# --- End to end: the rule-based planner finds both planted bugs ----------------------


def test_cross_user_access_to_an_uploaded_file_is_found(tmp_path):
    FILES.clear()
    spec = parse_spec(UPLOAD_SPEC)

    async def go():
        async with upload_client() as client:
            return await Autopilot(upload_config(tmp_path), spec, HeuristicPlanner(), client).run()

    result = run(go())
    confirmed = {(f.oracle, f.operation_id) for f in result.confirmed}
    assert ("builtin.auth_differential", "getFile") in confirmed  # bob read alice's upload
    assert ("builtin.error_leak", "uploadFile") in confirmed  # missing file -> SQL in the 500
    assert ("builtin.server_error", "uploadFile") in confirmed
