# Changelog

All notable changes to Thinkroom are documented here.

## 0.2.6 — Unreleased

### Fixed

- Prime RPC now accounts repeated cumulative `message_update` snapshots through a narrowly
  allowlisted projection while retaining an independent absolute raw-wire ceiling. This prevents
  non-authoritative telemetry amplification from exhausting the ordinary transport budget without
  weakening lifecycle, custody, ordering, event-count, timeout, or terminal-output checks.
- Provider-call audit rows preserve content-free transport metrics when an otherwise successful
  Prime result fails phase-schema validation, improving diagnosis of malformed provider output.
- Exact canonical v0.2.5 SQLite databases can migrate to v0.2.6 by adding the accounted-transport
  counter; earlier v0.2.4 databases retain the existing sequential migration path, with the complete
  DDL chain and final attestation committed or rolled back as one transaction.
- Managed Skills now recognize the exact released v0.2.5 six-file receipt and migrate it directly
  to the v0.2.6 bundle; modified payloads and unknown receipts still fail closed before mutation.

## 0.2.5 — 2026-09-01

### Added

- Research detail responses now expose snapshot-labelled, derived provider progress with phase,
  route, elapsed time, timeout remaining, call-budget use, an evidence watermark, and explicit
  active, slow, degraded, stalled, and presumed-dead classifications.
- Jobs preserve a typed partial-evidence artifact when the soft deadline prevents queued phases or
  rollout branches from starting. Partial evidence is explicitly not a completed synthesis.
- Prime RPC provider-call evidence includes content-free byte and event counters on success and
  typed failure so operators can distinguish transport amplification without retaining model text.

### Changed

- Provider availability handling now shares a hard three-call budget across one fast same-route
  retry, one fallback, and one route-preserving schema repair. Timeouts skip same-route retry;
  semantic/result output-limit failures remain terminal. A primary raw-transport limit skips retry,
  opens the attempt-local primary circuit, and may use the configured fallback once.
- A persisted, attempt-local circuit skips new primary calls after one primary timeout or two fast
  transient failures, including producer-affine schema repair. HTTP 429 is retried within the call
  budget but does not open the circuit.
- Independent rollout-call concurrency is configurable from one through two and remains one by
  default. Non-rollout phases and the default active-job limit remain one.
- Existing canonical v0.2.4 SQLite databases are migrated in place with route role, effective
  timeout, normalized error-code evidence, and content-free Prime RPC transport counters needed for
  recovery-correct progress, routing, and output-volume diagnosis.

## 0.2.4 — 2026-09-01

### Fixed

- Prime Agent RPC telemetry no longer consumes the semantic event budget used for research output.
  Raw transport bytes, telemetry events, semantic events, control bytes, final text, process
  envelopes, and validated responses now fail closed under separate bounded limits with stable
  `OUTPUT_LIMIT_EXCEEDED` audit subtypes.
- Provider output-limit failures during initial fork generation or schema repair now use the existing
  deterministic fork fallback instead of aborting the complete research run. Other provider and
  timeout failures keep their previous failover behavior.
- Managed Skills now migrate directly from the exact six-file v0.2.3 receipt to the v0.2.4 bundle;
  modified payloads and unknown, malformed, duplicate, or foreign receipt authorities still fail
  closed before mutation.

## 0.2.3 — 2026-09-01

### Fixed

- Managed Skills now migrate directly from the exact six-file v0.2.1 and v0.2.2 receipts while
  continuing to reject unknown receipts and modified payloads without mutating the target tree.

## 0.2.2 — 2026-08-31

### Fixed

- Prime Agent 0.8.1 RLM custody now uses the runtime's stable child lifecycle snapshots and
  `repliedSinceTask` signal when custom child messages are absent from the RPC transcript. The
  adapter binds the original admission handle, streamed snapshot, current registry child, and
  cleanup receipt to one child ID. It still requires completed exact-child evidence, exact cleanup,
  a post-cleanup terminal, and aggregate reconciliation; unexpected, replaced, non-replying, or
  incomplete children fail closed.

## 0.2.1 — 2026-08-30

### Added

- Codex App and Hermes Agent installation profiles over one receipt-owned Agent Skills bundle.
- Codex App `agents/openai.yaml` metadata without forking trigger or operation policy.
- A host-integration guide covering Codex Windows/WSL config sharing and Hermes named profiles.

### Changed

- Prime Agent availability failures can use one explicitly configured sequential fallback while
  preserving per-call audit evidence, cancellation/deadline boundaries, attempt policy identity, and
  fail-closed interrupted-call recovery.
- The MCP research tool now exposes question/context bounds, the exact domain enum, and branch-count
  bounds in its callable schema; the operation skill maps descriptive research categories to the
  `generic` default and preflights idempotency keys before submission. Managed skill migration also
  recognizes the exact CRLF form of the historical text-only v0.2 Windows checkout.
- Prime/RLM phases now delete the completed named child from the parent registry after child-message
  custody and before accepting terminal phase JSON; a matching executed cleanup recipe and marker are
  required. Terminal acceptance also reconciles a post-cleanup standalone assistant event with the
  aggregate transcript and rejects aggregate-only or repeated terminal output plus unexpected child
  custody. This is a bounded lifecycle workaround, not a producer-side RPC transport fix.
- The managed Skills installer recognizes the exact pre-profile v0.2 receipt, adds Codex metadata,
  updates changed owned payloads, and publishes the current receipt while preserving fail-closed
  behavior for unknown, malformed, or modified installations and preserving concurrent replacements
  during rollback.
- Installed wheels expose `thinkroom verify package` and `thinkroom verify process`; CI builds the
  deterministic candidate, installs its hash-locked closure, and runs both verification commands.

### Security boundary

- Prime child deletion writes a Prime-owned tombstone and removes the child from messaging and
  observation, but does not erase Prime transcript/artifact bytes already written or RPC bytes already
  transported.
- Prime RPC now has an independent 64 MB raw transport cap and 20,000-record ceiling in addition to
  retained semantic/result limits.

## 0.2.0 — 2026-08-29

### Added

- Native Prime Agent RLM execution through invocation-local JSONL RPC sessions.
- One matching RLM child and child `agent_message` are required before accepting phase JSON.
- Bounded RPC event parsing, terminal-result limits, timeout/cancellation cleanup, and temporary
  session ownership.
- Prime RPC stderr is drained without retention and cannot race or override validated JSONL results.
- Full Traditional Chinese README and a public Prime RLM architecture decision.

### Changed

- Relicensed Thinkroom and its bundled Agent Skills under the MIT License.
- Reworked the public README around user value, decision triggers, and Hermes/MCP onboarding.
- Prime Agent authentication remains in Prime Agent's credential store; Thinkroom accepts only an
  executable and optional provider/model/thinking routing settings.
- The bundled operation skill now documents strict MCP request values, one corrected retry after
  `INVALID_ARGUMENT`, and supervisor-owned blocking waits for asynchronous jobs.

### Security boundary

- The RLM adapter enables Prime Agent's built-in IPython tool but disables context files,
  extensions, and prompt templates and runs in an invocation-owned temporary directory.
- IPython/RLM is not a sandbox. The Prime Agent executable, installed skills, provider account, and
  service host remain trusted infrastructure with the service account's operating-system authority.

## 0.1.0 — 2026-08-28

### Added

- Durable single-node research lifecycle: FRAME → FORK → isolated ROLLOUT → evidence → delayed CRITIQUE → SYNTHESIZE.
- Scripted, OpenAI-compatible, and Prime Agent backends behind typed ports.
- REST API, Web UI, CLI, Python SDK, MCP tools, and bundled Agent Skills.
- SQLite restart recovery, bounded concurrency, cancellation, deadlines, provenance, and typed public errors.
- Deterministic wheel and sdist builder with locked build dependencies and artifact integrity checks.
- Native POSIX/WSL package-and-process production path.
- Operator-owned Docker reference files and verification guidance; Docker is not part of the native release claim.

### Security boundary

- Loopback-only defaults; v0.1 has no authentication, RBAC, multi-tenancy, or public-ingress protection.
- SQLite support is single-instance and requires POSIX ownership and mode semantics.

### Original license

- The initial v0.1.0 release was published under the repository's then-current source-available license and was subsequently relicensed under MIT in v0.2.0.
