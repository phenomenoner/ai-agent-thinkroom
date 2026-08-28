# Thinkroom

**English** · [繁體中文](README.zh-TW.md)

**Turn one hard question into multiple independent lines of inquiry—and one evidence-linked answer.**

Thinkroom is a local-first, single-node AI research service for decisions that deserve more than one model pass. It frames the question, forks isolated perspectives, waits until every branch finishes before critique begins, and synthesizes the result with traceable evidence.

## Why Thinkroom

A normal AI answer follows one path. Thinkroom deliberately creates several:

- **Reduce anchoring.** Independent branches cannot see or imitate one another during rollout.
- **Delay groupthink.** Critique starts only after every perspective has committed its result.
- **Keep the reasoning inspectable.** Evidence, provenance, verification status, and job state remain attached to the final synthesis.
- **Fit your existing agent stack.** Use the Web UI, CLI, Python SDK, REST API, MCP tools, or bundled Agent Skills.
- **Start locally.** The deterministic `scripted` backend lets you test the full orchestration flow without provider credentials.

## When to use it

Thinkroom is a strong fit when the question is:

- consequential enough that a shallow answer would be risky;
- uncertain, contested, or supported by competing hypotheses;
- improved by independent technical, product, operational, or adversarial perspectives;
- expected to produce an auditable recommendation rather than a disposable chat response.

Skip Thinkroom for trivial lookups, deterministic calculations, simple rewrites, or low-consequence prompts where one direct tool call is enough.

## How it works

```text
FRAME → FORK → ISOLATED ROLLOUTS → EVIDENCE → DELAYED CRITIQUE → SYNTHESIZE
```

1. **Frame** the question and constraints.
2. **Fork** independent perspectives.
3. **Research in isolation** so branches do not converge prematurely.
4. **Collect evidence** with provenance and verification status.
5. **Critique after completion**, never while branches are still forming their views.
6. **Synthesize** the strongest supported conclusion and preserve dissent.

## What’s included

| Surface | What it gives you |
| --- | --- |
| Web UI | Submit a question and inspect an evidence-rich result. |
| CLI | Research, inspect, list, cancel, serve, and run MCP. |
| Python SDK | Remote `ThinkroomClient` and embedded `Thinkroom` usage. |
| REST API | Stable job-oriented integration with idempotency support. |
| MCP | `thinkroom_research`, `thinkroom_get_research`, `thinkroom_list_research`, and `thinkroom_cancel_research`. |
| Agent Skills | Installation, trigger policy, and operations guidance for compatible agents. |
| Backends | Deterministic `scripted`, OpenAI-compatible, and Prime Agent adapters behind typed ports. |

## Quick start from source

Requirements: Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv lock --check
uv sync --locked --all-extras --dev
install -d -m 0700 .data
uv run thinkroom serve
```

In another terminal:

```bash
uv run thinkroom research \
  --question "Should we adopt this design?" \
  --idempotency-key demo-001
```

The default service listens on `127.0.0.1:8787` and uses the deterministic `scripted` backend.

## Install the bundled Agent Skills

Thinkroom ships three managed Agent Skills:

| Skill | Load it when… |
| --- | --- |
| `thinkroom-trigger` | deciding whether an important, uncertain, multi-hypothesis question merits Thinkroom; do not load it for trivial or deterministic work. |
| `thinkroom-operate` | submitting, polling, inspecting, cancelling, or interpreting a Thinkroom job through the CLI, REST API, or MCP. |
| `thinkroom-install` | installing, checking, or removing the managed Thinkroom skill projection. |

Install them into a compatible skill root, then verify the managed hashes:

```bash
uv run thinkroom skills install --target ~/.hermes/skills
uv run thinkroom skills status --target ~/.hermes/skills
```

The installer is idempotent and refuses to overwrite unmanaged or diverged files.

## Connect Thinkroom to Hermes through MCP

Start the service, then register Thinkroom’s stdio MCP server:

```bash
hermes mcp add thinkroom --command "$(pwd)/.venv/bin/thinkroom" --args mcp
hermes mcp test thinkroom
```

Start a fresh Hermes session or reload MCP discovery after configuration. Hermes exposes the registered tools with the `mcp_thinkroom_` prefix.

If port `8787` is already in use, run the service on another loopback port and pass the matching endpoint to the MCP subprocess:

```bash
THINKROOM_PORT=18788 uv run thinkroom serve
THINKROOM_ENDPOINT=http://127.0.0.1:18788 uv run thinkroom mcp
```

## Provider backends

The default backend is `scripted`, which proves orchestration mechanics—not model quality. For provider integrations, configure one of:

```bash
export THINKROOM_BACKEND=openai
export THINKROOM_OPENAI_API_KEY=...
```

or:

```bash
export THINKROOM_BACKEND=prime_agent
export THINKROOM_PRIME_AGENT_EXECUTABLE=...
```

See [Operations](docs/OPERATIONS.md) for the exact runtime contract and environment variables.

## Idempotent jobs

Long-running submissions return a job handle. REST callers send `Idempotency-Key`; the Python SDK uses `ThinkroomClient.research(..., idempotency_key="demo-001")`; the CLI uses `--idempotency-key`; and MCP exposes `idempotency_key` on `thinkroom_research`.

Reusing the same key with a different request fails with `IDEMPOTENCY_CONFLICT`.

## Release-authorized production install

For the release-authorized production path, download the wheel, `requirements-production.txt`, `uv.lock`, and `verify_locked_runtime.py` from the **same GitHub Release**. Install the exact locked dependency closure before installing the wheel without dependency resolution:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-production.txt
uv pip install --python .venv/bin/python --no-deps thinkroom-0.1.0-py3-none-any.whl
.venv/bin/python verify_locked_runtime.py uv.lock --write-manifest runtime-lock-manifest.json
```

An ordinary dependency-resolving wheel install is convenient for evaluation, but it is not the locked production closure. The release-authorized deployment is the installed Python package running as a native POSIX process on Linux, or inside WSL on Windows.

## Operations and security boundary

Thinkroom v0.1 is intentionally a single-node service:

- production defaults bind to literal loopback;
- SQLite supports one service instance only;
- the database parent must already exist on a Linux/POSIX filesystem, be owned by the effective service user (or root), and not be group/world-writable;
- v0.1 has no authentication, RBAC, or multi-tenancy;
- wider access requires a separately secured, authenticated reverse proxy.

Docker is optional operator-owned reference material, not part of the v0.1 native release claim. If you use it, publish only on host loopback—for example `-p 127.0.0.1:8787:8787`—and complete the hardening checklist in [Operations](docs/OPERATIONS.md).

## Verification

After installing the wheel:

```bash
python scripts/smoke_package.py
python scripts/smoke_process.py
```

Repository contributors should run the complete gates documented in [AGENTS.md](AGENTS.md).

## Project docs

- [Product specification](docs/specification.md)
- [Operations](docs/OPERATIONS.md)
- [Security policy](SECURITY.md)
- [Architecture decision: modular monolith](docs/adr/0001-modular-monolith.md)
- [Architecture decision: native process release authority](docs/adr/0002-native-process-release-authority.md)

## License

Thinkroom is open source under the [MIT License](LICENSE).
