# Thinkroom

Thinkroom is a single-node AI research service that frames a question, forks independent perspectives, performs isolated rollouts, delays critique until all branches finish, and synthesizes traceable evidence.

This public repository is **source-available, not open source**. See [LICENSE](LICENSE) before copying, modifying, redistributing, hosting, or using it in production.

## Run

```bash
uv lock --check
uv sync --locked --all-extras --dev
install -d -m 0700 .data
uv run thinkroom serve
uv run thinkroom research --question "Should we adopt this design?" --idempotency-key demo-001
```

The commands above create a development environment. For the release-authorized production install, download the wheel, `requirements-production.txt`, `uv.lock`, and the flat `verify_locked_runtime.py` asset from the same GitHub Release, then install the exact locked closure before installing the wheel without dependency resolution:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-production.txt
uv pip install --python .venv/bin/python --no-deps thinkroom-0.1.0-py3-none-any.whl
.venv/bin/python verify_locked_runtime.py uv.lock --write-manifest runtime-lock-manifest.json
```

An ordinary dependency-resolving wheel install is convenient for evaluation but is not the locked production closure. The default backend is deterministic `scripted`. Configure `THINKROOM_BACKEND=openai` or `prime_agent` for provider integrations. After the exact v0.1 tag and release are published, the release-authorized deployment path is the installed Python package running as a native POSIX process (Linux, or WSL on Windows). Production defaults bind to loopback and SQLite is single-instance only. The database parent directory must already exist on a Linux/POSIX filesystem, be owned by the effective service user (or root), and not be group/world-writable; Thinkroom never creates it during startup.

REST callers send `Idempotency-Key`; the Python SDK uses `ThinkroomClient.research(..., idempotency_key="demo-001")`, the CLI uses `--idempotency-key`, and the MCP `thinkroom_research` tool exposes `idempotency_key`. Reusing a key with a different request fails with `IDEMPOTENCY_CONFLICT`.

## Smoke tests

After installing the wheel, run `python scripts/smoke_package.py`. For the required native production-process smoke, run `python scripts/smoke_process.py`.

Docker is optional operator-owned reference material, not part of the v0.1 native release claim. Operators may use the supplied Dockerfile and smoke scripts as a starting point, but must complete and verify their own Docker Desktop/WSL/CI bridge. At minimum, publish only on host loopback (`-p 127.0.0.1:8787:8787`), verify `/health/ready`, non-root execution, UID-owned mode-0700 `/data`, resource bounds, sibling-container denial, persistence, and cleanup. v0.1 has no authentication, RBAC, or multi-tenancy; use a separately secured authenticated reverse proxy for any wider access.
