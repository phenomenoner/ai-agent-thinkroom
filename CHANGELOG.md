# Changelog

All notable changes to Thinkroom are documented here.

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

### License

- Public source-available release under the repository's proprietary `LICENSE`; this is not an open-source license.
