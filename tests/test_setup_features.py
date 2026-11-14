"""Spec discovery, starting the app, logging in, and reading a project folder."""

from __future__ import annotations

import json
import socket
import sys
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from autopilot.app_runner import AppStartError, RunningApp, is_up
from autopilot.auth import LoginError, log_in
from autopilot.cli import app
from autopilot.config import Config, ConfigError, StartConfig, load_config
from autopilot.discover import DiscoveryFailed, candidate_urls, discover_spec
from autopilot.project import detect_frameworks, find_spec_files, scan_project
from autopilot.spec import load_spec, parse_spec

from .conftest import ROOT, demo_client, run

DEMO_SPEC_TEXT = (ROOT / "demo" / "openapi.yaml").read_text()
runner = CliRunner()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def demo_command(port: int) -> str:
    return f"{sys.executable} -m uvicorn demo.shop_api:app --port {port} --app-dir {ROOT} --log-level warning"


# --- 1. Finding the spec on a running app ----------------------------------------


def _serve(routes: dict[str, tuple[int, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(request.url.path, (404, "not found"))
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discovery_finds_the_first_valid_spec():
    client = _serve({"/openapi.json": (200, "<html>docs page</html>"), "/v3/api-docs": (200, DEMO_SPEC_TEXT)})
    found = discover_spec("http://app.local:8080", client=client)
    assert found.url == "http://app.local:8080/v3/api-docs"  # the HTML page was skipped
    assert found.spec.title == "Demo Shop API"


def test_discovery_also_tries_the_server_root():
    urls = candidate_urls("http://app.local/api/v1")
    assert urls[0] == "http://app.local/api/v1/openapi.json"
    assert "http://app.local/openapi.json" in urls
    client = _serve({"/swagger.json": (200, DEMO_SPEC_TEXT)})
    assert discover_spec("http://app.local/api/v1", client=client).url == "http://app.local/swagger.json"


def test_discovery_explains_what_went_wrong():
    with pytest.raises(DiscoveryFailed, match="tried .* common addresses"):
        discover_spec("http://app.local", client=_serve({}))

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(DiscoveryFailed, match="Is the app running"):
        discover_spec("http://app.local", client=httpx.Client(transport=httpx.MockTransport(down)))


# --- 2. Starting the app -----------------------------------------------------------


def test_starts_the_app_finds_its_spec_and_stops_it(tmp_path):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    assert not is_up(url)
    with RunningApp(StartConfig(command=demo_command(port), timeout_s=30), url, tmp_path, tmp_path / "app.log") as running:
        assert not running.reused and is_up(url)
        found = discover_spec(url)
        assert found.url.endswith("/openapi.yaml") and found.spec.title == "Demo Shop API"
    assert not is_up(url), "the app was not stopped"


def test_an_app_that_is_already_running_is_reused_not_restarted(tmp_path):
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    start = StartConfig(command=demo_command(port), timeout_s=30)
    with RunningApp(start, url, tmp_path, tmp_path / "outer.log"):
        with RunningApp(start, url, tmp_path, tmp_path / "inner.log") as inner:
            assert inner.reused
        assert is_up(url), "leaving the inner block must not stop an app it didn't start"
    assert not is_up(url)


def test_an_app_that_crashes_on_start_reports_its_output(tmp_path):
    command = f"{sys.executable} -c \"print('database is not configured'); raise SystemExit(3)\""
    with pytest.raises(AppStartError, match="exited with code 3") as info:
        with RunningApp(StartConfig(command=command), f"http://127.0.0.1:{free_port()}", tmp_path, tmp_path / "app.log"):
            pass
    assert "database is not configured" in str(info.value)


def test_an_app_that_never_answers_times_out_and_is_stopped(tmp_path):
    command = f"{sys.executable} -c \"import time; time.sleep(60)\""
    runner_ = RunningApp(StartConfig(command=command, timeout_s=1.5), f"http://127.0.0.1:{free_port()}", tmp_path, tmp_path / "app.log")
    with pytest.raises(AppStartError, match="didn't answer"):
        with runner_:
            pass
    assert runner_.process is None


# --- 3. Logging in -----------------------------------------------------------------


def _login_config(**login):
    return Config.model_validate(
        {
            "spec": str(ROOT / "demo" / "openapi.yaml"),
            "target": {"base_url": "http://127.0.0.1:8765"},
            "identities": {"alice": {"login": {"path": "/auth/login", **login}}},
        }
    )


def test_log_in_gets_a_token_used_for_later_requests(monkeypatch):
    monkeypatch.setenv("ALICE_USER", "alice")
    monkeypatch.setenv("ALICE_PW", "alice-password")
    cfg = _login_config(body={"username": "${ALICE_USER}", "password": "${ALICE_PW}"})
    with pytest.raises(ConfigError, match="hasn't logged in"):
        cfg.identities["alice"].header_value("alice")

    async def go():
        async with demo_client() as client:
            assert await log_in(cfg, client) == ["alice"]
            me = await client.get("http://127.0.0.1:8765/me", headers={"Authorization": cfg.identities["alice"].header_value("alice")})
            return me.status_code, me.json()

    assert run(go()) == (200, {"username": "alice"})


def test_failed_login_never_leaks_the_password(monkeypatch):
    monkeypatch.setenv("ALICE_PW", "hunter2-secret")
    cfg = _login_config(body={"username": "alice", "password": "${ALICE_PW}"})

    async def go():
        async with demo_client() as client:
            await log_in(cfg, client)

    with pytest.raises(LoginError, match="returned 401") as info:
        run(go())
    assert "hunter2-secret" not in str(info.value)


def test_missing_env_var_and_wrong_token_path_are_explained(monkeypatch):
    monkeypatch.delenv("NOPE_PW", raising=False)
    with pytest.raises(ConfigError, match=r"\$\{NOPE_PW\}"):
        run(_login_and_close(_login_config(body={"username": "alice", "password": "${NOPE_PW}"})))
    cfg = _login_config(body={"username": "alice", "password": "alice-password"}, token_path="body.jwt")
    with pytest.raises(LoginError, match="access_token, token_type"):
        run(_login_and_close(cfg))


async def _login_and_close(cfg):
    async with demo_client() as client:
        await log_in(cfg, client)


def test_form_login_for_oauth2_style_endpoints():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"access_token": "tok-123"})

    cfg = _login_config(format="form", body={"username": "alice", "password": "pw"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await log_in(cfg, client)

    run(go())
    assert seen == {"type": "application/x-www-form-urlencoded", "body": "username=alice&password=pw"}
    assert cfg.identities["alice"].header_value("alice") == "Bearer tok-123"


def test_login_endpoint_detection():
    demo = load_spec(ROOT / "demo" / "openapi.yaml")
    hint = demo.login
    assert (hint.path, hint.format, hint.username_field, hint.password_field, hint.token_path) == (
        "/auth/login", "json", "username", "password", "body.access_token"
    )
    oauth = parse_spec(
        {
            "openapi": "3.0.3",
            "components": {"securitySchemes": {"o": {"type": "oauth2", "flows": {"password": {"tokenUrl": "/token", "scopes": {}}}}}},
            "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}},
        }
    )
    assert (oauth.login.path, oauth.login.format) == ("/token", "form")
    plain = parse_spec({"openapi": "3.0.3", "paths": {"/items": {"get": {"responses": {"200": {"description": "ok"}}}}}})
    assert plain.login is None


# --- 4. Reading a project folder ---------------------------------------------------


def test_finds_spec_files_and_ignores_dependency_folders(tmp_path):
    (tmp_path / "node_modules" / "lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "lib" / "openapi.yaml").write_text(DEMO_SPEC_TEXT)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "api.yaml").write_text(DEMO_SPEC_TEXT)
    (tmp_path / "package.json").write_text('{"name": "x"}')  # JSON, but not a spec
    found = find_spec_files(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p, _ in found] == ["docs/api.yaml"]


def _fastapi_project(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "main.py").write_text(textwrap.dedent('''
        from fastapi import FastAPI

        app = FastAPI(title="Inventory")

        @app.get("/items/{item_id}")
        def read_item(item_id: int) -> dict:
            return {"id": item_id}
    '''))
    (root / "requirements.txt").write_text("fastapi\nuvicorn\n")
    return root


def test_exports_a_fastapi_spec_without_running_the_app(tmp_path):
    scan = scan_project(_fastapi_project(tmp_path))
    assert [f.name for f in scan.frameworks] == ["FastAPI"]
    assert scan.frameworks[0].start_hint == "uvicorn app.main:app --port 8000"
    text, spec, how = scan.exported
    assert spec.title == "Inventory" and "read_item_items__item_id__get" in spec.operations
    assert how == "exported from app.main:app"


def test_detects_frameworks_that_only_serve_a_spec_while_running(tmp_path):
    (tmp_path / "pom.xml").write_text("<dependency><artifactId>springdoc-openapi-starter-webmvc-ui</artifactId></dependency>")
    (tmp_path / "mvnw").write_text("")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    [spring] = detect_frameworks(tmp_path)
    assert (spring.spec_path, spring.start_hint, spring.port) == ("/v3/api-docs", "./mvnw spring-boot:run", 8080)
    nest = tmp_path / "nest"
    nest.mkdir()
    (nest / "package.json").write_text('{"dependencies": {"@nestjs/swagger": "^7"}}')
    assert [(f.name, f.spec_path) for f in detect_frameworks(nest)] == [("NestJS", "/api-json")]


# --- init, end to end --------------------------------------------------------------


def test_init_project_exports_and_writes_config(tmp_path, monkeypatch):
    project = _fastapi_project(tmp_path / "svc")
    work = tmp_path / "tests"
    work.mkdir()
    monkeypatch.chdir(work)
    result = runner.invoke(app, ["init", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert "Detected FastAPI" in result.output and "assumed" in result.output
    cfg = load_config("autopilot.yaml")
    assert cfg.spec == "openapi.json" and cfg.target.base_url == "http://localhost:8000"
    assert json.loads(Path("openapi.json").read_text())["info"]["title"] == "Inventory"


def test_init_project_explains_runtime_only_frameworks(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text("springdoc")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--project", "."])
    assert result.exit_code == 2
    assert "autopilot init --url http://localhost:8080" in result.output
    assert "--start" in result.output and not Path("autopilot.yaml").exists()


def test_init_url_with_start_then_run_logs_in_and_tests(tmp_path, monkeypatch):
    """The whole flow: init starts the app, finds its spec and login, then run uses both."""
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--url", url, "--start", demo_command(port)])
    assert result.exit_code == 0, result.output
    assert "found at" in result.output and "/openapi.yaml" in result.output
    assert "Login:  login (POST /auth/login)" in result.output
    assert not is_up(url), "init must stop the app it started"

    cfg = yaml.safe_load(Path("autopilot.yaml").read_text())
    assert cfg["target"]["start"]["command"] == demo_command(port)
    assert cfg["identities"]["user_a"]["login"]["body"] == {"username": "${USER_A_USERNAME}", "password": "${USER_A_PASSWORD}"}
    Path(".env").write_text(
        "USER_A_USERNAME=alice\nUSER_A_PASSWORD=alice-password\nUSER_B_USERNAME=bob\nUSER_B_PASSWORD=bob-password\n"
    )
    for var in ("USER_A_USERNAME", "USER_A_PASSWORD", "USER_B_USERNAME", "USER_B_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    result = runner.invoke(app, ["run", "--planner", "heuristic", "--max-requests", "40"])
    assert result.exit_code == 0, result.output
    assert "Starting the app" in result.output and "Logged in as user_a, user_b" in result.output
    assert "Stopping the app" in result.output
    assert not is_up(url)
