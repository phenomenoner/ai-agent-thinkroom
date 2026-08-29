# Repository instructions

## Authority

Implement `docs/specification.md`. Preserve the product semantics in `thinkroom_ai_think_tank_product_concept.md`. Do not silently reduce P0 scope or release gates.

## Engineering method

- Use strict RED → GREEN → REFACTOR for behavior changes.
- Keep domain and application layers independent of FastAPI, SQLite, subprocess, provider SDKs, CLI, and MCP.
- Prefer small cohesive modules and explicit typed ports over framework magic.
- Treat model output as untrusted input: validate schemas, bound retries, and preserve evidence verification status.
- Never use `shell=True` or interpolate user input into commands.
- Do not add distributed infrastructure without an observed need.
- Keep production defaults loopback-only and fail closed on unsafe public binding while authentication is absent.

## Required gates

The implementation is not complete until these pass from a clean checkout:

```bash
uv lock --check
uv sync --locked --all-extras --dev
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run pytest
python3 scripts/build_release.py --out-dir /absolute/external/empty/dist
```

Add package-install and native production-process smoke tests/scripts. A production wheel release must include `requirements-production.txt`, `uv.lock`, and the flat asset `verify_locked_runtime.py`; install hash-locked dependencies first and the wheel with `--no-deps`. Docker is an operator-owned reference integration, not part of the v0.2 native release claim. Docker availability or smoke status does not gate the native release.

## Claim discipline

A scripted backend proves orchestration, not model quality. A real provider smoke proves integration, not correctness of the research. SQLite release support is single-instance only.
