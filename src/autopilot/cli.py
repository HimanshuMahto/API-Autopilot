"""Command-line interface: init, inspect, run, replay, demo."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .config import CONFIG_FILE, DEMO_CONFIG, Config, ConfigError, load_config
from .spec import ApiSpec, SpecError, load_spec

app = typer.Typer(
    help="API Autopilot: an AI agent that explores your API and turns confirmed bugs into replayable tests.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err = Console(stderr=True)

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to autopilot.yaml.")]


def _load(config_path: Path) -> tuple[Config, ApiSpec]:
    try:
        config = load_config(config_path)
        return config, load_spec(config.spec_source)
    except (ConfigError, SpecError) as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from None


@app.command()
def init(
    spec: Annotated[
        str | None,
        typer.Option(help="Your API's OpenAPI 3.x or Swagger 2.0 spec: a file path or URL. Omit for the demo."),
    ] = None,
    base_url: Annotated[
        str | None, typer.Option(help="Where to send requests. Defaults to the server listed in the spec.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Overwrite existing files.")] = False,
) -> None:
    """Create autopilot.yaml: for the bundled demo, or for your own API with --spec."""
    from urllib.parse import urljoin

    from .config import real_api_config
    from .spec import fetch_spec_text, is_url, parse_spec_text

    path = Path(CONFIG_FILE)
    if path.exists() and not force:
        err.print(f"{path} already exists. Use --force to overwrite it.")
        raise typer.Exit(1)
    if spec is None:
        path.write_text(DEMO_CONFIG)
        console.print(f"Wrote {path} for the demo API. Use `autopilot init --spec <file-or-url>` for your own API.")
        return

    try:
        if is_url(spec):
            text = fetch_spec_text(spec)
            parsed = parse_spec_text(text, spec)
            # Save a copy next to the config, so runs use a fixed version of the spec.
            local = Path("openapi.json" if text.lstrip().startswith("{") else "openapi.yaml")
            if local.exists() and not force:
                err.print(f"{local} already exists. Use --force to overwrite it.")
                raise typer.Exit(1)
            local.write_text(text)
            console.print(f"Downloaded the spec to {local}.")
            spec_ref = str(local)
        else:
            if not Path(spec).is_file():
                err.print(f"[red]Error:[/red] spec file not found: {spec}", markup=True)
                raise typer.Exit(2)
            parsed = parse_spec_text(Path(spec).read_text(), spec)
            spec_ref = spec
    except SpecError as exc:
        err.print(f"[red]Error:[/red] {exc}", markup=True)
        raise typer.Exit(2) from None

    if base_url is None and parsed.servers:
        server = parsed.servers[0]
        if "{" not in server:  # skip templated servers like https://{env}.example.com
            # A relative server ("/api/v1") is relative to where the spec was downloaded from.
            candidate = urljoin(spec, server) if is_url(spec) else server
            if is_url(candidate):
                base_url = candidate
    if base_url is None:
        err.print(
            "[red]Error:[/red] the spec doesn't name a full server URL. "
            "Pass one, e.g. --base-url https://staging.example.com",
            markup=True,
        )
        raise typer.Exit(2)

    auth = parsed.auth
    header, prefix = (auth.header, auth.prefix) if auth else ("Authorization", "Bearer ")
    note = auth.description if auth else "none found; assuming a bearer token"
    path.write_text(real_api_config(spec_ref, base_url.rstrip("/"), header, prefix, note))

    kind = "Swagger 2.0" if parsed.is_swagger else f"OpenAPI {parsed.openapi}"
    console.print(f"Wrote {path}: {parsed.title} ({kind}), {len(parsed.operations)} operations.")
    console.print(f"  Target: {base_url.rstrip('/')}  (read-only to start)")
    console.print(f"  Auth:   {note}")
    for item in parsed.notes:
        console.print(f"  [yellow]Note:[/yellow] {item}", markup=True)
    console.print(
        "\nNext:\n"
        "  1. Put GROQ_API_KEY, USER_A_TOKEN and USER_B_TOKEN in a .env file in this folder.\n"
        "  2. autopilot inspect                 # check what it understood\n"
        "  3. autopilot run --max-requests 50   # small first run"
    )


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
    if spec.auth:
        console.print(f"Auth in spec: {spec.auth.description}")
    for item in spec.notes:
        console.print(f"[yellow]Note:[/yellow] {item}")
    if config.target.looks_like_production:
        console.print("[yellow]Target looks like production: forced read-only.[/yellow]")


def _make_planner(config: Config, choice: str | None):
    """Build the planner. Only the chosen provider's SDK is imported."""
    from .llm.base import LLMError

    provider = choice or config.llm.provider
    if provider == "heuristic":
        from .llm.heuristic import HeuristicPlanner

        return HeuristicPlanner()
    model = config.llm.model_for(provider)
    key_env = config.llm.key_env_for(provider)
    try:
        if provider == "groq":
            from .llm.groq_planner import GroqPlanner

            return GroqPlanner(model, config.llm.effort, config.budgets.max_llm_calls, key_env)
        import anthropic

        from .llm.claude import ClaudePlanner

        try:
            return ClaudePlanner(model, config.llm.effort, config.budgets.max_llm_calls)
        except anthropic.AnthropicError as exc:
            raise LLMError(f"{exc}. Set {key_env} or use `--planner heuristic`.") from None
    except LLMError as exc:
        err.print(f"[red]Could not set up the {provider} planner:[/red] {exc}", markup=True)
        raise typer.Exit(2) from None


@app.command()
def run(
    config_path: ConfigOpt = Path(CONFIG_FILE),
    planner: Annotated[
        str | None, typer.Option(help="groq, claude or heuristic. Defaults to llm.provider in the config.")
    ] = None,
    max_requests: Annotated[int | None, typer.Option(help="Override budgets.max_requests.")] = None,
) -> None:
    """Explore the API, confirm defects, save them for replay and write a report."""
    from .agent import Autopilot
    from .replay import save_scenarios
    from .report import write_reports
    from .store import previous_fingerprints, save_run

    config, spec = _load(config_path)
    if planner not in (None, "groq", "claude", "heuristic"):
        err.print("--planner must be groq, claude or heuristic")
        raise typer.Exit(2)
    if max_requests is not None:
        config.budgets.max_requests = max_requests
    chosen = _make_planner(config, planner)

    async def go():
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await Autopilot(
                config, spec, chosen, client,
                # Literal output: "[201 200]" must not be parsed as Rich markup.
                on_event=lambda msg: console.print(msg, markup=False, highlight=False),
            ).run()

    try:
        result = asyncio.run(go())
    except ConfigError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from None

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

    config, spec = _load(config_path)
    frozen = load_scenarios(config.scenarios_dir)
    if not frozen:
        console.print(f"No saved scenarios in {config.scenarios_dir}. Run `autopilot run` first.")
        return

    async def go():
        async with httpx.AsyncClient(follow_redirects=False) as client:
            return await run_replay(config, spec, client, frozen)

    try:
        results = asyncio.run(go())
    except ConfigError as exc:
        err.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from None

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
