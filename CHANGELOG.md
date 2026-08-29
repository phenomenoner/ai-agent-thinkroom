# Changelog

All notable changes to Thinkroom are documented here.

## Unreleased

## 0.2.0 — 2026-08-29

### Added

- Native Prime Agent RLM execution through invocation-local JSONL RPC sessions.
- One matching RLM child and child `agent_message` are required before accepting phase JSON.
- Bounded RPC event parsing, terminal-result limits, timeout/cancellation cleanup, and temporary
  session ownership.
- Prime RPC stderr overflow fails closed even when stdout contains an otherwise valid result.
- Full Traditional Chinese README and a public Prime RLM architecture decision.

### Changed

- Relicensed Thinkroom and its bundled Agent Skills under the MIT License.
- Reworked the public README around user value, decision triggers, and Hermes/MCP onboarding.
- Prime Agent authentication remains in Prime Agent's credential store; Thinkroom accepts only an
  executable and optional provider/model/thinking routing settings.

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
