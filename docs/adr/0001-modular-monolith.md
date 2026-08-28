# ADR 0001: Python modular monolith with durable single-node jobs

- Status: Accepted
- Date: 2026-08-26

## Context

Thinkroom must iterate quickly while supporting long-running model calls, independent rollouts, delayed critique, restart recovery, several interfaces, and replaceable model backends. There is no existing code or operational platform contract.

## Decision

Use Python 3.12 and a modular monolith:

- FastAPI for REST and server-rendered/static Web delivery.
- Pydantic v2 at external and backend boundaries.
- `aiosqlite` behind a repository port for single-node persistence.
- An asyncio worker with bounded concurrency and startup recovery.
- Typer for CLI.
- FastMCP for MCP.
- `httpx` for OpenAI-compatible HTTP.
- `uv` and `hatchling` for reproducible dependency and package workflows.
- pytest, pytest-asyncio, Ruff, and mypy for gates.

Domain orchestration remains framework-independent. Provider, database, subprocess, HTTP, CLI, and MCP concerns are adapters.

## Consequences

### Positive

- One process and one artifact are fast to build, operate, and debug.
- Persistent jobs avoid request timeouts and survive process restart.
- Explicit ports preserve a path to PostgreSQL, external workers, and additional providers.
- Python/IPython-friendly development supports fast experimentation.

### Negative

- The release is single-instance; SQLite cannot safely coordinate several service replicas.
- In-process workers have lower throughput and fewer scheduling controls than a dedicated queue.
- Pydantic models crossing several interfaces require disciplined versioning.

## Rejected alternatives

- **Synchronous request execution:** operationally fragile for minute-scale model calls.
- **Celery/Redis immediately:** extra failure domains without evidence of required scale.
- **Microservices:** boundaries would be speculative and deployments needlessly complex.
- **Notebook as runtime:** excellent for experiments, poor as the production authority. Notebooks may consume the Python SDK instead.
- **Vendor-specific agent framework as the core:** violates backend replaceability and makes epistemic workflow policy depend on a provider runtime.

## Revisit triggers

Adopt an external queue and PostgreSQL when any of these become real requirements: multiple replicas, more than one concurrent worker host, tenant isolation, queue prioritization, or sustained throughput that one process cannot meet.
