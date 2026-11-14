<div class="cover">

<p class="eyebrow">PRODUCT REQUIREMENTS DOCUMENT</p>

# API Autopilot

<p class="subtitle">AI-native autonomous API exploration, testing, investigation and regression intelligence</p>

| Document field | Value |
|---|---|
| Version | 1.1 |
| Status | Proposed product specification (revised) |
| Product category | Developer tools / API quality / AI agents |
| Primary users | Backend developers, QA engineers, platform engineers |
| Initial delivery target | Weekend MVP (CLI-first), then Phase 2 (1–2 weeks) and Phase 3 (2–3 weeks) |
| Core proposition | An AI agent that experiments with APIs, observes real outcomes, and adapts its testing strategy, then freezes what it learns into deterministic regression suites. |

<div class="callout"><strong>Product principle:</strong> AI is the investigator; the deterministic testing engine is its laboratory. The product must demonstrate adaptive decisions grounded in real API execution, not just call an LLM to produce test cases. <strong>AI explores; deterministic replay gates.</strong></div>

<p class="muted">Prepared: 13 November 2026 · Revised: 13 November 2026 (v1.1)</p>

</div>

## What changed in v1.1

Sections marked <span class="new">NEW</span> or <span class="upd">UPDATED</span> in the body are changes from v1.0. Summary of the improvements and why each one matters:

| # | Improvement | Why it matters | Where |
|---|---|---|---|
| 1 | **Competitive landscape and baseline.** Position against Schemathesis, RESTler, EvoMaster, Postman and API-security DAST tools, and benchmark against them. | Comparing the agent only to "fixed deterministic tests" is a weak baseline. Credibility needs a comparison with the best open-source tool. | §2.4, §13.4 |
| 2 | **Explore mode vs replay mode.** LLM-driven exploration is nondeterministic. Confirmed scenarios are frozen into LLM-free replay suites for CI. | v1.0 promised "repeatable" runs but an LLM agent is not repeatable. Replay makes CI gating cheap, fast and deterministic. | §3.4, §5.4, §6.9 |
| 3 | **Oracle and invariant model.** Every finding records *where its expectation came from*: spec, user-declared invariant, generic heuristic, or AI-inferred (unconfirmed). | Schema validation alone misses business-logic bugs. Without explicit oracles, the AI ends up judging expected behavior, which breaks Principle 2. | §6.7, §8.4 |
| 4 | **Regression semantics and finding fingerprints.** "Resolved" requires re-execution, never mere absence. | An exploring agent may simply not revisit an endpoint. Absence of a finding does not prove it was fixed. | §6.6, §8.3 |
| 5 | **Test-data and environment hygiene.** A resource ledger, cleanup, run-scoped namespacing, and reset hooks. | Stateful write tests pollute staging and make findings irreproducible. | §6.8 |
| 6 | **Confirmation protocol and flakiness handling.** | "Confirmed" was undefined. Now it means reproduced N times from a clean precondition. | §6.5 |
| 7 | **Spec drift: spec defect vs implementation defect.** | The spec is often what's wrong. Findings should say which side likely needs fixing, and the tool can propose a spec patch. | §6.5, FR-047 |
| 8 | **Multi-identity auth profiles.** | Cross-user (BOLA) testing, the highest-value security class, requires at least two identities plus unauthenticated. | §6.2, §10.1 |
| 9 | **LLM cost budget and model tiering.** | Runtime limits covered requests but not tokens or money. Cost per confirmed finding is a key metric. | §7.8, §11.3 |
| 10 | **LLM data-egress controls.** Redact before model context, with a local-model option. | Response bodies from staging can contain PII. Sending them to a third-party LLM is a real adoption blocker. | §11.4 |
| 11 | **Target-safety additions.** 429/Retry-After handling, a circuit breaker, production heuristics, and SSRF-via-target payloads. | Protects shared staging environments and prevents the agent from using the target as a proxy. | §11 |
| 12 | **Coverage model and severity rubric.** | Coverage now has defined denominators, and severity has deterministic default rules. | §8.2, §8.5 |
| 13 | **CI, SARIF/JUnit and regression-test export moved earlier.** | Committed regression tests in the user's repo are the strongest retention mechanism, and the Platform persona needs CI. | §6.9, §14 |
| 14 | **MCP server.** Expose API Autopilot to coding agents. | This turns Claude Code/Codex from competitors (§2.2) into a distribution channel and a fix→verify loop. | §9.4 |
| 15 | **Leaner weekend MVP.** CLI plus a static HTML report, SQLite, and an in-process worker. React, Postgres and Redis come in Phase 2. | The v1.0 weekend stack (React + FastAPI + Postgres + Redis/Celery + agent) was too large for a weekend. | §9.1, §14.1 |
| 16 | **Product metrics and external benchmarks** (crAPI, VAmPI, held-out fixtures). Results come from multiple seeds at equal request budgets. | A self-seeded benchmark risks overfitting, and single LLM runs are noisy. | §13 |
| 17 | **Target-side correlation.** Run-ID header plus optional log/trace ingestion. | Lets the Investigator use real stack traces for 5xx errors instead of guessing. | FR-038, FR-048 |
| 18 | **Fixed the timeline inconsistency.** The cover said "2–3 week expansion" but the phases summed to 3–5 weeks. | Internal consistency. | Cover, §14 |

## Contents

1. Executive summary
2. Problem statement, opportunity and competitive landscape
3. Product vision, goals and non-goals
4. Users and core use cases
5. Product experience and end-to-end workflow
6. Functional requirements
7. AI and agent architecture
8. Testing, oracles and exploration capabilities
9. System architecture and technology
10. Data model and API contracts
11. Safety, security and operational controls
12. UX and reporting requirements
13. Evaluation and success metrics
14. MVP scope and phased roadmap
15. Acceptance criteria
16. Risks, mitigations and open questions
- Appendix A. Example autonomous investigation
- Appendix B. Suggested repository structure
- Appendix C. Built-in oracle catalogue <span class="new">NEW</span>

<div class="pb"></div>

## 1. Executive summary

API Autopilot is an AI-native API quality platform. It autonomously explores an API, discovers unexpected behavior, investigates failures, and preserves useful knowledge as **deterministic regression suites** for future runs.

A developer supplies an OpenAPI/Swagger document and a permitted target environment. The platform parses the API contract, builds a model of endpoints and dependencies, and launches an orchestrated investigation. AI agents propose hypotheses and select experiments. Deterministic tools execute requests and validate results against explicit **oracles**. The system uses observations to decide what to test next.

Unlike a basic LLM wrapper, API Autopilot is built around a persistent testing engine, bounded autonomous execution, evidence-backed findings, repeatable workflows and historical comparison. <span class="upd">UPDATED</span> It runs in two modes:

- **Explore mode** (AI-driven, adaptive, nondeterministic): finds new behavior.
- **Replay mode** (no LLM, deterministic, cheap): re-executes frozen scenarios in CI and gates releases.

<div class="callout">The product does not claim that an LLM can prove an API is bug-free. It reports observed behavior, contract violations and evidence-backed hypotheses, with confidence, expectation source and reproducibility details.</div>

### 1.1 Product at a glance

| Dimension | Definition |
|---|---|
| Input | OpenAPI 3.x specification, base URL, environment configuration, one or more test identities, and explicit authorization to test. |
| Core processing | Contract parsing, endpoint graph construction, AI planning, stateful request execution, oracle/assertion validation, adaptive exploration. |
| Output | Test runs, coverage view, reproducible findings, execution traces, regression comparisons, exportable regression tests (pytest/Hurl/curl), SARIF/JUnit reports, and suggested spec patches. |
| AI role | Interpret API semantics, formulate hypotheses, propose candidate invariants, choose experiments, interpret observations and prioritize follow-up tests. |
| Deterministic role | Enforce scope and budgets, execute HTTP calls, evaluate oracles, track state and created resources, classify hard failures, store evidence, and replay frozen scenarios. |
| Interfaces <span class="new">NEW</span> | CLI (first), web UI, REST API, CI action, MCP server for coding agents. |
| Initial deployment | Local-first, single-user or small-team, containerized for local/staging use. |

## 2. Problem statement, opportunity and competitive landscape

### 2.1 Problem

API testing is often fragmented across manually written tests, API clients, CI scripts and ad-hoc debugging. Test suites may cover expected happy paths while missing boundary conditions, stateful workflows, authorization gaps and interactions between endpoints. When a failure occurs, developers still need to reproduce it, correlate requests, and work out whether it is a contract issue, an environment issue, a business-logic defect, or simply an outdated spec.

### 2.2 Why existing coding agents are not the whole answer

General coding agents such as Claude Code and Codex can generate tests and execute tools when configured. API Autopilot should not compete by claiming those agents cannot test APIs. Its differentiated value is a dedicated, repeatable quality system: persistent test history, controlled execution, coverage and state tracking, adaptive exploration, regression comparison, evidence capture, and team-friendly reporting. <span class="upd">UPDATED</span> Coding agents are also a **distribution channel**: through the MCP server (§9.4), a developer's coding agent can launch a run, read findings, apply a fix, and re-run the replay suite.

- **Persistent:** test knowledge and run history survive beyond a single chat session.
- **Repeatable:** confirmed scenarios replay deterministically, without an LLM, with recorded environment context.
- **Bounded:** request, token and cost budgets, timeouts, endpoint scope and destructive-operation policies are enforced by code.
- **Measurable:** findings, coverage, false positives, cost and discovery effectiveness can be evaluated.
- **Operational:** scheduled and CI-triggered runs execute without a developer supervising every request.

### 2.3 Product opportunity

Provide a focused autonomous testing workspace for teams that want to find more defects from an API contract and a staging environment, without hand-authoring every possible test. Start with a reliable single-project workflow, then expand toward continuous API quality intelligence.

### 2.4 Competitive landscape and differentiation <span class="new">NEW</span>

Strong non-AI tools already generate tests from OpenAPI. API Autopilot must beat them on a measurable axis, not just describe itself differently.

| Category | Examples | What they do well | Gap API Autopilot targets |
|---|---|---|---|
| Property-based / schema fuzzers | Schemathesis | Fast, mature, schema-driven generation; stateful testing via OpenAPI links; CI-friendly. | Semantic intent from descriptions; business-rule invariants the schema doesn't express; explained, triaged findings. |
| Stateful REST fuzzers | RESTler (Microsoft Research) | Infers producer→consumer dependencies; finds 5xx errors and resource leaks. | Hypothesis-driven, explainable exploration; authorization differentials; human-readable investigations. |
| Search-based test generation | EvoMaster | Evolutionary generation; can emit test code; white-box mode for some stacks. | Low-setup black-box use with natural-language reasoning and cross-run memory. |
| API clients with AI assist | Postman (incl. AI assistant), Insomnia | Ubiquitous; collections; manual + scripted tests. | Autonomous, budgeted exploration and regression intelligence rather than assisted authoring. |
| API security DAST | StackHawk, 42Crunch, Burp Suite | Security checks, CI integration, compliance reporting. | Functional and business-logic defects alongside authorization checks, with full reproduction. |
| Traffic-recording test generators | Keploy and similar | Tests from real traffic; low authoring effort. | Works pre-release from the contract alone; explores behavior that traffic never exercised. |

**Positioning statement:** *Schemathesis finds what the schema says is wrong; API Autopilot finds what the schema never said, then turns it into a deterministic test.* **Strategic implication:** API Autopilot may reuse a mature fuzzer (e.g., Schemathesis) as one deterministic strategy rather than rebuilding generic fuzzing, and spend its effort on semantic hypotheses, invariants, investigation and memory.

## 3. Product vision, goals and non-goals

### 3.1 Vision

<div class="callout">Give every API a tireless, evidence-driven investigator that continuously explores its behavior, finds meaningful edge cases, and helps engineers reproduce and prevent defects.</div>

### 3.2 Product goals

- Turn an OpenAPI specification into an executable model of endpoints, schemas, auth requirements and likely dependencies.
- Use AI to generate and prioritize test hypotheses rather than output static test lists.
- Execute safe, stateful multi-step workflows against explicitly authorized environments.
- Adapt subsequent experiments using real responses and previous observations.
- Produce findings with request/response evidence, reproduction steps, clear confidence labels, and an explicit expectation source.
- Compare runs to identify newly introduced regressions and recurring or flaky behavior.
- <span class="new">NEW</span> Convert confirmed findings and valuable exploratory scenarios into deterministic replay suites and exportable test code.
- Make AI behavior measurable through a benchmark of seeded and external defects, compared against a strong non-AI baseline.

### 3.3 Non-goals for the first release

- Replacing a full enterprise API management, observability or security platform.
- Guaranteeing discovery of all bugs or proving absence of vulnerabilities.
- Testing arbitrary public targets without authorization.
- Automatically modifying production data or fixing application code.
- Training a foundation model from scratch.
- Supporting every API protocol, authentication scheme and specification format on day one.
- Allowing unconstrained multi-agent conversations or unlimited autonomous tool execution.
- <span class="new">NEW</span> Rebuilding generic schema fuzzing that mature open-source tools already do well.

### 3.4 Product principles

1. **Evidence before explanation:** findings must link to concrete requests, responses, assertions or traces.
2. **Deterministic execution:** the LLM never decides whether a hard assertion passed; code does.
3. **Bounded autonomy:** agents operate under budgets, policies, allowlists and time limits.
4. **Human control:** users explicitly authorize targets and choose what data-changing actions are allowed.
5. **Useful memory:** retain structured, inspectable run knowledge, not opaque claims that the model has learned.
6. <span class="new">NEW</span> **Every expectation has a source:** each oracle is tagged as spec, user-declared, generic heuristic, or AI-inferred. AI-inferred expectations produce only *suspected* findings until a human confirms the invariant.
7. <span class="new">NEW</span> **AI explores, replay gates:** CI pass/fail decisions come from deterministic replay of frozen scenarios, never from a live LLM run.
8. <span class="new">NEW</span> **Leave no trace:** every resource the agent creates is recorded and cleaned up where policy allows.

## 4. Users and core use cases

### 4.1 Primary personas

| Persona | Needs | Primary value |
|---|---|---|
| Backend developer | Validate new endpoints, catch edge cases, reproduce failures before merge. | Fast feedback, actionable bug reports, and a ready-to-commit regression test. |
| QA / test engineer | Expand coverage, generate workflows and reduce repetitive test authoring. | More scenarios and reusable regression suites. |
| Platform / DevOps engineer | Run checks against staging or deployments and compare releases. | Deterministic CI quality gates and regression visibility. |
| Small engineering team lead | Understand API quality without building a large custom test framework. | Centralized results, prioritization and cost visibility. |
| <span class="new">NEW</span> Coding agent (via MCP) | Verify an API change it just made. | Machine-readable findings and a fast replay loop. |

### 4.2 Core user stories

- As a developer, I can upload an OpenAPI document and see the endpoints and schemas the platform extracted.
- As a developer, I can register a staging base URL and explicitly approve the target before any requests run.
- As a tester, I can launch an investigation and see which hypothesis the agent is testing and why.
- As a tester, I can inspect every request, response, assertion and tool call in a run.
- As a developer, I can reproduce a finding using the exact sanitized sequence of requests.
- As a team member, I can compare a current run with a baseline run and identify new failures.
- As an administrator, I can limit methods, endpoints, request rate, run duration, LLM spend and destructive operations.
- <span class="new">NEW</span> As a developer, I can export a confirmed finding as a pytest/Hurl/curl regression test and commit it to my repo.
- <span class="new">NEW</span> As a tester, I can confirm or reject an AI-proposed invariant (e.g., "order total = price × quantity") so it becomes a deterministic oracle.
- <span class="new">NEW</span> As a platform engineer, I can run the replay suite in CI and get JUnit/SARIF output and a non-zero exit code on regressions.
- <span class="new">NEW</span> As a developer, I can see whether a finding most likely means the *spec* or the *implementation* is wrong, and get a suggested spec patch.

### 4.3 Key use cases

| Use case | Trigger | Expected outcome |
|---|---|---|
| Initial API exploration | User uploads a specification and configures a staging target. | Endpoint map, spec quality report, baseline checks and prioritized test plan. |
| Stateful workflow discovery | API has related create/read/update/delete or auth endpoints. | Multi-request workflows with IDs and tokens carried between steps; created resources cleaned up. |
| Adaptive edge-case testing | An initial response reveals an unexpected behavior. | Agent proposes a follow-up experiment informed by the observation. |
| Failure investigation | A test fails or an invariant is violated. | Evidence-backed diagnosis with minimal, confirmed reproduction steps where feasible. |
| Regression analysis | A new deployment is tested against a prior run. | Baseline scenarios replayed; new, resolved, recurring, flaky and changed findings highlighted. |
| <span class="new">NEW</span> CI quality gate | Pull request or deploy pipeline. | Deterministic replay suite runs in minutes with no LLM calls; optional bounded explore run reports separately. |
| <span class="new">NEW</span> Authorization sweep | Two or more identities are configured. | Cross-identity access differentials across all object-ID endpoints. |

## 5. Product experience and end-to-end workflow

### 5.1 Primary workflow

1. **Create project:** user creates a project and uploads an OpenAPI JSON/YAML file, or provides a specification URL (URL import may be deferred).
2. **Review API model:** platform displays parsed endpoints, methods, parameters, request/response schemas, security schemes, parsing warnings, and a <span class="new">spec quality report</span> (missing examples, missing error responses, untyped fields, missing descriptions).
3. **Configure environment:** user enters base URL, non-production environment label, <span class="upd">one or more identity profiles</span>, allowed endpoint/method scope, and optional reset/seed hooks.
4. **Authorize run:** user confirms target ownership/permission, safe methods, request budget, <span class="upd">LLM cost budget</span>, and whether data-changing operations are permitted.
5. **Plan investigation:** AI proposes hypotheses, workflows and candidate invariants. User can inspect the plan, and confirm or reject invariants, before execution.
6. **Execute:** engine runs requests, stores state, records created resources, evaluates oracles and streams progress.
7. **Adapt:** agent observes responses and chooses bounded follow-up experiments.
8. **Confirm:** <span class="new">NEW</span> candidate findings are re-executed from a clean precondition per the confirmation protocol (FR-046).
9. **Clean up:** <span class="new">NEW</span> the engine deletes created resources where policy allows and reports anything left behind.
10. **Review findings:** dashboard shows severity, confidence, expectation source, evidence, reproduction steps and limitations.
11. **Promote:** <span class="new">NEW</span> user promotes confirmed scenarios into the project's replay suite and/or exports them as test code.
12. **Save baseline:** user marks an accepted run as a baseline for future regression comparisons.

### 5.2 Main navigation

- **Projects:** project list, environment status and recent runs.
- **API Explorer:** parsed endpoint inventory, schemas, dependencies, spec quality and coverage.
- **Investigations:** active and completed autonomous runs, plans and agent activity.
- **Findings:** searchable issue list with evidence and reproduction steps.
- **Invariants** <span class="new">NEW</span>: user-declared and AI-proposed oracles with confirmation status.
- **Replay suites** <span class="new">NEW</span>: frozen scenarios, last results and flakiness history.
- **Regression:** run comparisons and historical trends.
- **Settings:** identities, credentials, scope, safety policies, budgets and retention.

### 5.3 Run states <span class="upd">UPDATED</span>

| State | Meaning |
|---|---|
| Draft | Configuration is incomplete; no requests have been sent. |
| Awaiting approval | Plan is ready and requires user authorization. |
| Queued | Approved run is waiting for an execution worker. |
| Running | Agent and test engine are executing within limits. |
| Needs input <span class="new">NEW</span> | Agent raised a question (e.g., an ambiguous invariant) and the policy says to wait; other non-dependent work may continue. |
| Paused | User or policy has paused the run. |
| Cleaning up <span class="new">NEW</span> | No new experiments; the resource ledger is being cleaned. |
| Completed | Run ended normally. A `stop_reason` is always recorded: `plan_exhausted`, `request_budget`, `cost_budget`, `time_budget`, `iteration_budget`, `no_progress`. |
| Stopped by safety <span class="new">NEW</span> | Circuit breaker, policy violation or kill switch ended the run. |
| Failed | Infrastructure or unrecoverable execution error prevented completion. |
| Cancelled | User cancelled the run. |

### 5.4 Run modes <span class="new">NEW</span>

| Mode | LLM used | Deterministic | Typical trigger | Output |
|---|---|---|---|---|
| Plan | Yes | No | User | Hypotheses, workflows, candidate invariants; no requests sent. |
| Explore | Yes | No (seeded where possible) | User, schedule | New findings, coverage, candidate scenarios. |
| Replay | **No** | Yes | CI, deploy, schedule | Pass/fail per frozen scenario; regression report; exit code. |
| Confirm | No | Yes | Automatic after a candidate finding | Confirmed / flaky / not reproduced. |

## 6. Functional requirements

Priority definitions: **P0** = required for MVP; **P1** = important next increment; **P2** = later enhancement.

### 6.1 Project and specification management

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-001 | Create and manage projects. | P0 | User can create, rename and archive a project. |
| FR-002 | Upload OpenAPI 3.x JSON/YAML. | P0 | Valid specification parses; invalid input produces actionable errors. |
| FR-003 | Extract operations, parameters, schemas and security schemes. | P0 | Endpoint inventory is viewable and searchable. |
| FR-004 | Display parser warnings and unsupported constructs. | P0 | Warnings identify the affected path/operation. |
| FR-005 | Support multiple environments per project. | P1 | Dev/staging configurations are stored separately. |
| FR-006 | Import specs from a URL or repository. | P2 | External fetch is allowlisted and protected against SSRF. |
| FR-007 <span class="new">NEW</span> | Spec quality report. | P1 | Lists missing error responses, examples, constraints and descriptions per operation, with a testability score. |
| FR-008 <span class="new">NEW</span> | Spec versioning and diff. | P1 | New upload creates a version; added/removed/changed operations are shown and linked to runs. |
| FR-009 <span class="new">NEW</span> | Import Postman collections or HAR traffic to seed examples and workflows. | P2 | Imported requests become example inputs and candidate workflows, never executed without policy approval. |

### 6.2 API knowledge model

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-010 | Build endpoint dependency graph from path parameters, schemas, references, OpenAPI links and operation semantics. | P0 | Graph nodes and edges can be inspected. |
| FR-011 | Identify candidate workflows such as create→read→update→delete. | P0 | At least one plausible workflow is produced for a seeded sample API. |
| FR-012 | Represent auth requirements and required inputs. | P0 | Protected operations are marked and missing auth blocks execution. |
| FR-013 | Extract semantic hints from descriptions and operation IDs. | P0 | Structured hints include source references and confidence. |
| FR-014 | Allow users to edit or confirm inferred dependencies. | P1 | Manual corrections persist and influence planning. |
| FR-015 <span class="new">NEW</span> | Multiple identity profiles per environment (e.g., `user_a`, `user_b`, `admin`, `anonymous`). | P0 (2 identities) / P1 (roles) | Each request names an identity reference; secrets resolved only by the execution service. |
| FR-016 <span class="new">NEW</span> | Auth setup flows: static bearer/API key (P0), login-endpoint token acquisition and refresh (P1), OAuth2 client-credentials (P2). | P0/P1/P2 | Expired tokens are refreshed without consuming the agent's iteration budget. |
| FR-017 <span class="new">NEW</span> | Classify operation side-effect risk (read, write, delete, external side effect such as payment, email or webhook) from method, tags, names and descriptions, with user override. | P0 | Risk class is visible and enforced by policy. |

### 6.3 AI planning and exploration

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-020 | Generate test hypotheses from API contract and project context. | P0 | Each hypothesis includes rationale, target operation, expected observation and **oracle reference**. |
| FR-021 | Generate positive, negative and boundary-value test scenarios. | P0 | Scenarios are schema-aware and validatable. |
| FR-022 | Plan multi-step workflows with variable extraction. | P0 | IDs/tokens from earlier responses can feed later requests. |
| FR-023 | Choose follow-up experiments based on prior observations. | P0 | At least one supported observation type changes the next experiment. |
| FR-024 | Prioritize by risk, coverage gap and historical yield. | P1 | Priority rationale is visible. |
| FR-025 | Use historical outcomes to recommend future tests. | P1 | Prior findings and test outcomes are retrieved with provenance. |
| FR-026 | Support multiple specialized agents. | P2 | Planner, explorer and investigator can be separately evaluated. |
| FR-027 <span class="new">NEW</span> | Propose candidate invariants from descriptions and observed behavior. | P1 | Each proposal is expressed in the invariant DSL (§8.4), shows its source text, and is inactive until confirmed. |
| FR-028 <span class="new">NEW</span> | Agent can ask the user a question (`ask_user`) instead of guessing. | P1 | Questions are non-blocking by default; answers persist as project knowledge or invariants. |
| FR-029 <span class="new">NEW</span> | Progress detection. | P0 | Run stops with `no_progress` after K consecutive iterations with no new coverage, observation class or finding. |

### 6.4 Execution and validation

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-030 | Execute HTTP requests using a controlled execution service. | P0 | Requests obey target allowlist, timeout and rate limits. |
| FR-031 | Validate status codes, headers, response schema and configured assertions/oracles. | P0 | Validation results are deterministic and separately recorded. |
| FR-032 | Extract response values into workflow variables. | P0 | JSON paths can populate subsequent request fields. |
| FR-033 | Record request/response metadata and execution trace. | P0 | Each finding links to its evidence. |
| FR-034 | Support request, token/cost, duration and concurrency caps. | P0 | Limits cannot be overridden by the agent. |
| FR-035 | Support cancellation and pause. | P0 | No new requests start after cancellation is acknowledged. |
| FR-036 | Support retry policy for transient network errors. | P1 | Retries are bounded and distinguish transport failure from API behavior. |
| FR-037 <span class="new">NEW</span> | Honor `429`/`Retry-After`, and trip a circuit breaker on 5xx bursts or latency spikes. | P0 | Run pauses or stops with `stopped_by_safety`; the breaker event is visible in the timeline. |
| FR-038 <span class="new">NEW</span> | Send correlation headers on every request (`X-Autopilot-Run-Id`, `X-Autopilot-Step-Id`, and W3C `traceparent` when enabled). | P0 | Target-side logs can be joined to execution events. |
| FR-039 <span class="new">NEW</span> | Deterministic seeds for all generated values; record seed, model and prompt version per test case. | P0 | Regenerating a test case from its seed yields identical request payloads. |

### 6.5 Findings and investigation

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-040 | Create findings for contract violations and failed oracles/invariants. | P0 | Finding includes evidence, classification and expectation source. |
| FR-041 | AI summarizes likely cause with explicit uncertainty. | P0 | Summary distinguishes observed fact from hypothesis. |
| FR-042 | Generate reproducible request sequence. | P0 | User can replay an allowed sequence in a sandbox. |
| FR-043 | Minimize failing workflow where feasible. | P1 | Reducer (delta-debugging) attempts to remove steps/inputs while preserving failure. |
| FR-044 | Group duplicate or related findings. | P1 | Grouping uses fingerprints (FR-055) and does not discard original evidence. |
| FR-045 | Allow triage: open, accepted, false positive, fixed, ignored, won't fix. | P1 | Status and notes persist; false-positive feedback is used in evaluation metrics. |
| FR-046 <span class="new">NEW</span> | Confirmation protocol. | P0 | A finding is **confirmed** only if its sequence reproduces the failure in N of N re-executions (default N=2) from a fresh precondition. Partial reproduction is labelled **flaky**; no reproduction is labelled **not reproduced**. |
| FR-047 <span class="new">NEW</span> | Classify contract mismatches as *likely implementation defect*, *likely spec defect*, or *ambiguous*, and propose a spec patch for spec defects. | P1 | Patch is a valid OpenAPI diff and is never applied automatically. |
| FR-048 <span class="new">NEW</span> | Optional target-side evidence: ingest logs/traces filtered by correlation ID (OpenTelemetry, file tail or log API). | P2 | Stack traces for 5xx errors are attached as evidence with provenance. |
| FR-049 <span class="new">NEW</span> | Detect sensitive data in responses (tokens, secrets, PII patterns, fields absent from schema). | P1 | Raises an "excessive data exposure" finding; the matched value is redacted in storage. |

### 6.6 Regression and memory

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-050 | Persist run summaries, test cases, findings and evidence metadata. | P0 | History is available after reload. |
| FR-051 | Compare current run against a selected baseline. | P1 | New, resolved, recurring, flaky and changed findings are identified per the semantics below. |
| FR-052 | Track endpoint and scenario coverage over time. | P1 | Coverage uses the defined denominators in §8.6. |
| FR-053 | Retrieve relevant prior findings for planning. | P1 | Retrieved context shows source run and timestamp. |
| FR-054 | Support scheduled and CI-triggered runs. | <span class="upd">P1</span> | Runs inherit project safety policies and budgets. |
| FR-055 <span class="new">NEW</span> | Stable finding fingerprint = hash(operation_id, category, oracle_id, normalized parameter path, identity relation). | P0 | The same defect in two runs gets the same fingerprint even with different generated values. |
| FR-056 <span class="new">NEW</span> | "Resolved" requires re-execution. | P1 | A baseline finding is marked resolved only if its frozen scenario was replayed and passed. If it was not re-executed, it is shown as **not re-tested**. |

**Regression status definitions** <span class="new">NEW</span>

| Status | Rule |
|---|---|
| New | Fingerprint present in current run, absent from baseline. |
| Recurring | Fingerprint present in both. |
| Resolved | Present in baseline; its scenario was replayed in the current run and passed. |
| Not re-tested | Present in baseline; scenario not executed in the current run. |
| Flaky | Scenario outcome differs across confirmation attempts or across the last K replays. |
| Changed | Same fingerprint, but evidence differs materially (e.g., different status code or error body). |

### 6.7 Oracles and invariants <span class="new">NEW</span>

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-060 | Built-in generic oracles (Appendix C): 5xx, schema conformance, undocumented status, read-after-write, delete-then-404, auth differential, idempotency of PUT/DELETE. | P0 (first four) / P1 (rest) | Each oracle has an ID, version and deterministic implementation. |
| FR-061 | User-declared invariants in a small declarative DSL (§8.4). | P1 | Invariants are versioned, validated against the API model, and evaluated deterministically. |
| FR-062 | Every finding records its `expectation_source`: `spec`, `user_invariant`, `generic_oracle`, or `ai_inferred`. | P0 | Findings with `ai_inferred` sources cannot be marked confirmed above "suspected" without human confirmation of the invariant. |
| FR-063 | Metamorphic relations (e.g., filter result ⊆ unfiltered list; pagination pages are disjoint and complete; sort order respected). | P2 | Relations are expressible in the DSL and evaluated deterministically. |

### 6.8 Test data and environment hygiene <span class="new">NEW</span>

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-070 | Resource ledger: record every resource created (operation, ID, identity, run). | P0 | Ledger is visible per run. |
| FR-071 | Automatic cleanup in reverse creation order where a delete operation exists and policy allows. | P1 | End-of-run report lists cleaned and left-behind resources. |
| FR-072 | Run-scoped namespacing: generated string values carry a run prefix (e.g., `ap-<run>-...`) where schemas allow. | P0 | Test data is identifiable and filterable in the target. |
| FR-073 | Environment hooks: optional `reset` and `seed` commands or HTTP calls before a run and before confirmation attempts. | P1 | Hooks run under the same policy; failures stop the run safely. |
| FR-074 | Precondition snapshot: record the fixtures/state a scenario depends on so replay can recreate it. | P1 | Replay fails fast with "precondition unavailable" rather than producing a false finding. |

### 6.9 Replay, export and integrations <span class="new">NEW</span>

| ID | Requirement | Priority | Acceptance summary |
|---|---|---|---|
| FR-080 | Promote a confirmed scenario into a replay suite (frozen requests, variable bindings, oracles, seed). | P0 | Replay runs with no LLM calls and gives identical pass/fail on an unchanged target. |
| FR-081 | CLI: `autopilot init / plan / explore / replay / report`, with exit codes for CI. | P0 | Non-zero exit when replay detects new or recurring findings above a severity threshold. |
| FR-082 | Export findings/scenarios as pytest + httpx, Hurl, or curl scripts. | P1 | Exported test fails on the buggy target and passes after the fix. |
| FR-083 | Machine-readable reports: JSON, Markdown, JUnit XML, SARIF. | P0 (JSON/MD) / P1 (JUnit/SARIF) | SARIF uploads to GitHub code scanning without errors. |
| FR-084 | GitHub Action / generic CI recipe. | P1 | Replay runs on pull request; explore runs optional and non-blocking. |
| FR-085 | MCP server exposing plan, run, replay, list findings and get finding. | P1 | A coding agent can trigger a replay and read results; the same policy and budgets apply. |
| FR-086 | Notifications (Slack/webhook) on new high-severity findings. | P2 | Payload contains only redacted summaries and links. |

## 7. AI and agent architecture

### 7.1 AI design principles

- Use an LLM for semantic interpretation, hypothesis generation, invariant proposal, planning and explanation, **not** for deterministic validation.
- Constrain agent outputs to typed JSON schemas and validate every proposed action before execution.
- Use explicit tool definitions with narrow capabilities. Never give the model arbitrary shell or network access.
- Keep an auditable record of prompts/templates, model/provider, tool calls, observations and decisions.
- Use retrieval for relevant API documentation and historical findings, rather than stuffing the entire project history into every prompt.
- Treat API descriptions, responses and user-provided specs as untrusted data. They must never override system policies or tool permissions.
- <span class="new">NEW</span> Send the model **summaries of observations** (status, schema-diff, oracle results, redacted excerpts), not raw response bodies, by default.

### 7.2 Logical agents

| Component | Responsibility | Inputs | Outputs |
|---|---|---|---|
| API Understanding | Interpret contract semantics and likely relationships. | OpenAPI model, descriptions, schemas. | Structured API knowledge, dependency candidates, candidate invariants, confidence. |
| Planner | Propose investigation hypotheses and workflows. | API graph, scope, invariants, past outcomes, budget. | Prioritized experiment plan. |
| Explorer | Select and adapt tests based on observations. | Plan, previous outcomes, coverage gaps. | Next validated experiment proposal. |
| Investigator | Explain failures and request further evidence. | Execution traces, oracle results, related endpoints, target-side logs (optional). | Evidence-linked diagnosis, spec-vs-implementation classification, reproduction sequence. |
| Memory/retrieval service | Retrieve prior runs and similar findings. | Structured run store and embeddings. | Relevant historical context with provenance. |

For the MVP these are **roles/prompts within one orchestrator**, not separate autonomous agents. Splitting them is gated on evaluation (§14.3).

### 7.3 Agent execution loop

1. Load project policy, API model, invariants, identities and run budget.
2. Build a compact context for the current investigation.
3. Ask planner/explorer for one or more candidate experiments.
4. Validate the proposed experiment against policy, schemas and the side-effect risk class.
5. Execute through the deterministic tool gateway; record created resources in the ledger.
6. Evaluate oracles and persist the observation.
7. Update coverage, workflow state, investigation memory and progress counters.
8. If an oracle fails, queue a confirmation attempt (deterministic, no LLM).
9. Ask the agent to interpret the observation and choose a follow-up.
10. Stop on completion, budget exhaustion, no progress, cancellation or safety block, then run cleanup.

### 7.4 Tool catalogue

| Tool | Purpose | Required controls |
|---|---|---|
| list_operations | Return permitted operations and schemas. | Project and scope filtering. |
| get_operation_details | Return parameters, auth and response schemas. | Read-only knowledge access. |
| execute_request | Send one HTTP request. | Allowlisted host, method policy, risk class, identity reference, rate limit, timeout, payload limit. |
| extract_variables | Extract values from prior responses. | JSONPath allowlist and typed values. |
| search_run_history | Find prior runs/findings. | Project-level access control and provenance. |
| get_related_traces | Retrieve correlated execution evidence. | Trace/run scoping. |
| validate_response | Run deterministic contract and oracle checks. | Versioned validation rules. |
| compare_with_baseline | Compare results to baseline. | Same project/environment context required. |
| propose_invariant <span class="new">NEW</span> | Submit a candidate invariant in DSL form. | Parsed and validated; inactive until confirmed. |
| ask_user <span class="new">NEW</span> | Ask a clarifying question. | Rate-limited per run; non-blocking by default. |
| run_strategy <span class="new">NEW</span> | Invoke a deterministic strategy (e.g., a boundary sweep, or an external fuzzer like Schemathesis on one operation). | Strategy allowlist; counts against request budget. |

### 7.5 Adaptive exploration strategy

The agent should not freely invent unlimited requests. It proposes experiments from a controlled strategy library, and the policy layer validates them. Candidate strategies include boundary values, missing required fields, unexpected enum values, invalid identifiers, authorization variations (identity swaps), state transition tests, duplicate submissions, cross-resource access checks, mass-assignment probes (sending read-only/undocumented fields), and concurrency races (P2, opt-in).

For later iterations, a contextual bandit can rank test strategies using context such as endpoint type, schema constraints, prior yield, failure history and coverage gap. Reward signals may include unique confirmed findings and newly covered behaviors, with penalties for redundant requests, cost, time and false positives. This should be an optional ranking layer, not a claim that the system has trained a general bug-finding model.

### 7.6 Memory design

- **Working memory:** current run state, extracted variables, active hypothesis and recent observations.
- **Episodic memory:** previous run events, test sequences, failures and outcomes.
- **Semantic memory:** embeddings or searchable summaries of API descriptions, prior findings and remediation notes.
- **Policy memory:** project-level constraints, approved operations and environment configuration. This is authoritative configuration, not model-generated memory.
- <span class="new">NEW</span> **Procedural memory:** replay suites and confirmed invariants. These are the most valuable and most durable form of "learning", and they are fully inspectable.

### 7.7 AI reliability and hallucination controls

- Require structured output and reject invalid tool arguments.
- Verify operation IDs, parameters and schema references against the parsed API model.
- Do not accept a model's claim of success without an execution result.
- Attach citations/provenance to retrieved historical context.
- Label root-cause explanations as hypotheses unless confirmed by deterministic evidence.
- Detect repeated or circular agent actions and stop or request human review.
- Version prompts and record model/provider identifiers for reproducibility.
- <span class="new">NEW</span> Run the evaluation benchmark on every prompt or model change (prompt regression testing), and block changes that reduce recall or precision beyond a threshold.

### 7.8 Cost and model strategy <span class="new">NEW</span>

- **Budgets:** per-run caps on input/output tokens and estimated cost. These are enforced by the orchestrator, which stops with `cost_budget` when hit.
- **Model tiering:** a small/fast model for routine experiment selection and summarization; a larger model for planning, invariant proposal and investigation of confirmed failures.
- **Caching:** cache the API model context and static instructions (provider prompt caching where available); cache plan outputs per spec version.
- **Deterministic-first:** generic strategies (boundary sweeps, schema fuzzing) run without LLM calls. The LLM is consulted for *what to try next* and *what it means*, not for generating every payload.
- **Reporting:** show tokens, cost, and cost per confirmed finding for every run.
- **Bring your own key / local model:** support a user-supplied API key and an OpenAI-compatible local endpoint (e.g., Ollama) for data-sensitive teams.

## 8. Testing, oracles and exploration capabilities

### 8.1 Test categories

| Category | Examples | Implementation approach |
|---|---|---|
| Contract testing | Unexpected status, malformed response, missing required property. | OpenAPI schema validation and explicit assertions. |
| Positive testing | Valid create, read, update and delete workflows. | Schema-aware payload generation and state extraction. |
| Negative testing | Missing fields, invalid types, unknown IDs. | Constraint-based payload mutation. |
| Boundary testing | Zero, negative, min/max, very large values and string lengths. | Schema constraints plus strategy templates. |
| Stateful testing | Create resource, update it, read it, delete it, access after deletion. | Workflow graph, variable store and resource ledger. |
| Authorization testing | Unauthenticated, wrong-role or cross-user access (BOLA/BFLA). | Multiple identity profiles and auth-differential oracle. |
| Business-logic testing <span class="new">NEW</span> | Totals don't add up, invalid state transitions accepted, inventory not decremented. | Confirmed invariants (DSL) and state-machine checks. |
| Data exposure <span class="new">NEW</span> | Secrets/PII or undocumented fields in responses; mass assignment accepted. | Response scanning and read-only field probes. |
| Fuzzing | Enum mutations, type changes, nested payload variations. | Bounded mutation operators and reproducible seeds; optionally delegated to an external fuzzer. |
| Regression testing | Previously passing behavior now fails. | Deterministic replay of frozen scenarios. |

### 8.2 Finding severity

| Severity | Definition | Example |
|---|---|---|
| Critical | Severe, directly evidenced impact with high confidence. | Sensitive data exposed across users in an authorized test environment. |
| High | Important security, data integrity or core business rule failure. | Unauthorized modification of another test user's resource. |
| Medium | Meaningful contract or business behavior issue. | Invalid quantity accepted and stored. |
| Low | Minor inconsistency or robustness issue. | Unexpected optional field behavior. |
| Informational | Observation or improvement opportunity, not necessarily a defect. | Undocumented response variation. |

Severity is assigned using deterministic rules where possible and may be suggested by AI. The interface must distinguish severity from confidence. A high-severity hypothesis with weak evidence must not be presented as a confirmed critical vulnerability.

**Default severity rubric** <span class="new">NEW</span>. These are deterministic starting points that AI may suggest raising or lowering, with a recorded reason:

| Oracle / category | Default severity |
|---|---|
| Cross-identity read of another user's resource | High (Critical if sensitive fields are exposed) |
| Cross-identity write/delete | High |
| Unauthenticated access to protected operation | High |
| Secrets/tokens in response | Critical |
| 5xx on schema-valid input | Medium |
| 5xx on invalid input (should be 4xx) | Low |
| Documented constraint not enforced (accepted and stored) | Medium |
| Response schema violation | Low |
| Undocumented status code | Informational |
| Confirmed invariant violated | As declared on the invariant (default Medium) |

### 8.3 Finding data

- Title and concise summary.
- Finding category and severity.
- Confidence level and whether confirmed, suspected, flaky, not reproduced, or informational.
- <span class="new">NEW</span> Expectation source (spec / user_invariant / generic_oracle / ai_inferred) and oracle ID + version.
- <span class="new">NEW</span> Likely fix side: implementation, spec, or ambiguous.
- <span class="new">NEW</span> Fingerprint, first-seen run and last-seen run.
- Project, environment, spec version, endpoint and HTTP method.
- Timestamp, run ID, test case ID, seed and agent hypothesis ID.
- Identity references used (never secrets).
- Redacted request and response details.
- Expected behavior and observed behavior.
- Exact reproduction steps, required preconditions and confirmation attempts.
- Evidence references: status, schema diff, oracle result, trace, response excerpt and target-side logs (if enabled).
- Suggested remediation, regression test recommendation and export links.

### 8.4 Invariant DSL <span class="new">NEW</span>

A small, declarative, deterministic language that lets users (or the AI, as a proposal) express business rules the OpenAPI schema can't. Example:

```yaml
- id: order_total_matches
  scope: POST /orders -> 201
  assert: response.total == response.unit_price * response.quantity
  severity: medium
  source: user            # or ai_proposed (inactive until confirmed)

- id: no_cross_user_order_read
  scope: GET /orders/{id}
  given: resource created_by identity user_a
  when: identity user_b
  assert: status in [403, 404]
  severity: high

- id: cancelled_is_terminal
  scope: POST /orders/{id}/cancel
  given: order.status == "completed"
  assert: status in [409, 422]
```

The DSL is evaluated in a sandboxed expression engine (no arbitrary code). Invariants are versioned and linked to the findings they produce.

### 8.5 Exploration vs replay <span class="new">NEW</span>

Exploration runs produce **candidate scenarios**. A scenario becomes part of the replay suite only when it is (a) linked to a confirmed finding, or (b) explicitly promoted by a user as a valuable passing check. Replay suites are stored as plain, diff-able files (YAML/JSON) so they can live in the user's repository.

### 8.6 Coverage model <span class="new">NEW</span>

Every coverage number must state its denominator. The product reports these separately and never as a single "API tested" percentage:

| Coverage dimension | Numerator / denominator |
|---|---|
| Operation coverage | Operations executed at least once / operations in scope |
| Documented-response coverage | (operation, documented status) pairs observed / pairs documented |
| Parameter constraint coverage | Constraints (min, max, enum, required, pattern) probed / constraints declared |
| Workflow coverage | Inferred workflows executed end-to-end / workflows inferred |
| Authorization coverage | Object-ID operations tested with a second identity / object-ID operations |
| Strategy matrix | Strategies applied per operation (heat map, not a percentage) |

## 9. System architecture and technology

### 9.1 Recommended architecture <span class="upd">UPDATED</span>

Use a modular monolith with a separate execution worker boundary. **For the weekend MVP, collapse the stack**: CLI plus a static HTML report, SQLite, and an in-process async worker. Add the web UI, PostgreSQL and a job queue in Phase 2 behind the same internal interfaces.

| Layer | Weekend MVP | Phase 2+ | Reason |
|---|---|---|---|
| Interface | CLI (Typer) + generated static HTML/Markdown report | React + TypeScript web UI | A CLI is enough to prove the loop and is CI-native from day one. |
| API backend | — (library + CLI) | Python + FastAPI | Typed APIs and a strong Python ecosystem for testing/AI. |
| Specification parsing | prance / openapi-spec-validator | Same | Contract parsing and validation. |
| Graph model | NetworkX | Same | Endpoint dependency and workflow graph algorithms. |
| HTTP execution | HTTPX (async) | Same, in a separate worker process | Timeouts, connection pooling, event hooks for policy checks. |
| Schema validation | jsonschema / openapi-core | Same | Deterministic response/request checks. |
| Data generation | Hypothesis strategies (seeded) | Same + optional Schemathesis strategy | Reproducible boundary/fuzz values. |
| AI integration | Provider-agnostic adapter + mock adapter | Same + model tiering, local model | Switch providers; tests need no live key. |
| Persistence | SQLite | PostgreSQL | Zero-setup for the weekend; Postgres for teams. |
| Vector retrieval | — | pgvector (optional) | Semantic search over past findings. |
| Background jobs | asyncio in-process | Redis + RQ/Celery or Postgres-backed queue | Avoid queue infrastructure until needed. |
| Packaging | `pipx install` + Docker Compose for demo API | Docker Compose | Reproducible local development and demo. |

### 9.2 High-level components

- **Interfaces:** CLI, web UI (Phase 2), REST API, MCP server (Phase 2–3).
- **API service:** authentication, project CRUD, spec ingestion, run creation, results and policy configuration.
- **Knowledge service:** spec normalization, versioning, graph generation, operation metadata, risk classification and retrieval.
- **Agent orchestrator:** bounded state machine coordinating planner, explorer and investigator roles; enforces progress and cost budgets.
- **Tool gateway:** validates every agent action against project policy before execution.
- **Execution worker:** performs HTTP requests, resolves identity references, maintains the resource ledger, and runs cleanup.
- **Oracle engine** <span class="new">NEW</span>: evaluates generic oracles and DSL invariants deterministically.
- **Replay engine** <span class="new">NEW</span>: executes frozen scenarios without any LLM dependency.
- **Exporters** <span class="new">NEW</span>: pytest/Hurl/curl, JUnit, SARIF, Markdown.
- **Persistence layer:** stores structured data, run events, redacted evidence and optional embeddings.
- **Evaluation harness:** runs seeded and external API fixtures and calculates quality metrics.

### 9.3 Important design choice

<div class="callout">The LLM must never call the target API directly. It proposes a structured action; the tool gateway validates it; the execution worker performs it; the oracle engine records the result. <strong>The replay engine must be able to run with the LLM adapter removed entirely.</strong> This separation is central to security, reproducibility and trustworthy evaluation.</div>

### 9.4 MCP server <span class="new">NEW</span>

Expose a narrow MCP server so coding agents (Claude Code, Codex, IDE agents) can use API Autopilot as a tool:

| MCP tool | Effect | Control |
|---|---|---|
| `autopilot_list_findings` | Read findings for a project/run. | Read-only, redacted. |
| `autopilot_get_finding` | Evidence, reproduction, export snippet. | Read-only, redacted. |
| `autopilot_replay` | Run the replay suite against a configured environment. | Same policy/budgets; environment must be pre-approved. |
| `autopilot_explore` | Start a bounded explore run. | Requires a pre-approved environment and a per-call budget cap; disabled by default. |

This enables a tight **fix → verify** loop: the coding agent reads a finding, edits code, runs replay, and confirms the fingerprint is resolved.

## 10. Data model and API contracts

### 10.1 Core entities <span class="upd">UPDATED</span>

| Entity | Important fields |
|---|---|
| Project | id, name, description, created_at, owner_id |
| ApiSpecification | id, project_id, version, raw_file_ref, parsed_model, checksum, quality_report |
| Environment | id, project_id, name, base_url, allowed_hosts, policy_id, reset_hook, seed_hook, is_production_like |
| Identity <span class="new">NEW</span> | id, environment_id, name, role, auth_type, secret_ref, token_flow |
| Operation | id, spec_id, method, path, operation_id, tags, request_schema, response_schema, auth_required, risk_class |
| DependencyEdge | id, source_operation_id, target_operation_id, dependency_type, confidence, provenance, user_confirmed |
| Invariant <span class="new">NEW</span> | id, project_id, version, definition (DSL), source (user/ai_proposed/builtin), status (proposed/active/rejected), severity |
| TestRun | id, project_id, environment_id, spec_id, mode (plan/explore/replay/confirm), status, stop_reason, model_ids, prompt_version, budget, tokens_used, cost_usd, started_at, completed_at |
| RunApproval <span class="new">NEW</span> | id, run_id, approved_by, approved_at, target, scope, budgets, write_permission |
| Hypothesis | id, run_id, title, rationale, target_operation_id, oracle_ref, status, confidence |
| TestCase | id, run_id, hypothesis_id, strategy, workflow_definition, seed, priority |
| Scenario <span class="new">NEW</span> | id, project_id, source_test_case_id, frozen_definition, preconditions, oracle_refs, promoted_by, flakiness_score |
| ExecutionEvent | id, run_id, test_case_id, sequence, identity_ref, request_meta, response_meta, duration_ms, validation_result, correlation_id |
| ResourceLedgerEntry <span class="new">NEW</span> | id, run_id, operation_id, resource_id, identity_ref, created_at, cleanup_status |
| Finding | id, run_id, operation_id, fingerprint, category, severity, confidence, confirmation_status, expectation_source, oracle_id, fix_side, status, title, evidence_refs, first_seen_run_id, last_seen_run_id |
| Baseline | id, project_id, environment_id, run_id, created_at |
| AgentEvent | id, run_id, agent_role, model_id, action, input_summary, output_summary, token_usage, cost_usd, timestamp |
| Policy | id, project_id, allowed_methods, endpoint_scope, rate_limit, request_budget, token_budget, cost_budget, timeout, destructive_rules, circuit_breaker, llm_data_policy |

### 10.2 Suggested backend API

| Method + route | Purpose |
|---|---|
| POST /api/projects | Create project. |
| GET /api/projects/{id} | Get project details. |
| POST /api/projects/{id}/specifications | Upload OpenAPI specification (creates a new version). |
| GET /api/projects/{id}/specifications/{v}/diff?against={v2} | <span class="new">NEW</span> Spec version diff. |
| GET /api/projects/{id}/operations | List parsed operations. |
| GET /api/projects/{id}/graph | Retrieve endpoint dependency graph. |
| POST /api/projects/{id}/environments | Configure target environment. |
| POST /api/environments/{id}/identities | <span class="new">NEW</span> Add identity profile (secret reference only). |
| GET/POST /api/projects/{id}/invariants | <span class="new">NEW</span> List, create, confirm or reject invariants. |
| POST /api/projects/{id}/runs/plan | Generate an investigation plan without execution. |
| POST /api/projects/{id}/runs | Create and queue an approved run (`mode`: explore/replay). Requires `Idempotency-Key`. |
| GET /api/runs/{run_id} | Get run status, stop_reason, budget usage and summary. |
| GET /api/runs/{run_id}/events | Stream run events; SSE recommended. |
| POST /api/runs/{run_id}/cancel | Cancel an active run. |
| GET /api/runs/{run_id}/ledger | <span class="new">NEW</span> Created resources and cleanup status. |
| GET /api/runs/{run_id}/findings | List run findings. |
| GET /api/findings/{finding_id} | Get finding and evidence. |
| GET /api/findings/{finding_id}/export?format=pytest\|hurl\|curl | <span class="new">NEW</span> Export as regression test. |
| POST /api/findings/{finding_id}/promote | <span class="new">NEW</span> Promote scenario to replay suite. |
| GET /api/runs/{run_id}/report?format=json\|md\|junit\|sarif | <span class="new">NEW</span> Machine-readable report. |
| POST /api/projects/{id}/baselines | Save a selected run as baseline. |
| GET /api/projects/{id}/regressions?run_id=... | Compare run against baseline. |

### 10.3 Example structured agent action

```json
{
  "action": "execute_request",
  "hypothesis_id": "hyp_123",
  "operation_id": "createOrder",
  "method": "POST",
  "path": "/orders",
  "identity_ref": "user_a",
  "body": {
    "productId": "{{state.product_id}}",
    "quantity": -1
  },
  "oracle_refs": ["spec.constraint.minimum", "builtin.read_after_write"],
  "expected_observation": "Determine whether negative quantity is rejected",
  "reasoning_summary": "Probe a numeric boundary condition",
  "risk": "medium"
}
```

This action is only a proposal. The server must independently verify that the operation, method, path, identity reference, variable references, payload size, risk class and policy are allowed before execution.

### 10.4 Data retention and redaction

- Never persist raw secrets, authorization headers, cookies or unredacted sensitive response fields by default.
- Store credentials in an encrypted secret store or environment-specific secret references, not in prompts or ordinary run JSON.
- Allow configurable retention for raw response bodies; retain minimal evidence where possible.
- Keep an audit trail (RunApproval) of who authorized a run and what target/scope/budget was approved.
- Support deletion of projects and associated run data.
- <span class="new">NEW</span> Apply the same redaction pipeline to exported tests and reports (secrets become environment-variable placeholders).

## 11. Safety, security and operational controls

### 11.1 Authorization and scope

- User must affirm that they own or are authorized to test the target.
- Default to localhost, explicitly configured private staging hosts or verified allowlisted domains.
- Do not permit arbitrary target URLs supplied by the agent.
- Block cloud metadata IP ranges, loopback and private-network destinations unless explicitly configured for local testing. Defend against DNS rebinding and redirect-based SSRF.
- Validate every redirect destination and resolved IP before connecting.
- Apply endpoint allowlists and HTTP method restrictions.
- <span class="new">NEW</span> **Production heuristics:** if the hostname or environment label looks like production (e.g., contains `prod`, `live`, or matches a known public domain), or responses indicate real data, require a typed confirmation and force read-only mode unless an administrator overrides.
- <span class="new">NEW</span> **SSRF via the target:** for request fields that accept URLs (webhooks, callbacks, image URLs), the agent may only supply values from a configured canary/allowlist, never internal addresses. Otherwise the target API could be turned into a proxy.

### 11.2 Destructive-operation controls

- Default execution policy is read-only (GET/HEAD/OPTIONS) until a user explicitly enables writes.
- POST/PUT/PATCH/DELETE require environment-level permission and explicit run approval.
- Prefer isolated test tenants, synthetic data and resettable fixtures.
- Never execute destructive production tests by default.
- Require confirmation for operations whose risk class is `external_side_effect` (payments, messages, emails, webhooks, third-party calls).
- Provide a global kill switch and immediate run cancellation.
- <span class="new">NEW</span> Cleanup via the resource ledger; report any resources left behind.

### 11.3 Runtime limits <span class="upd">UPDATED</span>

| Control | MVP default (configurable) |
|---|---|
| Max requests per run | 100 |
| Max concurrent requests | 2 |
| Rate limit | 5 requests/second; honor `Retry-After` |
| Per-request timeout | 10 seconds |
| Max run duration | 10 minutes |
| Max request body | 1 MB |
| Max response body stored | 256 KB (truncated, redacted) |
| Redirects | Disabled by default or strictly revalidated |
| Retries | At most 1 retry for selected transient transport failures |
| Agent iterations | 20 |
| No-progress stop | 5 consecutive iterations without new coverage or findings |
| LLM token budget <span class="new">NEW</span> | 200k input + 20k output tokens per run |
| LLM cost budget <span class="new">NEW</span> | US$1.00 per run |
| Circuit breaker <span class="new">NEW</span> | Pause after 5 consecutive 5xx/timeouts, or p95 latency over 3× the baseline |
| Confirmation attempts <span class="new">NEW</span> | 2 per candidate finding (counted in the request budget) |

These are conservative starting defaults for a local/staging MVP, not universal production recommendations. The user or project administrator may lower them; raising them requires an explicit policy change.

### 11.4 AI-specific security and data egress <span class="upd">UPDATED</span>

- Treat API descriptions, examples, response bodies and error text as untrusted prompt content.
- Ignore instructions embedded in API responses or specification descriptions that attempt to change agent policy.
- Do not include secrets in model context. Use opaque credential references resolved only by the execution service.
- Use structured outputs, schema validation and tool allowlists.
- Separate system instructions from retrieved content and clearly delimit untrusted data.
- Apply output size limits, redaction and logging controls.
- <span class="new">NEW</span> **LLM data policy per project:** `metadata_only` (status, schema-diff, field names; default), `redacted_excerpts`, or `full` (explicit opt-in). PII/secret detectors run before any content reaches the model.
- <span class="new">NEW</span> Support a local or self-hosted model endpoint for teams that cannot send API data to third parties.
- <span class="new">NEW</span> Include prompt-injection fixtures in the benchmark (malicious descriptions and response bodies). The safety-compliance metric must stay at 100% on them.

### 11.5 Operational requirements

- All runs must be traceable by run ID and correlation ID.
- Workers must recover or mark runs safely after process interruption, and still attempt cleanup.
- Idempotency keys must prevent duplicate run creation on client retries.
- Failures in the LLM provider must not corrupt run history. Mark AI-dependent steps as unavailable or retry within policy; replay runs are unaffected.
- Expose worker health, queue depth, request counts, LLM usage/cost and execution latency.

## 12. UX and reporting requirements

### 12.1 Main dashboard

- Summary cards: total operations, operations tested, scenarios executed, findings by severity, latest run status, and <span class="new">cost of last run</span>.
- Coverage view using the dimensions in §8.6, with tested/untested operations and discovered workflows.
- Run activity stream showing agent hypothesis, selected tool, request outcome and next decision.
- Findings list with filters for severity, confidence, confirmation status, expectation source, status, operation and run.
- Regression summary showing new, resolved, recurring, flaky and not-re-tested findings.

### 12.2 Investigation detail

Each investigation provides a chronological timeline: hypothesis created → experiment proposed → policy approved/blocked → request executed → oracles evaluated → observation recorded → confirmation attempted → next experiment selected. Users must be able to inspect the exact sanitized inputs and outputs, plus the model, prompt version and cost of each AI step.

### 12.3 Finding detail

A finding page shows: title, severity, confidence, confirmation status, expectation source, likely fix side, status, affected operation, why it matters, expected versus observed behavior, reproduction workflow, evidence snippets, related findings, suggested remediation, and <span class="new">one-click export (pytest/Hurl/curl) and "promote to replay suite"</span>. Confirmed contract violations are visually separated from AI-generated explanations.

### 12.4 UX quality

- Make run scope, identities and destructive permissions visible before execution.
- Show progress and remaining request/time/cost budget during a run.
- Clearly explain why the agent stopped (`stop_reason`).
- Never show a fabricated percentage such as "100% API tested" without a defined coverage denominator.
- Allow exporting findings as JSON or Markdown. PDF report export can come later.
- <span class="new">NEW</span> CLI output is concise by default (summary table + report path), with `--verbose` for the full timeline.

## 13. Evaluation and success metrics

### 13.1 Evaluation strategy <span class="upd">UPDATED</span>

- **Seeded benchmark:** a local benchmark API with intentionally seeded defects and known expected outcomes, including known-good behavior.
- **External benchmarks** <span class="new">NEW</span>: also run against public intentionally vulnerable APIs (e.g., OWASP crAPI, VAmPI) to avoid overfitting to defects you seeded yourself.
- **Held-out fixtures** <span class="new">NEW</span>: keep a subset of seeded defects that are never used during prompt development, and report on them separately.
- **Repetition:** LLM runs are stochastic. Report each configuration as the mean and spread over at least 5 runs with fixed seeds.
- **Equal budgets:** compare all modes at the same request budget (e.g., 100 and 500 requests).

### 13.2 Core quality metrics

| Metric | Definition | Initial target |
|---|---|---|
| Seeded bug recall | Known seeded defects found / total seeded defects. | At least 60% on the curated benchmark; report held-out recall separately. |
| Finding precision | Confirmed useful findings / all reported findings. | At least 70% after triage on benchmark runs. |
| Reproducibility | Confirmed findings whose replay recreates the failure / confirmed findings. | At least 90% (the confirmation protocol should make this high). |
| Replay determinism <span class="new">NEW</span> | Replay outcomes identical across 5 runs on an unchanged target. | 100% for non-flaky scenarios. |
| Schema validation accuracy | Correct pass/fail decisions on known schema fixtures. | At least 95%. |
| Adaptive value | Additional unique defects found by adaptive exploration vs the best non-AI baseline at equal budget. | Demonstrate measurable positive lift on selected fixtures. |
| Request efficiency | Unique confirmed findings per 100 requests. | Track baseline and improve. |
| Cost per confirmed finding <span class="new">NEW</span> | LLM cost / confirmed findings. | Track; report alongside recall. |
| Time to first finding <span class="new">NEW</span> | Wall-clock time from run start to first confirmed finding. | Under 3 minutes on the demo API. |
| Safety compliance | Requests blocked when outside configured policy, including prompt-injection fixtures. | 100% on automated policy tests. |
| Run traceability | Executed requests with linked run/test/evidence metadata. | 100%. |
| Cleanup rate <span class="new">NEW</span> | Created resources cleaned / created resources with a delete operation. | At least 95%. |

Targets are proposed MVP goals, not guarantees. Report benchmark composition, test seeds, model version and confidence intervals where sample size permits.

### 13.3 Product metrics <span class="new">NEW</span>

These apply once real users exist (including yourself on real projects):

- **Activation:** % of new projects reaching a first confirmed finding within 15 minutes of spec upload.
- **Trust:** triage acceptance rate (findings marked accepted / triaged); false-positive rate by expectation source.
- **Retention signal:** % of confirmed findings exported or promoted to a replay suite; replay suites running in CI weekly.
- **North star:** confirmed, accepted findings per project per week, *excluding* duplicates.

### 13.4 Required benchmark scenarios

- Missing required property and incorrect response schema.
- Negative quantity accepted by an order endpoint.
- Unauthenticated access to a protected endpoint.
- Cross-user access to another test user's resource.
- Invalid state transition, such as cancelling an already completed order.
- Duplicate request causing duplicate resource creation.
- Unexpected behavior after deleting a referenced resource.
- <span class="new">NEW</span> Business-rule violation not expressible in the schema (e.g., order total ≠ price × quantity).
- <span class="new">NEW</span> Mass assignment (client sets a read-only `role` or `price` field).
- <span class="new">NEW</span> Sensitive field leaked in a list response.
- <span class="new">NEW</span> Spec defect: the implementation is correct but the spec documents the wrong status or field.
- <span class="new">NEW</span> Flaky endpoint (intermittent 5xx) to test the confirmation protocol.
- <span class="new">NEW</span> Prompt-injection text in an operation description and in a response body.
- Known-good API behavior to measure false positives.

### 13.5 AI ablation tests <span class="upd">UPDATED</span>

To show that the AI adds value, compare at equal request budgets:

1. Fixed deterministic tests only (generic oracles).
2. **Best open-source baseline** (e.g., Schemathesis with stateful testing enabled) <span class="new">NEW</span>.
3. LLM-generated static test plan (no adaptation).
4. Adaptive agent that selects follow-up tests from observations.
5. Adaptive agent + confirmed invariants (shows the value of the human-in-the-loop oracle).

Compare unique bug discovery, precision, reproducibility, request count, runtime and cost. **Honest reporting matters:** if mode 2 matches mode 4 on generic bug classes, say so, and show where mode 4/5 wins (business logic, authorization, explanation quality). This is a key portfolio demonstration.

## 14. MVP scope and phased roadmap

### 14.1 Weekend MVP: prove the core loop <span class="upd">UPDATED</span>

<div class="callout"><strong>Weekend objective:</strong> from the CLI, load a spec, understand endpoints, generate a hypothesis, execute a real test, observe the response, choose a follow-up, confirm the failure, and produce an evidence-backed finding, then replay it without the LLM.</div>

| Workstream | Weekend deliverable |
|---|---|
| Specification | Load OpenAPI JSON/YAML; parse operations and schemas. |
| Knowledge graph | Basic operation nodes and inferred dependency edges (path params + OpenAPI links). |
| AI planner | Generate a small structured plan with 3–5 hypotheses, each referencing an oracle. |
| Execution | Bounded HTTP calls against a local seeded demo API, with two identities. |
| Oracles | 5xx, schema conformance, documented-constraint enforcement, auth differential. |
| Adaptive loop | Use one observation to select at least one follow-up experiment. |
| Confirmation | Re-execute candidate findings twice; label confirmed/flaky. |
| Investigation | Summarize a failure with request/response evidence. |
| Replay | Freeze the confirmed scenario; `autopilot replay` runs it with no LLM. |
| Interface | CLI + static HTML/Markdown report (no React yet). |
| Persistence | SQLite. |
| Evaluation | Demonstrate at least two seeded defects, one cross-user defect and a known-good case. |

**Explicitly deferred from the weekend:** web UI, PostgreSQL, job queue, retrieval/embeddings, minimization, invariant DSL, exports beyond JSON/Markdown.

### 14.2 Phase 2: 1 to 2 weeks

- Web UI (React) on top of a FastAPI service; PostgreSQL.
- Stateful workflows with full variable extraction, resource ledger and cleanup.
- Boundary and mutation strategy library; optional Schemathesis strategy.
- Invariant DSL with user-declared invariants; AI-proposed invariants with confirmation.
- Finding fingerprints, deduplication and workflow minimization.
- Baseline comparison and regression dashboard with "resolved requires re-execution".
- Auth profiles: bearer, API key, login-flow token acquisition.
- Exports: pytest/Hurl/curl; JUnit and SARIF reports; GitHub Action for replay.
- Improved benchmark harness, external benchmarks and ablation results (including the Schemathesis baseline).

### 14.3 Phase 3: 2 to 3 weeks

- Historical run storage with semantic retrieval.
- Contextual bandit or learned prioritization of test strategies.
- Specialized planner, explorer and investigator agents, only if evaluation justifies separation.
- MCP server for coding agents.
- Scheduled runs, team collaboration, finding triage and notifications.
- Spec-defect classification with spec patch suggestions; spec version diffing.
- Target-side log/trace ingestion for investigation.
- Additional auth flows (OAuth2) and specification edge cases.
- More robust multi-worker execution, observability and deployment.

### 14.4 Explicit MVP exclusions

- Full multi-tenant SaaS billing and organization management.
- Production-scale distributed orchestration.
- Every API protocol beyond HTTP/REST (GraphQL/gRPC are candidates after Phase 3).
- Automatic code patching or pull-request creation.
- Unrestricted internet target scanning.
- Training or fine-tuning a custom foundation model.

## 15. Acceptance criteria

### 15.1 End-to-end acceptance

1. User can create a project and load a valid OpenAPI 3.x file (via CLI in the MVP; UI in Phase 2).
2. System displays parsed endpoints and at least one inferred relationship for the demo API.
3. User configures a local or staging base URL and two identities, and sees the scope and safety policy before execution.
4. System generates a structured hypothesis and test plan with a reason and an oracle reference for each proposed experiment.
5. Every agent-proposed request passes through a policy validator before execution.
6. Execution engine sends real HTTP requests and records sanitized request/response evidence.
7. Oracle evaluation is deterministic and produces a clear pass/fail result.
8. At least one observed response changes the next experiment selected by the agent.
9. System produces a **confirmed** finding (per FR-046) with expected/observed behavior, expectation source and reproducible steps for a seeded defect.
10. User can inspect the agent timeline and understand why the system chose each follow-up.
11. Run terminates with a recorded `stop_reason` when its request, time, cost, iteration or no-progress budget is exhausted.
12. Automated tests prove that disallowed hosts, methods, destructive operations and prompt-injection attempts are blocked.
13. <span class="new">NEW</span> The confirmed finding replays with the LLM adapter disabled and produces the same failure.
14. <span class="new">NEW</span> A cross-user access defect in the demo API is detected via the auth-differential oracle.

### 15.2 Definition of done

- Core flow works from a clean install (`pipx install` + Docker Compose for the demo API).
- README explains setup, supported spec features, safety limitations, LLM data policy and demo workflow.
- At least one seeded demo API and reproducible dataset are included.
- Backend tests cover parser, policy gateway, oracle engine, replay engine and run state transitions.
- AI provider can be mocked so core tests do not require a live API key.
- Model/provider errors are handled without losing run evidence.
- Demo video shows the full loop (explore → confirm → finding → replay), not just a generated answer.
- <span class="new">NEW</span> Benchmark results (with seeds, model version and the non-AI baseline) are committed in `docs/evaluation.md`.

## 16. Risks, mitigations and open questions

### 16.1 Risks

| Risk | Impact | Mitigation |
|---|---|---|
| LLM hallucinates API behavior | False or misleading findings. | Evidence-linked output; deterministic oracles; expectation-source labels; confirmation protocol. |
| Agent repeats unproductive tests | Wasted requests and cost. | Iteration and no-progress budgets, deduplication, stop conditions and strategy tracking. |
| Destructive test data changes | Damaged environment or external side effects. | Read-only default, risk classes, sandbox accounts, approvals, resource ledger, cleanup and reset hooks. |
| Prompt injection in API content | Agent attempts unsafe actions or leaks context. | Treat all external content as untrusted; tool gateway enforces policy; injection fixtures in benchmark. |
| Dependency inference is incorrect | Invalid workflows and confusing results. | Track edge provenance/confidence; use OpenAPI links; allow user confirmation. |
| Model/provider cost or latency | Slow or expensive runs. | Deterministic-first strategies, model tiering, caching, token and cost budgets. |
| Flaky or environment-dependent APIs | False positives and poor reproducibility. | Confirmation protocol, flaky label, circuit breaker, environment metadata. |
| Scope grows beyond weekend | Incomplete product. | CLI-first vertical slice; defer UI and infrastructure. |
| No measurable lift over existing fuzzers <span class="new">NEW</span> | Product thesis unproven. | Benchmark against Schemathesis early; focus on business logic, authorization and explanation, where AI adds the most. |
| "Resolved" reported for untested findings <span class="new">NEW</span> | False confidence in releases. | Resolved requires replay; "not re-tested" status. |
| Spec is wrong, not the API <span class="new">NEW</span> | Noise; developers lose trust. | Fix-side classification; spec quality report; spec patch suggestions. |
| Sensitive data sent to LLM provider <span class="new">NEW</span> | Privacy/compliance breach; adoption blocked. | Metadata-only default, redaction, local-model option. |
| Self-graded benchmark overfitting <span class="new">NEW</span> | Inflated metrics. | External benchmarks, held-out fixtures, multiple seeds. |
| Shared staging overloaded <span class="new">NEW</span> | Disrupts other teams. | Rate limits, Retry-After, circuit breaker, low default concurrency. |

### 16.2 Open product decisions

- Should the first release support bearer-token auth only, or API keys as well?
- Should spec upload be the only input, or should Postman collections/HAR be supported later?
- Should the initial product be local-first or hosted?
- Which LLM provider/model should be the default, and should users bring their own key?
- Should the first release include a lightweight demo API or ask users to connect their own staging service?
- How much raw response data should be retained by default?
- <span class="new">NEW</span> Build generic fuzzing in-house, or wrap Schemathesis as a strategy?
- <span class="new">NEW</span> Open-source license (e.g., Apache-2.0) vs source-available? This affects adoption and contributions.
- <span class="new">NEW</span> Should replay suites live in the user's repository (files) or only in the platform database?
- <span class="new">NEW</span> Default confirmation count N (2 vs 3) and how flaky findings affect CI exit codes.

### 16.3 Recommended decisions for MVP <span class="upd">UPDATED</span>

| Decision | Recommendation |
|---|---|
| Authentication | Bearer token and API key through secret references; at least two identities. |
| Input | OpenAPI 3.x JSON/YAML only. |
| Interface | CLI + static report for the weekend; web UI in Phase 2. |
| Deployment | Local-first; SQLite; Docker Compose for the demo API. |
| LLM | Provider abstraction; one selected provider for the demo, mock adapter for tests, BYO key; `metadata_only` data policy by default. |
| Target | Bundled local e-commerce demo API; optionally user-configured staging target. |
| Data retention | Persist metadata and redacted evidence; configurable response-body retention. |
| Generic fuzzing | Do not rebuild; wrap Schemathesis as an optional strategy in Phase 2. |
| Replay storage | Plain YAML files in the user's repository (`.autopilot/scenarios/`), mirrored in the database. |
| Confirmation | N = 2; flaky findings reported but non-blocking in CI by default. |
| License | Apache-2.0 (open-source core). |

## Appendix A. Example autonomous investigation

**Scenario: negative order quantity.** Demo API: an e-commerce service with users, authentication, products, inventory and orders. The API is a controlled local test environment populated with synthetic data.

1. **Understand:** agent reads the POST /orders schema and identifies `quantity` as an integer with a documented minimum of 1.
2. **Hypothesize:** agent proposes that server-side validation may not enforce the documented minimum (oracle: `spec.constraint.minimum`).
3. **Prepare state:** execution engine selects synthetic identity `user_a`, obtains a test token and creates a product (recorded in the resource ledger).
4. **Experiment:** agent proposes POST /orders with quantity = -1.
5. **Policy check:** gateway verifies host, method, scope, risk class, budget and write permission. If not approved, execution is blocked.
6. **Observe:** API returns 201 and an order with quantity -1.
7. **Validate:** oracle engine detects a mismatch with the documented constraint.
8. **Confirm:** <span class="new">NEW</span> the engine re-executes the minimal sequence twice from a fresh product. Both reproduce, so the finding is marked confirmed.
9. **Follow-up:** agent proposes reading the order as `user_a` and as `user_b`, and checking total price and inventory effects. It also proposes the invariant `total == unit_price * quantity` for user confirmation.
10. **Investigate:** system records the request sequence and evidence, classifies the issue as a likely implementation defect (spec is explicit), and explains the likely validation gap.
11. **Clean up:** <span class="new">NEW</span> created orders and products are deleted; the ledger shows 0 left behind.
12. **Report & promote:** finding is a confirmed contract violation. Broader business impact remains a hypothesis until verified. The scenario is promoted to the replay suite and exported as a pytest test.

**Example finding**

| Field | Example value |
|---|---|
| Title | Order endpoint accepts a negative quantity |
| Severity | Medium (default rubric: documented constraint not enforced) |
| Confidence | High for contract violation; impact requires further verification |
| Confirmation | Confirmed (2/2 reproductions) |
| Expectation source | spec (`components.schemas.OrderCreate.properties.quantity.minimum = 1`) |
| Likely fix side | Implementation |
| Fingerprint | `fp_9c1e…` (createOrder · constraint.minimum · body.quantity) |
| Endpoint | POST /orders |
| Expected | Quantity must be an integer greater than or equal to 1; expect 400/422 |
| Observed | HTTP 201; response contains quantity = -1 |
| Evidence | Sanitized request/response pair, schema rule, run trace, correlation ID |
| Reproduction | Authenticate `user_a`, create/select product, submit order with quantity -1 |
| Suggested remediation | Enforce server-side minimum quantity validation and add a regression test. |
| Export | `pytest` · `hurl` · `curl` · promoted to `.autopilot/scenarios/negative_quantity.yaml` |

## Appendix B. Suggested repository structure <span class="upd">UPDATED</span>

```text
api-autopilot/
├── apps/
│   ├── cli/                       # Typer CLI (weekend MVP entry point)
│   ├── web/                       # React + TypeScript UI (Phase 2)
│   ├── api/                       # FastAPI application (Phase 2)
│   └── mcp-server/                # MCP server for coding agents (Phase 3)
├── services/
│   ├── spec_parser/               # OpenAPI parsing, normalization, quality report
│   ├── knowledge/                 # Endpoint graph, risk classes, retrieval
│   ├── agent_orchestrator/        # Bounded agent state machine, budgets
│   ├── tool_gateway/              # Policy validation for agent actions
│   ├── execution_engine/          # HTTP execution, identities, resource ledger
│   ├── oracles/                   # Built-in oracles + invariant DSL engine
│   ├── replay/                    # LLM-free scenario replay
│   ├── exporters/                 # pytest/Hurl/curl, JUnit, SARIF, Markdown
│   ├── exploration/               # Mutation and test strategies
│   └── evaluation/                # Benchmarks, ablations, metrics
├── packages/
│   ├── shared-schemas/            # Pydantic/API data contracts
│   └── llm-adapters/              # Provider abstraction, local model, mock adapter
├── demo/
│   ├── ecommerce-api/             # Seeded local API (incl. held-out defects)
│   └── openapi.yaml
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── safety/                    # Policy + prompt-injection tests
│   └── benchmark/
├── infra/
│   └── docker-compose.yml
├── docs/
│   ├── architecture.md
│   ├── threat-model.md
│   └── evaluation.md
└── README.md
```

## Appendix C. Built-in oracle catalogue <span class="new">NEW</span>

| Oracle ID | Checks | Phase |
|---|---|---|
| builtin.server_error | Any 5xx response is a finding (severity per rubric). | MVP |
| builtin.schema_conformance | Response body/headers match the documented schema for the returned status. | MVP |
| builtin.undocumented_status | Returned status is not documented for the operation. | MVP |
| spec.constraint.* | Documented request constraints (min, max, enum, required, pattern, maxLength) are enforced: invalid input must not return 2xx. | MVP |
| builtin.auth_required | Protected operations reject unauthenticated requests (401/403). | MVP |
| builtin.auth_differential | Resource created by identity A is not readable/modifiable by identity B (unless documented). | MVP |
| builtin.read_after_write | Values written by create/update are returned by a subsequent read. | Phase 2 |
| builtin.delete_then_404 | Read after successful delete returns 404/410. | Phase 2 |
| builtin.idempotency | Repeated PUT/DELETE yields the same state; duplicate POST with an idempotency key doesn't duplicate. | Phase 2 |
| builtin.mass_assignment | Read-only or undocumented fields sent in a request are not persisted. | Phase 2 |
| builtin.sensitive_exposure | Response contains secrets/PII patterns or fields not present in the schema. | Phase 2 |
| builtin.pagination | Pages are disjoint, complete and stable for an unchanged dataset. | Phase 3 |
| builtin.filter_subset | Filtered list ⊆ unfiltered list; each item satisfies the filter. | Phase 3 |

<div class="pb"></div>

## Final product definition

<div class="callout">API Autopilot is an AI-native autonomous API investigator. It converts API specifications into an executable model, forms hypotheses about potential defects, runs bounded experiments through a deterministic testing engine, adapts based on observed behavior, confirms what it finds, and produces reproducible, evidence-backed findings that become deterministic regression tests. Its long-term value is persistent API quality intelligence, not a chat interface that happens to call an LLM.</div>

**Recommended build strategy:** deliver one complete vertical slice first: spec load → API understanding → AI hypothesis → policy-checked execution → oracle evaluation → adaptive follow-up → confirmation → evidence-backed finding → LLM-free replay. Benchmark it against the best non-AI baseline before expanding.
