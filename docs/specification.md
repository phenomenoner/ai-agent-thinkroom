# Thinkroom v0.2 — Product and Software Design Specification

Status: **FROZEN FOR IMPLEMENTATION**

Deployment amendment (2026-08-28): the native Python package and POSIX/WSL process are the v0.2 production authority. Docker is retained only as operator-owned reference material and is outside the native release claim.

Authority: `thinkroom_ai_think_tank_product_concept.md`

Authority SHA-256: `5fe3d145e5f9cb161f9fb59c0104b83183b376c6f4aab4e643de030da95cb212`

## 1. Outcome and release claim

Thinkroom v0.2 is a self-hostable AI research service that explores important questions through independent lines of inquiry, preserves evidence and uncertainty, runs cross-critique only after independent rollouts finish, and returns a traceable recommendation.

The target claim for this iteration is **DEPLOYABLE_PRODUCTION_CANDIDATE** for a single-node deployment. The claim requires a built artifact, persistent job recovery, bounded runtime controls, health checks, full automated test evidence, and a real local deployment smoke test. It does not claim public-cloud deployment until a deployment target and credentials are supplied.

## 2. Users and primary journeys

1. A decision-maker submits a question and optional context in the Web UI, CLI, Python SDK, REST API, or MCP.
2. Thinkroom frames the question and creates independent perspectives.
3. Each perspective researches without seeing sibling outputs.
4. A separate critique pass compares completed branches.
5. Synthesis returns a recommendation, alternatives, evidence ledger, uncertainties, and next actions.
6. A user can retrieve the durable result after process restart.

Initial domain packs are `generic`, `coding`, and `trading`. Trading is decision support only; it never executes trades. Coding is advisory only; it never changes a target repository.

## 3. Scope

### P0 — required for the release claim

- FRAME → FORK → ROLLOUT → CRITIQUE → SYNTHESIZE workflow.
- Two to six independent branches per research job.
- Structured branch claims, evidence, assumptions, unknowns, and falsifiers.
- Critiques generated only after every branch rollout reaches a terminal state.
- Synthesis that can recommend, combine, reject, or return `NEED_MORE_EVIDENCE`.
- Generic, coding, and trading domain packs.
- Pluggable rollout backend contract.
- Deterministic scripted backend for tests and offline demonstration.
- OpenAI-compatible backend for deployable use.
- Local Prime Agent backend with invocation-local RPC and matching native RLM child evidence per phase.
- Persistent SQLite job store with crash recovery for a single service instance.
- REST API, minimal Web UI, CLI, Python SDK, and MCP tools.
- A packaged Agent Skills set for installation, trigger policy, and operations, plus an idempotent fail-closed installer.
- Cancellation, deadlines, bounded concurrency, input limits, structured logs, liveness/readiness.
- Local-process deployment path from the built Python distribution.

Local-process deployment is the v0.2 release authority. Docker is an operator-owned reference integration: the repository may provide a Dockerfile, smoke scripts, and hardening guidance, but Thinkroom does not claim, certify, publish, or gate v0.2 on an operator's Docker, Docker Desktop, WSL bridge, container network, or CI environment.

### P1 — explicitly deferred

- External web retrieval and citation verification pipeline.
- PostgreSQL and horizontal workers.
- Authentication/RBAC and multi-tenancy.
- Persistent research branches across jobs.
- Automatic code changes, autonomous trading, or autonomous agent interruption.
- Learned meta-controller, self-improvement, or an agent society.

## 4. Functional requirements

### REQ-001 — Submit research

The system SHALL accept:

- `question`: 10–10,000 UTF-8 characters.
- `context`: optional, at most 100,000 UTF-8 characters.
- `domain`: `generic`, `coding`, or `trading`.
- `strategy`: a registered strategy; default `orthogonal`.
- `branch_count`: integer 2–6; default 3.
- optional request deadline; bounded by server configuration.

An API request with the same caller-provided `Idempotency-Key` and same normalized body SHALL return the original job. Reuse with a different body SHALL fail with HTTP 409.

### REQ-002 — Frame and fork

The engine SHALL produce a frame containing the decision to be made, scope, constraints, success criteria, ambiguities, and research questions. It SHALL create exactly `branch_count` distinct perspectives using the selected strategy and domain pack.

### REQ-003 — Independent rollout

Branch rollout inputs SHALL contain the frame, one perspective, user context, domain guidance, and output schema. They SHALL NOT contain sibling branch outputs. Branches MAY execute concurrently subject to `THINKROOM_MAX_CONCURRENCY`.

### REQ-004 — Evidence model

Each branch SHALL return:

- summary and claims;
- supporting and contradicting evidence;
- evidence source/reference when available;
- verification status: `verified`, `unverified`, or `not_applicable`;
- assumptions, uncertainties, falsifiers, and proposed next checks.

Model confidence SHALL NOT be represented as evidence. If no verifiable source is available, evidence must remain `unverified`.

In v0.2, `verified` means checked against a trusted local artifact/tool result or supplied authoritative record whose exact reference is persisted. Model-provided text, model-provided URLs, and externally retrieved content remain `unverified` unless such an independent check occurred. Synthesis SHALL NOT upgrade an evidence item's verification status.

### REQ-005 — Delayed cross-critique

Critique SHALL begin only after every branch in the current attempt is `succeeded` or `failed`. With one or more successful branches, a critic SHALL receive all successful outputs plus metadata for failed branches and identify agreements, contradictions, unsupported claims, blind spots, and discriminating evidence. A failed branch remains visible in the final audit trail. With zero successful branches, the job SHALL fail without critique or synthesis.

### REQ-006 — Synthesis

Synthesis SHALL return:

- disposition: `RECOMMEND`, `COMBINE`, `REJECT_ALL`, or `NEED_MORE_EVIDENCE`;
- recommendation and rationale;
- ranked alternatives;
- evidence ledger with provenance and verification status;
- important disagreements and residual uncertainties;
- falsifiers and concrete next actions.

The synthesizer SHALL be allowed to combine branches; it is not constrained to pick a winner.

### REQ-007 — Persistent job lifecycle

The durable state machine SHALL be:

`queued → framing → rolling_out → critiquing → synthesizing → succeeded`

Any active state may transition to `failed` or `cancelled`. Cancellation is idempotent. Each execution has an immutable `attempt_id`; frames, branches, critiques, syntheses, transitions, and provider calls are attempt-scoped. Startup recovery marks a stranded attempt `abandoned` and creates a fresh attempt that recomputes every phase. Artifacts SHALL never be mixed or reused across attempts. The final result names exactly one attempt and the branch IDs consumed by critique and synthesis. A configurable retry ceiling SHALL prevent infinite restart loops.

Cancellation of a queued job becomes terminal without invoking a backend. Cancellation during execution sets a durable cancellation request, cancels in-process tasks, closes HTTP requests, and terminates a Prime Agent subprocess with a bounded graceful period followed by forced termination. No new phase starts after cancellation; late provider output is discarded by a state guard. Deadline expiry follows the same propagation path but ends `failed` with code `DEADLINE_EXCEEDED`. Partial branch success proceeds to critique and synthesis when at least one branch succeeds; total branch failure ends `failed` with code `NO_SUCCESSFUL_BRANCHES`.

### REQ-008 — Interfaces

- REST: create, inspect/list, cancel, health, and version endpoints.
- Web: submit a question and inspect/poll a complete evidence-rich result.
- CLI: `thinkroom research`, `thinkroom get`, `thinkroom list`, `thinkroom serve`, and `thinkroom mcp`.
- Python: `ThinkroomClient` for remote operation and `Thinkroom` for embedded use.
- MCP: `thinkroom_research`, `thinkroom_get_research`, and `thinkroom_list_research`.
- Agent Skills: packaged install, trigger, and operation skills use the same CLI/REST/MCP contracts.

Long-running REST and MCP submission SHALL return a job handle rather than keep a request open for the full research duration.

### REQ-009 — Backend boundary

A backend SHALL expose one operation accepting `BackendRequestV1` and returning one validated phase result. The request contains `schema_version`, `phase`, `job_id`, `attempt_id`, optional `branch_id`, `prompt_version`, `input`, expected output schema name, deadline, and correlation ID. Phases are `frame`, `fork`, `rollout`, `critique`, and `synthesis`. The application engine owns phase order and persistence; a backend owns provider invocation and raw-output parsing only.

Provider output must be exactly one JSON object (an optional single Markdown JSON fence may be removed) and must validate against the phase schema. On malformed or invalid output, the same phase may be repaired once with bounded validation feedback on the route that produced the invalid output, if the shared three-call physical budget and deadline still permit it. Backend-specific parsing, retry details, credentials, and invocation remain outside the research domain. Supported configuration values are `scripted`, `openai`, `prime_agent`, and `prime_agent_failover`.

`prime_agent_failover` invokes one explicitly configured Prime Agent primary and one explicitly configured fallback under the contract in [Provider resilience and progress](provider-resilience-v0.2.5.md). A fast transient or rate limit may retry the same route once; a timeout skips same-route retry. Availability or an unclassified error may then use the fallback. Output limits, cancellation, fencing, and deadline exhaustion are terminal. Each `(attempt, phase, branch)` is limited to three physical calls across retry, fallback, and route-preserving schema repair. Persisted call evidence reconstructs an attempt-local primary circuit after restart and prevents new primary calls after one timeout or two fast transient failures; rate limits do not contribute. No credential, raw provider error, prompt, or response is persisted.

### REQ-010 — Safety and operations

- User text SHALL be treated as data, not as executable instructions for the host.
- Prime Agent execution SHALL use an argument vector, never a shell string. Each phase SHALL use an
  invocation-local JSONL RPC session with only the built-in IPython tool, no context files, no
  extensions, no prompt templates, and a temporary working/session directory. Thinkroom SHALL
  instruct the parent to call preloaded `rlm(...)` exactly once with one predictably named child and
  SHALL return phase JSON only after the same RPC stream first exposes either a matching legacy
  child `agent_message` or a matching Prime 0.8.1 child-lifecycle series with one stable child ID,
  the requested session name, completed status, and eventual `repliedSinceTask=true`. The field may
  be absent from an early snapshot because it is optional in Prime 0.8.1, but absence SHALL NOT
  establish custody. The parent SHALL retain the original `rlm(...)` admission handle. An exact
  IPython cleanup recipe then boundedly polls for that named child's completed status, rejects any
  unexpected direct child, requires the original admission-handle ID to match the current singleton
  registry entry, removes that exact child from the parent registry, verifies the deletion receipt
  carries the same ID,
  confirms the invocation-local direct-child registry is empty, and emits the expected cleanup marker
  plus that ID from the same correlated tool call. For lifecycle custody, the adapter SHALL match the
  cleanup identity line to the streamed child ID before accepting terminal output. Finally a later
  terminal assistant `message_end` appears and matches the terminal message in `agent_end`. A legacy
  aggregate transcript SHALL contain exactly
  one child message from the requested child and exactly one terminal assistant after that child. A
  Prime 0.8.1 aggregate, which does not repeat lifecycle custody as a custom transcript message, SHALL
  contain no child message and exactly one textual terminal assistant across the aggregate. Both forms
  SHALL be reconciled with the observed lifecycle, cleanup, and terminal order before accepting JSON.
  Cleanup
  before child custody, an unexpected or repeated child or terminal assistant,
  duplicate/replayed cleanup calls or results, any post-cleanup tool execution or child custody, an
  aggregate-only terminal message, and a marker without the matching executed recipe and `toolCallId`
  are not cleanup evidence. Missing lifecycle proof, malformed child fields, an unexpected or
  replaced child, a child without eventual reply proof, and a non-completed child SHALL fail closed
  and SHALL NOT be inferred from a parent answer. Coding
  context is bounded
  request data; the adapter SHALL NOT make a target
  repository the working directory or grant a repository write port.
- Prime RPC input, argv, each LF-delimited event, raw transport bytes, semantic event count,
  discarded `message_update` telemetry event count, retained result/control bytes,
  terminal assistant text, timeout, and cleanup SHALL be bounded. Stderr SHALL be drained without
  retention and SHALL NOT participate in result validity; its task SHALL be cancelled after a valid
  JSONL result. A `message_update` remains untrusted and consumes both the telemetry-event ceiling
  and the raw-transport ceiling, but SHALL NOT consume the lower semantic-event ceiling used for
  lifecycle, tool, response, terminal, and unknown event types. Unknown types SHALL fail safely into
  the semantic accounting class rather than being treated as discardable telemetry. Each output-limit
  site SHALL retain the public backend code `OUTPUT_LIMIT_EXCEEDED` and SHALL attach one bounded,
  non-secret audit status identifying the enforced ceiling. Process isolation and provider-call audit
  SHALL preserve that status; terminal job/API/MCP errors SHALL continue to expose only the stable
  public code and safe message. The production process wrapper SHALL retain same-group descendant custody until
  physical exit. Invalid JSONL, wrong child
  identity, missing or forged child-cleanup evidence, early parent output, failed turns, prompt
  rejection, timeout, cancellation, or premature
  exit SHALL fail as typed backend errors and settle the owned process before capacity is released.
- Prime Agent owns its provider credentials. Thinkroom SHALL NOT copy OAuth tokens or ambient provider
  credentials into its configuration or persistence. IPython/RLM is trusted provider execution, not a
  sandbox; the executable, installed skills, provider account, and host remain operator-owned trust.
- Secrets SHALL be read from environment variables and never logged or returned.
- Input sizes, subprocess output, deadlines, retries, and concurrency SHALL be bounded.
- v0.2 SHALL reject non-loopback bind addresses. Internet-facing access requires a separately tested authenticated reverse-proxy boundary and is not claimed by this release.
- SQLite production mode SHALL acquire an exclusive inter-process service lock before recovery or worker startup. A second service instance SHALL fail startup and never become ready.
- Queue depth, backend response bytes/tokens, persisted bytes per job, total context bytes, deadlines, retries, and concurrency SHALL have validated limits. Capacity rejection uses HTTP 429 / MCP `RESOURCE_EXHAUSTED`; oversized input or output uses HTTP 413 / MCP `INVALID_ARGUMENT`; provider-limit failure is durable and auditable.

### REQ-011 — Bundled Agent Skills product surface

The distribution SHALL package three Agent Skills-compatible skills:

1. `thinkroom-install`: install/configure/verify the product and the skill set.
2. `thinkroom-trigger`: open Thinkroom for consequential, uncertain, multi-hypothesis questions; avoid trivial, deterministic, or low-consequence prompts.
3. `thinkroom-operate`: submit, poll, inspect, cancel, and interpret research jobs through stable public interfaces.

`thinkroom skills install --target <skill-root>` SHALL plan and apply a managed projection with `ADD`, `EXACT`, `UPDATE`, and `DIVERGED` classifications, write a receipt, remain idempotent, and refuse to overwrite unmanaged or diverged targets. `UPDATE` is permitted only when an allowlisted previous receipt and every existing managed payload match their exact historical hashes; unknown receipt lineages, missing owned files, and modified payloads remain `DIVERGED` or invalid without mutation. The equivalent `--profile codex` and `--profile hermes` forms SHALL resolve to `$HOME/.agents/skills` and `$HERMES_HOME/skills` respectively, with Hermes defaulting to `$HOME/.hermes/skills`. Exactly one of `--profile` or `--target` is required. All forms SHALL use the same manifest, payloads, receipt, migration allowlist, and divergence policy; `status` and `uninstall` SHALL verify ownership and exact managed hashes. Skills SHALL contain no machine-local paths, secrets, or hidden dependency on the source checkout.

The wheel SHALL contain this canonical bundle:

```text
thinkroom/bundled_skills/
  manifest.json
  thinkroom-install/
    SKILL.md
    agents/openai.yaml
  thinkroom-trigger/
    SKILL.md
    agents/openai.yaml
  thinkroom-operate/
    SKILL.md
    agents/openai.yaml
```

Each `SKILL.md` starts at byte zero with Agent Skills YAML frontmatter containing `name`, a capability/trigger `description` no longer than 60 characters, semver `version`, human-first `author`, `license`, `platforms`, and tags, followed by a non-empty actionable body. Each optional `agents/openai.yaml` contains bounded Codex presentation and invocation-policy metadata but no machine-local executable path or provider credential. `manifest.json` is schema versioned and contains bundle/product versions plus every managed payload file's POSIX-relative path and lowercase SHA-256; it explicitly excludes itself from the entry list. Paths must be unique, canonical, confined below the bundle root, and free of absolute paths or `..` components.

The installer validates the manifest syntax, the exact payload file set, and all payload hashes before planning. It rejects a symlinked target root, any symlink in a managed source/target path, path traversal, missing/extra payload files, duplicate paths, and malformed manifests or receipts before mutation. The receipt at `<skill-root>/.thinkroom/skills-receipt-v1.json` records receipt version, bundle version, SHA-256 of the exact manifest bytes, and each installed relative path and hash. Apply stages validated bytes and writes the receipt last; status compares manifest, receipt, and target bytes. Uninstall is all-or-nothing and removes only receipt-owned exact files; drift yields `DIVERGED` without deletion.

### 4.1 Versioned phase schemas

- `FrameOutputV1`: decision, scope, constraints, success criteria, ambiguities, research questions.
- `ForkOutputV1`: exactly the requested number of `PerspectiveV1` items, each with ID, title, hypothesis, approach, and differentiator.
- `BranchOutputV1`: summary, claims, supporting/contradicting evidence, assumptions, uncertainties, falsifiers, next checks.
- `CritiqueOutputV1`: agreements, contradictions, unsupported claims, blind spots, discriminating evidence, branch assessments.
- `SynthesisOutputV1`: disposition, recommendation, rationale, ranked alternatives, evidence ledger, disagreements, uncertainties, falsifiers, next actions, source attempt and branch IDs.

Fork normalization compares case-folded titles, hypotheses, and approaches. Duplicate perspectives are regenerated once; if diversity is still insufficient, deterministic domain-pack fallbacks fill the requested count and a provenance warning is recorded. A schema-invalid or unparsable fork is regenerated once, then uses the same deterministic fallback. An output-limit failure on either the initial fork or its one allowed schema-repair response SHALL use the deterministic domain-pack perspectives, SHALL persist an explicit provenance warning, and SHALL NOT retry, cross providers, truncate output, or accept invalid JSON. Output-limit failures in framing, rollout, critique, and synthesis retain their existing fatal or branch-containment semantics.

All schemas forbid unknown fields at provider boundaries and carry `schema_version = 1`. Non-empty text fields are 1–4,000 characters; summaries/recommendations are at most 12,000; evidence references are at most 2,048; ordinary collections contain 1–50 items and next-action lists contain 1–20. `EvidenceV1` contains a required ID matching `^[a-z][a-z0-9_-]{0,63}$`, statement, relationship (`supports` or `contradicts`), optional source label/reference, verification status, and verification basis. Evidence IDs are unique within one branch. `ClaimV1` contains statement and related evidence IDs, each of which must resolve within that branch. `BackendRequestV1.input` is a discriminated union for the named phase. Unknown enum values, fields, empty required collections, duplicate evidence IDs, over-limit text, cross-attempt IDs, or references to absent evidence fail validation. The OpenAPI document is the normative external schema; these phase schemas are normative internal backend contracts.

### 4.2 Initial domain-pack contract

Every pack supplies framing guidance, deterministic fallback perspectives, evaluation criteria, safety language, and prompt version. The minimum v0.2 packs are:

- `generic`: evidence quality, feasibility, reversibility, consequences, and missing information.
- `coding`: correctness, simplicity, maintainability, migration/rollback cost, security, operability, and testability; output remains advisory and must identify needed repository evidence.
- `trading`: thesis quality, out-of-sample validity, robustness, drawdown, regime dependency, execution cost, and alternative explanations; every result is labeled research/decision support and never an execution instruction.

## 5. Architecture

```text
Web / CLI / SDK / MCP / REST
              │
       Application service
              │
  ResearchEngine (domain orchestration)
      ├── DomainPack registry
      ├── Strategy registry
      ├── RolloutBackend port
      └── ResearchRepository port
              │
       Infrastructure adapters
      ├── SQLiteRepository
      ├── ScriptedBackend
      ├── OpenAIBackend
      └── PrimeAgentBackend
```

This is a modular monolith. Domain models and orchestration do not import FastAPI, SQLite, subprocess, or provider SDKs. Interfaces depend inward on application ports.

### Why not distributed workers now?

Long LLM calls require asynchronous jobs and durable state, but v0.2 is a single-node product. A persisted queue plus one bounded in-process worker survives restart and keeps operational dependencies small. Redis/Celery or cloud queues become justified only when multi-instance scheduling or throughput evidence demands them.

## 6. Data model and provenance

- `research_jobs`: immutable normalized request, request hash, current state, timestamps, deadline, cancellation request, terminal error.
- `attempts`: attempt ID/number, backend and model identity, start/end times, outcome, recovery reason.
- `state_transitions`: attempt, from/to state, timestamp, reason, correlation ID.
- `frames`: one immutable result per attempt.
- `branches`: attempt ID, branch ID, perspective, state, structured rollout result, timing, error.
- `provider_calls`: phase, schema/prompt versions, model/backend, timing, retry index, output status and size.
- `critiques`: one result per attempt naming consumed branch IDs.
- `syntheses`: one result per successful attempt naming consumed critique and branch IDs.
- `idempotency_keys`: key, request hash, job id.

JSON payloads are versioned. An empty database receives the current canonical schema. Only the exact canonical v0.2.4 provider-call schema may migrate to v0.2.5: append `route_role`, `effective_timeout_seconds`, and `error_code` columns to `provider_calls`, then re-attest the complete v0.2.5 shape before readiness. Any other existing noncanonical or prerelease schema is rejected before live DDL or other writes and readiness remains false. A future schema migration requires another explicit versioned contract and independent release evidence before readiness may succeed.

Retention defaults to 30 days for completed jobs and never deletes active jobs. Cleanup is bounded per cycle and disabled only by explicit configuration. A job exceeding its persisted-byte budget fails with `ARTIFACT_LIMIT_EXCEEDED`; data already persisted remains available for diagnosis.

## 7. Configuration

Environment variables use prefix `THINKROOM_`. Explicit embedded/CLI configuration overrides environment, which overrides the following defaults:

| Setting | Default | Validated bound |
|---|---:|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///.data/thinkroom.db` | SQLite only in v0.2 |
| `BACKEND` | `scripted` | `scripted`, `openai`, `prime_agent`, `prime_agent_failover` |
| `MAX_CONCURRENCY` | 1 | 1–12 |
| `ROLLOUT_PROVIDER_CONCURRENCY` | 1 | 1–2 |
| `MAX_QUEUED_JOBS` | 100 | 1–10,000 |
| `JOB_SOFT_TIMEOUT_SECONDS` | 900 | 30–7,199 and must leave the configured provider reserve |
| `JOB_TIMEOUT_SECONDS` | 1,200 | 30–7,200 |
| `BACKEND_TIMEOUT_SECONDS` | 180 | 10–1,800 and no greater than job timeout |
| `FAILOVER_PRIMARY_TIMEOUT_SECONDS` | 90 | 10–1,799 and less than backend timeout in failover mode |
| `MAX_JOB_ATTEMPTS` | 2 | 1–5 |
| `MAX_BACKEND_RESPONSE_BYTES` | 1,000,000 | 16,384–10,000,000 |
| `MAX_CONTEXT_BYTES` | 1,000,000 | 16,384–10,000,000 |
| `MAX_BACKEND_OUTPUT_TOKENS` | 8,192 | 256–32,768 |
| `MAX_PERSISTED_BYTES_PER_JOB` | 10,000,000 | 1,000,000–100,000,000 |
| `RETENTION_DAYS` | 30 | 1–3,650 |
| `HOST` / `PORT` | `127.0.0.1` / `8787` | loopback only / 1–65,535 |
| `LOG_LEVEL` | `INFO` | standard levels |

OpenAI-compatible backends enforce `MAX_BACKEND_OUTPUT_TOKENS` in the provider request. Prime Agent has no supported output-token CLI flag, so its adapter converts that setting to four-byte-per-token prompt guidance while `MAX_BACKEND_RESPONSE_BYTES` remains the independent hard ceiling for retained control events and terminal JSON. Historical messages repeated inside an `agent_end` envelope are parsed under the separate raw-transport ceiling but are never retained or charged as terminal output.

Provider settings are `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `PRIME_AGENT_EXECUTABLE`, `PRIME_AGENT_PROVIDER`, `PRIME_AGENT_MODEL`, `PRIME_AGENT_THINKING`, `PRIME_AGENT_FALLBACK_PROVIDER`, `PRIME_AGENT_FALLBACK_MODEL`, and `PRIME_AGENT_FALLBACK_THINKING`. Required settings for the selected backend are validated at startup. Secret values never appear in validation errors.

## 8. API surface

- `POST /api/v1/research` → `202` with job resource; `Idempotency-Key` supported.
- `GET /api/v1/research` → paginated newest-first jobs.
- `GET /api/v1/research/{job_id}` → complete job and result.
- `DELETE /api/v1/research/{job_id}` → cancellation request.
- `GET /health/live` → process liveness.
- `GET /health/ready` → store and worker readiness.
- `GET /api/v1/version` → package and schema versions.
- `GET /` → Web UI.

All error bodies use `{ "code", "message", "details" }`. Unknown IDs return 404, validation 422, idempotency conflicts 409, and unavailable dependencies 503.

The OpenAPI 3.1 document generated from checked-in typed models is part of the release artifact and contract-tested. Create responses contain `job_id`, `state`, `created_at`, and canonical resource URL. Detail responses contain request metadata, attempt/state audit data, branches, critique, synthesis, and terminal error without secrets or raw provider prompts.

Idempotency normalization is canonical UTF-8 JSON over validated request fields with sorted keys and omitted defaults filled before SHA-256 hashing. Keys are scoped to the service, 1–128 printable ASCII characters, and retained as long as their job. List pagination uses an opaque base64url cursor over `(created_at, job_id)`, a limit of 1–100, and stable newest-first ordering. Cancellation returns the current resource: `202` while propagation is pending and `200` for an already terminal job.

CLI/SDK default to `http://127.0.0.1:8787`; an explicit endpoint overrides the environment variable `THINKROOM_ENDPOINT`. MCP uses stdio by default and acts as a client of that endpoint; an embedded mode must be explicitly selected and acquire the same exclusive service lock.

`/health/live` reports only process event-loop liveness. `/health/ready` returns 200 only after configuration validation, migrations, exclusive lock acquisition, startup recovery, worker startup, and selected backend configuration validation; otherwise it returns 503 with non-secret failed predicate names.

## 9. Verification and acceptance

### AC-001 — happy-path evidence synthesis

Given a scripted backend and three branches, an end-to-end job reaches `succeeded`; every branch is independently prompted; critique occurs after rollouts; synthesis contains disposition, provenance, uncertainty, and next actions.

### AC-002 — honest low-evidence result

Given rollouts with only unverified claims, synthesis can return `NEED_MORE_EVIDENCE` and never upgrades evidence to `verified`.

### AC-003 — crash recovery

Given a durable job stranded in an active state, a new service process re-queues it, increments attempt count, and eventually reaches a terminal state without duplicating the job.

### AC-004 — idempotency

Same key plus same body returns one job; same key plus different body returns 409.

### AC-005 — branch isolation

Captured branch prompts contain no sibling output. Critique is not invoked until all rollout tasks complete or fail.

### AC-006 — bounded failure

Timeout, malformed model JSON, cancellation, and partial branch failure produce a terminal auditable state; no task remains active forever.

### AC-007 — interface contract

REST, CLI, embedded Python, Web, and MCP all create or inspect the same durable job model; contract tests cover schema and error semantics.

### AC-008 — deployable artifact

A clean POSIX/WSL environment can install the built package, start the native production command, pass readiness, submit a scripted smoke job, retrieve its result, stop, restart, and retrieve the same result.

### AC-009 — bundled Agent Skills

The built distribution contains all three skills. In an isolated skill root, install reports `ADD`, repeat install reports `EXACT`, an exact allowlisted previous receipt reports `UPDATE`/`ADD` and migrates to `EXACT`, manual drift reports `DIVERGED` without overwrite, status verifies hashes, uninstall removes only exact managed files, and a fresh agent session can load the trigger and operation skills and choose Thinkroom for a qualifying prompt.

### AC-010 — production limits and ownership

A second service instance cannot acquire worker ownership; readiness/public bind/resource-limit tests fail closed with the specified state or error. Attempt recovery never mixes artifacts, and provenance identifies every branch consumed by synthesis.

### AC-011 — Prime RPC output-limit discrimination and containment

Given a valid Prime RPC lifecycle with more discardable `message_update` events than the semantic-event ceiling, the phase succeeds while remaining below the separate telemetry and raw-byte ceilings. Exceeding the semantic-event, telemetry-event, raw-byte, per-event, control-byte, or terminal-text ceiling fails with public code `OUTPUT_LIMIT_EXCEEDED`, preserves the corresponding safe audit status through process isolation and physical provider-call settlement, and never makes that code eligible for provider failover. An initial fork output-limit failure completes with deterministic perspectives and an output-limit-specific provenance warning; the same failure in framing remains terminal.

### Required gates

1. formatter and linter;
2. static type check;
3. unit, integration, contract, and end-to-end tests;
4. package build and installation smoke; the wheel production claim additionally requires same-release `requirements-production.txt`, `uv.lock`, and the flat GitHub Release asset `verify_locked_runtime.py`, with dependencies installed under `--require-hashes` and the wheel installed with `--no-deps`;
5. security-oriented configuration and subprocess tests;
6. packaged-skill validation plus isolated install/load/operation smoke;
7. independent specification and code review.

Package/local-process smoke is mandatory. Docker availability, execution, or smoke status is non-authoritative and does not affect native release authorization. If an operator chooses the reference Docker integration, that operator owns bridge completion and verification against the documented loopback, non-root, persistence, health, resource, sibling-isolation, and cleanup checks.

## 10. Release boundaries

A passing scripted backend proves orchestration and operations, not model quality. A real backend smoke proves integration, not epistemic superiority. The product-value hypothesis—multi-branch evidence synthesis beats a single answer—requires a later evaluation dataset and comparative study; v0.2 preserves the data needed for that study without fabricating the conclusion.
