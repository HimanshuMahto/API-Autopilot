"""Command-line interface: init, inspect, run, replay, demo."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import CONFIG_FILE, DEMO_CONFIG, PROVIDERS, Config, ConfigError, load_config
from .spec import ApiSpec, SpecError, load_spec

app = typer.Typer(
    help="API Autopilot: an AI agent that explores your API and turns confirmed bugs into replayable tests.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to autopilot.yaml.")]


def _fail(exc: Exception, code: int = 2) -> typer.Exit:
    """Print an error (escaped: app logs can contain [brackets]) and return Exit."""
    err.print("[red]Error:[/red]", escape(str(exc)))
    return typer.Exit(code)


def _load_config(config_path: Path) -> Config:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        raise _fail(exc) from None


def _load_spec(config: Config) -> ApiSpec:
    try:
        return load_spec(config.spec_source)
    except SpecError as exc:
        raise _fail(exc) from None


def _load(config_path: Path) -> tuple[Config, ApiSpec]:
    config = _load_config(config_path)
    return config, _load_spec(config)


def _say(msg: str) -> None:
    console.print(msg, markup=False, highlight=False)


def _nice_path(target: Path) -> str:
    """A path relative to here, unless that means climbing far up; then absolute."""
    import os

    rel = os.path.relpath(Path(target).resolve(), Path.cwd())
    return str(Path(target).resolve()) if rel.startswith(os.pardir + os.sep + os.pardir + os.sep) else rel


def _save_spec_copy(text: str, force: bool) -> str:
    """Save a downloaded or exported spec next to the config, so runs use a fixed version."""
    local = Path("openapi.json" if text.lstrip().startswith("{") else "openapi.yaml")
    if local.exists() and not force:
        raise _fail(f"{local} already exists. Use --force to overwrite it.", 1)
    local.write_text(text)
    return str(local)


@app.command()
def init(
    spec: Annotated[
        str | None,
        typer.Option(help="Your API's OpenAPI 3.x or Swagger 2.0 spec: a file path or URL."),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(help="Where your running app is, e.g. http://localhost:8000. Its spec is found automatically."),
    ] = None,
    project: Annotated[
        Path | None,
        typer.Option(help="Your project folder. Finds a spec file in it, or exports one (FastAPI, Django REST)."),
    ] = None,
    start: Annotated[
        str | None,
        typer.Option(help="Command that starts your app. Saved in the config so runs start and stop it."),
    ] = None,
    base_url: Annotated[
        str | None, typer.Option(help="Where to send requests, if not --url or the spec's server.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Create autopilot.yaml: for the demo, or for your API with --spec, --url or --project."""
    import os
    from urllib.parse import urljoin

    from .app_runner import AppStartError, RunningApp
    from .config import StartConfig, real_api_config
    from .discover import discover_spec
    from .spec import fetch_spec_text, is_url, parse_spec_text

    path = Path(CONFIG_FILE)
    if path.exists() and not force:
        raise _fail(f"{path} already exists. Use --force to overwrite it.", 1)
    if spec and url:
        raise _fail("Use either --spec or --url, not both.")
    if spec is None and url is None and project is None:
        if start:
            raise _fail("--start needs --url or --project, so Autopilot knows where the app will answer.")
        path.write_text(DEMO_CONFIG)
        console.print(
            f"Wrote {path} for the demo API. For your own API use --spec <file-or-url>, "
            "--url <running app> or --project <folder>."
        )
        return
    if url is not None and not is_url(url):
        raise _fail(f"--url must start with http:// or https:// (got {url!r})")

    start_cwd = "."
    if project is not None:
        if not project.is_dir():
            raise _fail(f"Project folder not found: {project}")
        start_cwd = _nice_path(project)
    parsed = None
    spec_ref = ""
    source = ""  # where the spec came from, for messages
    assumed_base = None  # a framework's usual local address, used if nothing better is known

    try:
        if spec is not None:
            if is_url(spec):
                text = fetch_spec_text(spec)
                parsed = parse_spec_text(text, spec)
                spec_ref = _save_spec_copy(text, force)
                source = f"downloaded from {spec} to {spec_ref}"
            else:
                if not Path(spec).is_file():
                    raise _fail(f"spec file not found: {spec}")
                parsed = parse_spec_text(Path(spec).read_text(), spec)
                spec_ref, source = spec, spec
        elif project is not None:
            from .project import scan_project

            console.print(f"Looking for an API spec in {project} ...")
            scan = scan_project(project)
            for fw in scan.frameworks:
                console.print(f"  Detected {fw.name} ({fw.evidence})")
            if scan.spec_files:
                found_path, parsed = scan.spec_files[0]
                spec_ref = _nice_path(found_path)
                source = f"found {spec_ref}"
                for other, _ in scan.spec_files[1:4]:
                    console.print(f"  Also found {_nice_path(other)} (pass it with --spec to use it)")
            elif scan.exported:
                text, parsed, how = scan.exported
                spec_ref = _save_spec_copy(text, force)
                source = f"{how}, saved to {spec_ref}"
            else:
                for problem in scan.export_errors:
                    console.print(f"  Could not export: {escape(problem)}")
                runtime = scan.runtime_frameworks
                if runtime and (start or url):
                    fw = runtime[0]
                    url = url or f"http://localhost:{fw.port}"
                else:
                    if runtime:
                        fw = runtime[0]
                        console.print(
                            f"\n{fw.name} only produces its spec while the app is running. Either start it "
                            f"yourself and run:\n  autopilot init --url http://localhost:{fw.port}\n"
                            f"or let Autopilot start it (and keep starting it for every run):\n"
                            f"  autopilot init --project {project} --start {_quote(fw.start_hint or '')}"
                        )
                    else:
                        console.print(
                            "\nNo spec file found, and no framework that Autopilot can export a spec from. "
                            "Pass one with --spec, or point --url at the running app."
                        )
                    raise typer.Exit(2)
            if parsed is not None and scan.frameworks and scan.frameworks[0].port:
                assumed_base = f"http://localhost:{scan.frameworks[0].port}"

        if parsed is None:  # --url, possibly after --project fell through to the running app
            assert url is not None
            if start:
                # The first start may include a full build (Maven, Gradle, npm), so allow 5 minutes.
                runner = RunningApp(StartConfig(command=start, cwd=start_cwd, timeout_s=300), url, Path.cwd(),
                                    Path(".autopilot") / "app.log", on_event=_say)
                with runner:
                    found = discover_spec(url)
            else:
                found = discover_spec(url)
            parsed = found.spec
            spec_ref = _save_spec_copy(found.text, force)
            source = f"found at {found.url}, saved to {spec_ref}"
    except AppStartError as exc:
        raise _fail(exc) from None
    except SpecError as exc:
        raise _fail(exc) from None

    # Where requests go: explicit flag, then the running app, then the spec, then a guess.
    if base_url is None and url is not None:
        base_url = url
    if base_url is None and parsed.servers:
        server = parsed.servers[0]
        if "{" not in server:  # skip templated servers like https://{env}.example.com
            candidate = urljoin(spec, server) if spec and is_url(spec) else server
            if is_url(candidate):
                base_url = candidate
    guessed = False
    if base_url is None and assumed_base:
        base_url, guessed = assumed_base, True
    if base_url is None:
        raise _fail("the spec doesn't name a full server URL. Pass one, e.g. --base-url https://staging.example.com")
    base_url = base_url.rstrip("/")

    auth = parsed.auth
    header, prefix = (auth.header, auth.prefix) if auth else ("Authorization", "Bearer ")
    note = auth.description if auth else "none found; assuming a bearer token"
    text, env_vars = real_api_config(spec_ref, base_url, header, prefix, note, start, parsed.login)
    if start and start_cwd != ".":
        text = text.replace("    timeout_s:", f"    cwd: {start_cwd}\n    timeout_s:", 1)
    path.write_text(text)

    kind = "Swagger 2.0" if parsed.is_swagger else f"OpenAPI {parsed.openapi}"
    console.print(f"\nWrote {path}: {escape(parsed.title)} ({kind}), {len(parsed.operations)} operations.")
    console.print(f"  Spec:   {escape(source)}")
    console.print(f"  Target: {base_url}  (read-only to start)" + ("  [yellow]assumed; edit if wrong[/yellow]" if guessed else ""))
    if start:
        console.print(f"  Start:  {escape(start)}")
    console.print(f"  Auth:   {escape(note)}")
    if parsed.login:
        console.print(f"  Login:  {escape(parsed.login.description)}")
    for item in parsed.notes:
        console.print(f"  [yellow]Note:[/yellow] {escape(item)}")
    console.print(
        f"\nNext:\n  1. Put {', '.join(env_vars)} and an AI key ({AI_KEYS_HINT})\n"
        "     in a .env file in this folder. No AI key? Add `--planner heuristic` to runs.\n"
        "  2. autopilot inspect                 # check what it understood\n"
        "  3. autopilot run --max-requests 50   # small first run"
    )


def _quote(command: str) -> str:
    import shlex

    return shlex.quote(command)


@app.command()
def inspect(config_path: ConfigOpt = Path(CONFIG_FILE)) -> None:
    """Show what Autopilot understood from the spec."""
    config, spec = _load(config_path)
    kind = "Swagger 2.0" if spec.is_swagger else f"OpenAPI {spec.openapi}"
    console.print(f"[bold]{spec.title}[/bold] {spec.version} · {kind} · {len(spec.operations)} operations")
    table = Table(show_lines=False)
    for col in ("Operation", "Method", "Path", "Auth", "Body", "Allowed"):
        table.add_column(col)
    from .policy import Policy

    policy = Policy(config, spec)
    for op in spec.operations.values():
        allowed = (
            policy.allow_deletes if op.method == "DELETE" else policy.allow_writes if op.is_write else True
        )
        table.add_row(
            op.operation_id,
            op.method,
            op.path,
            "yes" if op.requires_auth else "-",
            "yes" if op.request_schema else "-",
            "[green]yes[/green]" if allowed else "[yellow]no[/yellow]",
        )
    console.print(table)
    console.print(f"Identities: {', '.join(config.identity_names)}")
    for name, ident in config.identities.items():
        how = (f"logs in with {ident.login.method} {ident.login.path}" if ident.login
               else f"token from ${ident.value_env}" if ident.value_env else "token in the config")
        console.print(f"  {name}: {escape(how)}")
    if config.target.start:
        console.print(f"Starts the app with: {escape(config.target.start.command)}")
    if spec.auth:
        console.print(f"Auth in spec: {spec.auth.description}")
    for item in spec.notes:
        console.print(f"[yellow]Note:[/yellow] {item}")
    if config.target.looks_like_production:
        console.print("[yellow]Target looks like production: forced read-only.[/yellow]")


AI_KEYS_HINT = "OPENAI_API_KEY, GEMINI_API_KEY, GROQ_API_KEY or ANTHROPIC_API_KEY"


def _make_planner(config: Config, choice: str | None):
    """Build the planner. Only the chosen provider's SDK is imported."""
    import os

    from .llm.base import LLMError

    provider = config.llm.resolve(choice)
    if provider is None:
        err.print(
            "[red]No AI key found.[/red] Put one of "
            f"{AI_KEYS_HINT} in a .env file next to autopilot.yaml (or in your shell), "
            "or run without AI: `--planner heuristic`.",
            markup=True,
        )
        raise typer.Exit(2)
    if provider == "heuristic":
        from .llm.heuristic import HeuristicPlanner

        return HeuristicPlanner()
    model = config.llm.model_for(provider)
    key_env = config.llm.key_env_for(provider)
    if (choice or config.llm.provider) == "auto":
        console.print(f"Using {provider} ({model}): found {key_env}.")
    try:
        if provider == "groq":
            from .llm.groq_planner import GroqPlanner

            return GroqPlanner(model, config.llm.effort, config.budgets.max_llm_calls, key_env)
        if provider in ("openai", "gemini"):
            from .llm.openai_compat import OpenAICompatPlanner

            return OpenAICompatPlanner(
                provider, model, config.llm.effort, config.budgets.max_llm_calls, key_env,
                base_url=config.llm.base_url_for(provider),
            )
        import anthropic

        from .llm.claude import ClaudePlanner

        try:
            key = os.environ.get(key_env)
            client = anthropic.AsyncAnthropic(api_key=key) if key else None
            return ClaudePlanner(model, config.llm.effort, config.budgets.max_llm_calls, client=client)
        except anthropic.AnthropicError as exc:
            raise LLMError(f"{exc}. Set {key_env} or use `--planner heuristic`.") from None
    except LLMError as exc:
        err.print(f"[red]Could not set up the {provider} planner:[/red] {exc}", markup=True)
        raise typer.Exit(2) from None


@app.command()
def run(
    config_path: ConfigOpt = Path(CONFIG_FILE),
    planner: Annotated[
        str | None, typer.Option(help="auto, openai, gemini, groq, claude or heuristic (no AI). Defaults to llm.provider in the config.")
    ] = None,
    max_requests: Annotated[int | None, typer.Option(help="Override budgets.max_requests.")] = None,
) -> None:
    """Explore the API, confirm defects, save them for replay and write a report."""
    from .agent import Autopilot
    from .replay import save_scenarios
    from .report import write_reports
    from .store import previous_fingerprints, save_run

    config = _load_config(config_path)
    if planner is not None and planner not in PROVIDERS:
        err.print(f"--planner must be one of: {', '.join(PROVIDERS)}")
        raise typer.Exit(2)
    if max_requests is not None:
        config.budgets.max_requests = max_requests
    chosen = _make_planner(config, planner)
    from .app_runner import app_for
    from .auth import log_in

    async def go(spec: ApiSpec):
        async with httpx.AsyncClient(follow_redirects=False) as client:
            await log_in(config, client, on_event=_say)
            # Literal output: "[201 200]" must not be parsed as Rich markup.
            return await Autopilot(config, spec, chosen, client, on_event=_say).run()

    try:
        with app_for(config, on_event=_say):
            spec = _load_spec(config)  # after starting: the spec may be served by the app
            result = asyncio.run(go(spec))
    except ConfigError as exc:
        raise _fail(exc) from None

    if not result.results and result.stats.stop_reason.startswith("planner error"):
        err.print("[red]Error:[/red] the planner failed before any scenario ran (see above).", markup=True)
        raise typer.Exit(2)

    db = config.state_dir / "autopilot.db"
    previous = previous_fingerprints(db, result.run_id)
    save_run(db, result)
    saved = save_scenarios(result.findings, config.scenarios_dir)
    html_path, _ = write_reports(result, config.state_dir / "reports", previous)

    s = result.stats
    console.print()
    console.print(
        f"[bold]{len(result.confirmed)} confirmed[/bold] finding(s) from {s.scenarios} scenarios, "
        f"{s.requests} requests, {s.llm_calls} LLM calls"
        + (f", ${s.cost_usd:.4f}" if s.cost_usd else "")
        + f". Stopped because: {s.stop_reason}."
    )
    for f in result.findings:
        colour = {"critical": "red", "high": "red", "medium": "yellow", "low": "white"}[f.severity]
        console.print(f"  [{colour}]{f.severity:8}[/{colour}] {f.status.value:14} {f.title}")
    if saved:
        console.print(f"Saved {len(saved)} scenario(s) to {config.scenarios_dir} for `autopilot replay`.")
    console.print(f"Report: {html_path}")


@app.command()
def replay(
    config_path: ConfigOpt = Path(CONFIG_FILE),
    include_flaky: Annotated[bool, typer.Option(help="Fail on flaky scenarios too.")] = False,
) -> None:
    """Re-run saved scenarios without any LLM. Exits 1 if a defect is still present (for CI)."""
    from .replay import load_scenarios
    from .replay import replay as run_replay

    from .app_runner import app_for
    from .auth import log_in

    config = _load_config(config_path)
    frozen = load_scenarios(config.scenarios_dir)
    if not frozen:
        console.print(f"No saved scenarios in {config.scenarios_dir}. Run `autopilot run` first.")
        return

    async def go(spec: ApiSpec):
        async with httpx.AsyncClient(follow_redirects=False) as client:
            await log_in(config, client, on_event=_say)
            return await run_replay(config, spec, client, frozen)

    try:
        with app_for(config, on_event=_say):
            spec = _load_spec(config)
            results = asyncio.run(go(spec))
    except ConfigError as exc:
        raise _fail(exc) from None

    style = {"still_failing": "red", "fixed": "green", "could_not_run": "yellow"}
    for r in results:
        flaky = " (flaky)" if r.frozen.flaky else ""
        console.print(f"[{style[r.outcome]}]{r.outcome:14}[/{style[r.outcome]}] {r.frozen.title}{flaky}  [dim]{r.frozen.fingerprint}[/dim]")
        if r.outcome != "fixed":
            console.print(f"               [dim]{r.detail}[/dim]")
    failing = [r for r in results if r.outcome == "still_failing" and (include_flaky or not r.frozen.flaky)]
    broken = [r for r in results if r.outcome == "could_not_run"]
    console.print(
        f"{sum(r.outcome == 'fixed' for r in results)} fixed, {len(failing)} still failing, "
        f"{len(broken)} could not run."
    )
    if failing:
        raise typer.Exit(1)
    if broken:
        raise typer.Exit(2)


@app.command()
def demo(port: Annotated[int, typer.Option(help="Port to listen on.")] = 8765) -> None:
    """Serve the bundled demo shop API (with planted bugs) on 127.0.0.1. Run from the repo root."""
    try:
        import uvicorn

        sys.path.insert(0, str(Path.cwd()))
        from demo.shop_api import app as shop_app
    except ImportError as exc:
        err.print(
            f"[red]Could not start the demo:[/red] {exc}\n"
            "Run this from the api-autopilot repo root with the demo extra installed "
            "(`uv sync` includes it)."
        )
        raise typer.Exit(2) from None
    console.print(f"Demo shop API on http://127.0.0.1:{port} (Ctrl+C to stop)")
    uvicorn.run(shop_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
