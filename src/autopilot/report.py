"""Static HTML and Markdown run reports (the MVP's only UI)."""

from __future__ import annotations

import json
import shlex
from html import escape
from pathlib import Path
from typing import Any

from .agent import RunResult
from .models import Exchange, Finding, FindingStatus

MAX_EVIDENCE_CHARS = 1500


def _json(value: Any) -> str:
    text = json.dumps(value, indent=2, default=str) if not isinstance(value, str) else value
    return text if len(text) <= MAX_EVIDENCE_CHARS else text[:MAX_EVIDENCE_CHARS] + "\n… (truncated)"


def _request_line(ex: Exchange) -> str:
    return f"{ex.method} {ex.url}  as {ex.identity}"


def _curl(ex: Exchange) -> str:
    parts = [f"curl -X {ex.method} {shlex.quote(ex.url)}"]
    if ex.identity != "anonymous":
        parts.append(f"-H 'Authorization: <{ex.identity} token>'")
    if ex.request_body is not None:
        parts.append("-H 'Content-Type: application/json'")
        parts.append(f"-d {shlex.quote(json.dumps(ex.request_body))}")
    return " \\\n  ".join(parts)


CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e6e4df;--code:#f4f3f0;
--critical:#b42318;--high:#c4320a;--medium:#b54708;--low:#475467;--ok:#067647;--warn:#b54708}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe8;--muted:#a3a29c;--card:#1f1f1d;
--line:#33322f;--code:#272725;--critical:#f97066;--high:#fd853a;--medium:#fdb022;--low:#98a2b3;--ok:#47cd89;--warn:#fdb022}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px}
.muted{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:20px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat b{display:block;font-size:22px}.card{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:18px;margin:14px 0}.card h3{margin:0 0 8px;font-size:16px}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;
border:1px solid currentColor;margin-right:6px}.critical{color:var(--critical)}.high{color:var(--high)}
.medium{color:var(--medium)}.low{color:var(--low)}.confirmed{color:var(--ok)}.flaky{color:var(--warn)}
.unconfirmed{color:var(--warn)}.not_reproduced{color:var(--muted)}pre{background:var(--code);border-radius:8px;padding:10px;
overflow-x:auto;font-size:12.5px;margin:6px 0}details{margin:8px 0}summary{cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:13.5px}td,th{text-align:left;padding:6px 8px;
border-bottom:1px solid var(--line);vertical-align:top}dl{display:grid;grid-template-columns:max-content 1fr;
gap:4px 14px;margin:10px 0}dt{color:var(--muted)}dd{margin:0}
"""


def _finding_html(f: Finding, new: bool | None) -> str:
    steps = "".join(
        f"<details><summary>Step {ex.step_index + 1}: <code>{escape(_request_line(ex))}</code> → "
        f"<b>{ex.status if ex.status is not None else escape(ex.error or 'blocked')}</b></summary>"
        + (f"<div class='muted'>Request body</div><pre>{escape(_json(ex.request_body))}</pre>" if ex.request_body is not None else "")
        + f"<div class='muted'>Response body</div><pre>{escape(_json(ex.response_body))}</pre>"
        + f"<div class='muted'>curl</div><pre>{escape(_curl(ex))}</pre></details>"
        for ex in f.evidence
    )
    tag = "" if new is None else ("<span class='badge high'>new</span>" if new else "<span class='badge low'>seen before</span>")
    return f"""
<div class="card">
  <h3>{escape(f.title)}</h3>
  <span class="badge {f.severity}">{f.severity}</span><span class="badge {f.status}">{f.status.value.replace('_', ' ')} {f.reproduced}/{f.attempts}</span>{tag}
  <p>{escape(f.explanation or f.message)}</p>
  <dl>
    <dt>What broke</dt><dd>{escape(f.message)}</dd>
    <dt>Oracle</dt><dd><code>{escape(f.oracle)}</code></dd>
    <dt>Expected behaviour from</dt><dd>{escape(str(f.source).replace('_', ' '))}</dd>
    <dt>Probably wrong</dt><dd>{escape(f.likely_wrong)}</dd>
    <dt>Fingerprint</dt><dd><code>{f.fingerprint}</code></dd>
  </dl>
  <div class="muted">Reproduction ({len(f.evidence)} step{'s' if len(f.evidence) != 1 else ''})</div>
  {steps}
</div>"""


def render_html(run: RunResult, previous: set[str] | None = None) -> str:
    s = run.stats
    real = [f for f in run.findings if f.status != FindingStatus.NOT_REPRODUCED]
    dropped = [f for f in run.findings if f.status == FindingStatus.NOT_REPRODUCED]
    cards = "".join(_finding_html(f, None if previous is None else f.fingerprint not in previous) for f in real)
    if not cards:
        cards = "<p class='muted'>No defects were confirmed in this run.</p>"
    scenario_rows = "".join(
        f"<tr><td>{escape(r.scenario.title)}</td><td>{' '.join(str(e.status) if e.status else 'blocked' for e in r.exchanges)}</td>"
        f"<td>{escape(', '.join(sorted({v.oracle for v in r.violations})) or '—')}</td></tr>"
        for r in run.results
    )
    notes = "".join(f"<li>{escape(n)}</li>" for n in run.notes)
    dropped_html = "".join(f"<li>{escape(f.title)}: {escape(f.message)}</li>" for f in dropped)
    cost = f"${s.cost_usd:.4f}" if s.llm_calls and s.cost_usd else "—"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autopilot run {run.run_id}</title><style>{CSS}</style></head>
<body><main>
<h1>API Autopilot report</h1>
<div class="muted">Run <code>{run.run_id}</code> against <code>{escape(run.target)}</code> · {escape(run.started_at)} → {escape(run.finished_at)} · planner: {escape(s.planner)}</div>
<div class="stats">
  <div class="stat"><b>{len(run.confirmed)}</b>confirmed</div>
  <div class="stat"><b>{sum(f.status == FindingStatus.FLAKY for f in run.findings)}</b>flaky</div>
  <div class="stat"><b>{s.scenarios}</b>scenarios</div>
  <div class="stat"><b>{s.requests}</b>requests</div>
  <div class="stat"><b>{s.llm_calls}</b>LLM calls</div>
  <div class="stat"><b>{cost}</b>LLM cost</div>
</div>
<div class="muted">Stopped because: {escape(s.stop_reason)}</div>
<h2>Findings</h2>{cards}
{f"<h2>Not reproduced</h2><ul>{dropped_html}</ul>" if dropped else ""}
<h2>Everything that ran</h2>
<details><summary>{len(run.results)} scenarios</summary><table><tr><th>Scenario</th><th>Statuses</th><th>Violations</th></tr>{scenario_rows}</table></details>
{f"<h2>Notes</h2><ul>{notes}</ul>" if notes else ""}
<p class="muted">Confirmed findings are saved in <code>.autopilot/scenarios/</code>. Run <code>autopilot replay</code> to re-check them without an LLM.</p>
</main></body></html>"""


def render_markdown(run: RunResult) -> str:
    s = run.stats
    lines = [
        f"# API Autopilot report: run {run.run_id}",
        "",
        f"Target `{run.target}` · planner `{s.planner}` · {s.scenarios} scenarios · {s.requests} requests · "
        f"{s.llm_calls} LLM calls · stopped because: {s.stop_reason}",
        "",
    ]
    for f in run.findings:
        if f.status == FindingStatus.NOT_REPRODUCED:
            continue
        lines += [
            f"## [{f.severity}] {f.title} ({f.status.value} {f.reproduced}/{f.attempts})",
            "",
            f.explanation or f.message,
            "",
            f"- What broke: {f.message}",
            f"- Oracle: `{f.oracle}` (expected behaviour from: {f.source})",
            f"- Probably wrong: {f.likely_wrong}",
            f"- Fingerprint: `{f.fingerprint}`",
            "",
            "Reproduction:",
            "",
        ]
        for ex in f.evidence:
            lines.append(f"{ex.step_index + 1}. `{_request_line(ex)}` → {ex.status}")
        lines.append("")
    if not run.confirmed:
        lines.append("No defects were confirmed in this run.")
    return "\n".join(lines) + "\n"


def write_reports(run: RunResult, directory: Path, previous: set[str] | None = None) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    html_path = directory / f"run-{run.run_id}.html"
    md_path = directory / f"run-{run.run_id}.md"
    html_path.write_text(render_html(run, previous))
    md_path.write_text(render_markdown(run))
    return html_path, md_path
