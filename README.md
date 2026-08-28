# Thinkroom

**English** · [繁體中文](README.zh-TW.md)

> **Don’t just generate another answer. Create another line of inquiry.**

Thinkroom is an on-demand AI think tank for questions that deserve more than one model pass. It turns one consequential question into independent lines of inquiry, tests them against evidence, and brings them back as one traceable recommendation—without hiding trade-offs, uncertainty, or dissent.

## The problem with one good answer

Most AI interactions follow a single path:

```text
Question → one interpretation → one reasoning path → one answer
```

That path can be impressive and still be fragile. Once an early assumption, framing, or causal story is wrong, every later step can become a more polished version of the same mistake.

Important human decisions are rarely made that way. We ask people with different priors to investigate independently, compare evidence, challenge hidden assumptions, run experiments, and then decide what survives. Thinkroom makes the smallest useful version of that think-tank process repeatable for people and agents.

```text
Question
   │
   ▼
Frame the real decision
   │
   ├── Perspective A ── independent research ── evidence
   ├── Perspective B ── independent research ── evidence
   └── Perspective C ── independent research ── evidence
                              │
                              ▼
                    delayed cross-critique
                              │
                              ▼
              recommendation + dissent + unknowns
```

Each branch is a temporary mind, not a permanent agent persona. It gets the same bounded context, forms its own hypothesis, records supporting and contradicting evidence, and commits its result before seeing sibling branches. Only then does critique begin.

## Not a multi-agent chat room

Thinkroom is not designed to produce a transcript of AI characters agreeing and disagreeing. The unit of value is a researched line of inquiry.

- **Independence before discussion.** Branches cannot anchor on one another while their views are forming.
- **Evidence before confidence.** A confident claim is not treated as evidence; provenance and verification status travel with the result.
- **Critique after commitment.** Debate is useful only after each branch has exposed its own assumptions and falsifiers.
- **Merge before winner-takes-all.** The best recommendation may combine one branch’s architecture, another’s risk, and a third branch’s validation plan.
- **Uncertainty is an allowed result.** `NEED_MORE_EVIDENCE` is better than a forced answer when the available record cannot support a decision.

The external mental model stays simple:

```text
Question → Perspectives → Evidence → Recommendation
```

Underneath, Thinkroom runs a disciplined research loop:

```text
FRAME → FORK → ISOLATED ROLLOUTS → EVIDENCE → DELAYED CRITIQUE → SYNTHESIZE
```

## A concrete example

Ask whether an event pipeline should be redesigned. A single agent may quickly fall in love with one architecture. Thinkroom can instead open a few genuinely different routes:

- keep the current design and remove the measured bottleneck;
- make a bounded modular refactor;
- move to an event bus or actor model;
- challenge whether the stated problem is architectural at all.

Each route can inspect code, run tests or benchmarks, estimate migration cost, and state what would falsify its conclusion. The final result does not have to crown one route. It may keep the existing core, adopt one boundary from the refactor, and use the contrarian branch’s experiment as the rollout gate.

The same research core can support an investment thesis, incident diagnosis, product strategy, policy trade-off, or due-diligence question because domain knowledge, branch strategy, evaluator, and rollout backend remain optional and composable.

## When to use Thinkroom

Thinkroom is a strong fit when all of these are true:

- the outcome is consequential enough that a shallow answer could be costly;
- the answer is genuinely uncertain, contested, or supported by competing hypotheses;
- independent technical, product, operational, adversarial, or domain perspectives can discriminate between those hypotheses;
- the result should be an auditable recommendation rather than a disposable chat response.

Skip Thinkroom for trivial lookups, deterministic calculations, simple rewrites, direct tool operations, or questions one authoritative source can settle. More branches are useful only when they create meaningful alternatives—not when they merely create more text.

## What comes back

A completed research job is designed to answer:

- **Recommendation:** what the evidence currently supports;
- **Why:** the reasoning and verified evidence behind it;
- **Alternatives:** credible paths that remain available;
- **Trade-offs:** what each path gains and gives up;
- **Risks and falsifiers:** what could make the recommendation wrong;
- **Unknowns and dissent:** what the synthesis could not honestly resolve;
- **Next experiment:** the cheapest useful way to reduce the remaining uncertainty.

Evidence, provenance, verification status, preserved dissent, and job state remain linked to the synthesis.

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

Thinkroom is local-first and single-node by design. It can join an existing agent stack without owning the agent, the model provider, or the final decision.

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

- [Product concept and design philosophy](thinkroom_ai_think_tank_product_concept.md)
- [Product specification](docs/specification.md)
- [Operations](docs/OPERATIONS.md)
- [Security policy](SECURITY.md)
- [Architecture decision: modular monolith](docs/adr/0001-modular-monolith.md)
- [Architecture decision: native process release authority](docs/adr/0002-native-process-release-authority.md)

## License

Thinkroom is open source under the [MIT License](LICENSE).
