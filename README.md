# API Autopilot

An AI agent that tests your API like a curious senior QA engineer. It reads your
OpenAPI spec, decides what might break, tries it safely, follows up on anything
odd, and hands back **confirmed** bugs as scenarios you can replay in CI without
any AI.

> Status: weekend MVP (PRD §14.1). CLI + static HTML report. Full product
> requirements are in [docs/PRD.md](docs/PRD.md).

## Quick start (demo)

```bash
uv sync                                   # install
uv run autopilot demo                     # terminal 1: demo shop API with 4 planted bugs
uv run autopilot run --planner heuristic  # terminal 2: explore without an API key
uv run autopilot replay                   # re-check saved bugs; exits 1 while any remain
```

To plan with an LLM, put your Groq key in a `.env` file next to `autopilot.yaml`
(it is git-ignored) and drop `--planner heuristic`:

```bash
echo 'GROQ_API_KEY=your-key-here' > .env
uv run autopilot run
```

The default model is `openai/gpt-oss-120b` on Groq, using strict structured
outputs. Claude is also supported (`--planner claude`) but never used unless you
choose it.

The report is written to `.autopilot/reports/run-<id>.html`.

## How it works

```
spec ──► planner (Groq, Claude or heuristic) ──► proposed scenarios
                                              │
                             policy gateway ◄─┘  hosts, writes, deletes, budgets
                                   │
                              executor ──► target API
                                   │
                              oracles (deterministic) ──► violations
                                   │
         follow-up round ◄─────────┤  planner sees status codes, shapes and verdicts only
                                   │
                    confirm: re-run each candidate N times from scratch
                                   │
          .autopilot/scenarios/*.yaml ──► `autopilot replay` (no LLM)
```

- **The LLM never touches the target.** It proposes scenarios; the policy
  gateway checks every step, the executor sends it, and fixed oracles decide
  whether it is a bug.
- **Confirmed means reproduced N/N times** (default 2). Partial reproduction is
  reported as flaky and does not fail CI by default.
- **Replay is LLM-free.** A test enforces that `autopilot replay` imports no LLM
  code. A finding only counts as fixed when its scenario actually re-ran clean.
- **Metadata only.** The planner sees the spec, response *shapes* and oracle
  verdicts, never response values or tokens.

### Oracles (MVP)

| Oracle | Flags |
|---|---|
| `builtin.server_error` | any 5xx |
| `builtin.schema_conformance` | response body doesn't match the documented schema |
| `builtin.undocumented_status` | status code not listed in the spec |
| `spec.constraint` | request broke a documented constraint but got 2xx |
| `builtin.auth_required` | protected operation answered an anonymous caller |
| `builtin.auth_differential` | one identity reached a resource another identity created |

## Commands

| Command | What it does |
|---|---|
| `autopilot init [--spec FILE_OR_URL] [--base-url URL]` | write `autopilot.yaml`: for the demo, or for your API with `--spec` |
| `autopilot inspect` | show operations, auth and what the policy allows |
| `autopilot run [--planner groq\|claude\|heuristic] [--max-requests N]` | explore, confirm, save scenarios, write a report |
| `autopilot replay [--include-flaky]` | re-run saved scenarios; exit 1 if a defect remains, 2 if one couldn't run |
| `autopilot demo` | serve the demo shop API on 127.0.0.1:8765 |

## Testing your own API

Autopilot works on any HTTP API described by an **OpenAPI 3.x or Swagger 2.0**
spec, in JSON or YAML.

### 1. What you need

- **The spec.** Many frameworks serve one: FastAPI at `/openapi.json`, Spring Boot
  (springdoc) at `/v3/api-docs`, NestJS usually at `/api-json`, Swagger 2.0 apps
  often at `/swagger.json` or `/v2/api-docs`. A file on disk works too.
- **A staging or local copy of the API, never production.** Autopilot creates
  data and sends invalid input on purpose. Hosts whose name contains `prod`,
  `production` or `live` are forced read-only.
- **Two test accounts** and a token for each, so it can check that one user
  can't reach the other's data.

### 2. Set it up

Run Autopilot from a folder of its own, so each API keeps its own config,
reports and saved bugs:

```bash
# Lets `autopilot` run from any folder
alias autopilot='uv run --project ~/Documents/Github-Personal/api-autopilot autopilot'

mkdir ~/autopilot-myapi && cd ~/autopilot-myapi
autopilot init --spec https://staging.yourapp.com/openapi.json
```

`init --spec` accepts a URL or a file path. It:

- downloads the spec to `openapi.json` (or `.yaml`), so every run uses the same version;
- takes the base URL from the spec's servers (Swagger: `schemes`, `host`,
  `basePath`). If the spec doesn't name a full URL, pass `--base-url https://...`;
- picks the auth header from the spec's security scheme (bearer token, API-key
  header or basic auth) and sets up two identities, `user_a` and `user_b`;
- starts **read-only** (`allow_writes: false`);
- lists anything it can't test yet, such as form uploads.

### 3. Add your keys

Put the keys in a `.env` file next to `autopilot.yaml`. Don't put them in the
config, and don't paste them into chats or tickets.

```bash
GROQ_API_KEY=...
USER_A_TOKEN=...
USER_B_TOKEN=...
```

`.env` is git-ignored. Variables already set in your shell take priority.

### 4. Check, then run small

```bash
autopilot inspect                    # operations, auth, what the policy allows
autopilot run --max-requests 50      # small first run
open "$(ls -t .autopilot/reports/*.html | head -1)"
```

Read the report. When you trust what it does, set `allow_writes: true` in
`autopilot.yaml` and run again (staging only). Use `autopilot replay` to
re-check found bugs after a fix, with no AI involved.

### Configuration reference

- `spec`: file path (relative to `autopilot.yaml`) or http(s) URL.
- `target.base_url` / `allowed_hosts`: requests to any other host are refused.
- `target.allow_writes` / `allow_deletes`: off unless you turn them on.
- `identities`: `header`, `prefix` (e.g. `"Bearer "`, or `""` for a raw API key)
  and `value_env`, the environment variable that holds the token.
- `budgets`: request, follow-up round and LLM-call limits.
- `llm`: `provider` (`groq`, `claude` or `heuristic`), `model`, `effort` and
  `api_key_env`.

Commit `.autopilot/scenarios/` if you want the found bugs as regression tests;
the database and reports are git-ignored.

### What Groq sees

The spec, plus the *shape* and status code of each response. It never sees
response values or your tokens. If the spec itself is confidential, use
`--planner heuristic`: no AI, and nothing leaves your machine except the test
requests to your own API.

## Website

The product page lives in [`website/`](website/): plain HTML and CSS, no build step.
See [website/README.md](website/README.md) for previewing and hosting it.

## Development

```bash
uv run pytest
```

Tests run the real engine against the demo app in-process, and the Groq and
Claude adapters against fake clients, so they need no network or API key.

## Known MVP limits

- No reset/seed hooks yet, so confirmation re-runs share state with earlier
  scenarios on the target (PRD §6.8, Phase 2).
- One fingerprint per schema error: if a response has several different schema
  errors, the first (by path) is the one tracked.
- No invariant DSL, exports (pytest/Hurl), CI action or MCP server yet (Phase 2–3).
- Specs: OpenAPI 3.x and Swagger 2.0, with `$ref`s inside one file only.
- JSON request bodies only; form uploads are listed by `init`/`inspect` and skipped.
- Fixed tokens: no login flow, so refresh expired tokens by hand. API keys must
  go in a header, not the query string.
- No clean-up: data created on the target stays there.
