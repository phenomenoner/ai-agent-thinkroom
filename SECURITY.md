# Security Policy

## Supported version

Security fixes are considered for the latest tagged release only.

## Deployment boundary

Thinkroom v0.2 has no authentication, authorization, RBAC, multi-tenancy, or public-ingress protection. After the exact v0.2 tag and release are published, the release-authorized deployment path is the installed Python package running as a native POSIX process on Linux or WSL. Bind only to a literal loopback IP and place any wider access behind a separately secured authenticated reverse proxy.

The Prime Agent RLM backend enables model-generated Python inside Prime Agent's built-in IPython
runtime. Invocation-local directories, bounded RPC, disabled context files/extensions/templates, and
process cleanup limit coupling and lifetime; they do not sandbox the service account. Treat the Prime
Agent executable, installed skills, OAuth/provider account, and host as trusted infrastructure.

SQLite support is single-instance. Store the database on a POSIX filesystem whose ownership and mode semantics satisfy Thinkroom's fail-closed custody checks.

Docker files are operator-owned reference material. Thinkroom does not certify an operator's Docker Desktop, WSL/CI bridge, container network, persistence, resource, or cleanup implementation.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, tokens, connection strings, private data, or unredacted logs. Use GitHub's private vulnerability reporting or Security Advisory interface for `phenomenoner/ai-agent-thinkroom`. Include the affected version, bounded reproduction steps, impact, and a redacted trace. If private reporting is unavailable, open a minimal public issue requesting a private contact channel without disclosing the vulnerability.

## Sensitive information

Never include API keys, tokens, passwords, secrets, credentials, or connection strings in reports, examples, screenshots, or logs. Replace any that appear with `[REDACTED]`.
