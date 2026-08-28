# ADR 0002: Native process is the release authority

- Status: Accepted
- Date: 2026-08-28
- Decision owner: CK
- Amends: deployment and verification portions of `docs/specification.md`

## Context

Thinkroom's product invariants are the research lifecycle, durable single-node recovery, bounded provider execution, typed interfaces, and replaceable backends. None requires a container runtime. The Python package already supplies the CLI, embedded SDK, server, MCP surface, process isolation, SQLite custody, and Prime Agent integration.

Docker added a second supervisor, proxy, filesystem projection, network policy, Windows/WSL bridge, resource ownership model, and cleanup lifecycle. Those mechanisms can help an operator, but making them release authority caused Docker Desktop and bridge failures to block otherwise valid native product bytes.

## Necessity decision

**Decision: `DIRECT`.** The required production path is the built Python wheel running as a native POSIX process on Linux, or inside WSL on Windows.

- Delete Docker entirely: rejected because the existing Dockerfile and smoke scripts remain useful operator reference material.
- Require manual container certification: rejected because container operation is not needed for the product outcome.
- Embed container orchestration in the product: rejected as an unrelated authority and failure domain.
- Use the host's native Python/process/filesystem primitives: selected; these already enforce every signed v0.1 runtime invariant.
- Retain Docker as a release mechanism: rejected because it adds no required product capability.

## Decision

1. Wheel build/install, native process start/readiness, durable restart, package/Skills closure, and real-provider integration are release-authoritative.
2. Docker is operator-owned reference material. Thinkroom may provide a Dockerfile, Linux/PowerShell smoke scripts, and a hardening checklist, but does not certify an operator's Docker Desktop, WSL bridge, container network, storage, or CI runner.
3. Docker availability, absence, failure, or skipped smoke does not change native release authorization.
4. Anyone claiming a Docker deployment must independently verify loopback-only publication, literal-loopback Host behavior, non-root execution, UID-owned mode-0700 data storage, read-only root, bounded resources, health, persistence, sibling isolation, and ownership-checked cleanup.
5. Docker-specific static and unit regressions remain to keep the reference adapter internally coherent. They do not turn it into an official deployment target.

## Consequences

### Positive

- Release evidence matches the minimum product mechanism.
- WSL users can operate Thinkroom like Prime Agent minion without Docker Desktop.
- Docker bridge failures no longer masquerade as product failures.
- Optionality First applies to deployment as well as providers and interfaces.

### Negative

- Thinkroom v0.1 does not promise native Windows execution outside WSL.
- Operators choosing Docker own environment-specific completion and verification.
- Container receipts cannot substitute for native process evidence.

## Verification

Required release evidence:

- clean deterministic wheel/sdist build;
- isolated wheel install and package smoke;
- native production process readiness, submit, terminal result, restart, and readback;
- locked production dependency closure;
- required local/static/security tests;
- real Prime Agent integration smoke;
- independent exact-tree review.

Docker evidence, when present, is reported as non-authoritative operator/reference evidence only.
