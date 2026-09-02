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

## Install for Codex App or Hermes Agent

Thinkroom ships three managed Agent Skills:

| Skill | Load it when… |
| --- | --- |
| `thinkroom-trigger` | deciding whether an important, uncertain, multi-hypothesis question merits Thinkroom; do not load it for trivial or deterministic work. |
| `thinkroom-operate` | submitting, polling, inspecting, cancelling, or interpreting a Thinkroom job through the CLI, REST API, or MCP. |
| `thinkroom-install` | installing, checking, or removing the managed Thinkroom skill projection. |

The runtime and skill bundle are universal; only the host registration differs. Install and verify
the same managed bundle through the appropriate profile:

```bash
# Codex App / CLI / IDE
uv run thinkroom skills install --profile codex
uv run thinkroom skills status --profile codex

# Hermes Agent default profile
unset HERMES_HOME
uv run thinkroom skills install --profile hermes
uv run thinkroom skills status --profile hermes
```

Codex resolves to `$HOME/.agents/skills`; Hermes resolves to `$HERMES_HOME/skills`, defaulting to
`~/.hermes/skills`. Use `--target <absolute-skill-root>` for another Agent Skills-compatible host.
The installer is idempotent and refuses to overwrite unmanaged or diverged files.

## Connect through MCP

Start the service, then register the same stdio MCP server in the selected host.

Codex App/CLI/IDE:

```bash
codex mcp add thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  -- /absolute/path/to/thinkroom mcp
codex mcp list
```

Hermes Agent default profile:

```bash
hermes --profile default mcp add thinkroom \
  --command /absolute/path/to/thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  --args mcp
hermes --profile default mcp test thinkroom
```

Accept the interactive prompt to enable all four discovered Thinkroom tools, then start a fresh Hermes session.
For a named profile, export `HERMES_HOME="$HOME/.hermes/profiles/<profile-name>"` for every
Thinkroom Skills command and replace `default` above with that exact Hermes profile name.
On Windows, the production Codex App profile runs the agent and Thinkroom inside WSL2. Windows App
and WSL CLI use different Codex homes by default, so their shared MCP configuration must be selected
explicitly. See [Installation and agent integration](docs/INSTALLATION.md) for the exact Codex
Windows/WSL and Hermes profile procedures.

## Provider backends

The default backend is `scripted`, which proves orchestration mechanics—not model quality. For provider integrations, configure one of:

```bash
export THINKROOM_BACKEND=openai
export THINKROOM_OPENAI_API_KEY=...
```

or:

```bash
export THINKROOM_BACKEND=prime_agent
export THINKROOM_PRIME_AGENT_EXECUTABLE=/absolute/path/to/prime-agent
export THINKROOM_PRIME_AGENT_PROVIDER=openai-codex
export THINKROOM_PRIME_AGENT_MODEL=gpt-5.6-luna
export THINKROOM_PRIME_AGENT_THINKING=max
export THINKROOM_MAX_CONCURRENCY=1
export THINKROOM_ROLLOUT_PROVIDER_CONCURRENCY=1
export THINKROOM_JOB_SOFT_TIMEOUT_SECONDS=900
export THINKROOM_BACKEND_TIMEOUT_SECONDS=180
export THINKROOM_JOB_TIMEOUT_SECONDS=1200
```

For one sequential Prime Agent availability fallback, keep the same executable and configure both
routes explicitly:

```bash
export THINKROOM_BACKEND=prime_agent_failover
export THINKROOM_PRIME_AGENT_EXECUTABLE=/absolute/path/to/prime-agent
export THINKROOM_PRIME_AGENT_PROVIDER=openrouter
export THINKROOM_PRIME_AGENT_MODEL=z-ai/glm-5.3-flash
export THINKROOM_PRIME_AGENT_THINKING=high
export THINKROOM_PRIME_AGENT_FALLBACK_PROVIDER=openai-codex
export THINKROOM_PRIME_AGENT_FALLBACK_MODEL=gpt-5.6-terra
export THINKROOM_PRIME_AGENT_FALLBACK_THINKING=high
export THINKROOM_FAILOVER_PRIMARY_TIMEOUT_SECONDS=90
export THINKROOM_BACKEND_TIMEOUT_SECONDS=180
export THINKROOM_JOB_SOFT_TIMEOUT_SECONDS=900
export THINKROOM_JOB_TIMEOUT_SECONDS=1200
```

One fast provider failure can retry the same route once before fallback; a timeout skips that retry.
All retry, fallback, and route-preserving schema repair work shares a three-call phase budget.
Cancellation, fencing, exhausted deadlines, and semantic/result output limits never amplify across
routes. A primary raw-transport output limit is the sole exception: it never retries primary, opens
the attempt-local primary circuit, and may use the configured fallback once within the same budget.
Put API keys in a mode-0600 service environment file rather than in source, unit text, logs, or the
database. See [Provider resilience and progress](docs/provider-resilience-v0.2.5.md) for the exact
deadline, circuit, partial-result, and concurrency contract.

Authenticate that [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) installation first
with its interactive `/login` flow. The adapter
does not copy OAuth credentials into Thinkroom; Prime Agent reads and refreshes its own credential
store. Each Thinkroom provider invocation opens one bounded RPC session, requests one native RLM
child, and accepts schema JSON only after the same RPC stream proves either a matching legacy child
message or a stable Prime 0.8.1 child lifecycle that completed and explicitly replied to the parent.
For the lifecycle path, the admission handle, streamed snapshot, current registry entry, and cleanup
receipt must all carry the same child ID.
The invocation uses a temporary working/session directory and removes it after process settlement.
The concurrency and timeout values above are a conservative starting point for root-plus-child model
work, not a universal capacity claim; tune them from observed provider latency and quotas.

See [Operations](docs/OPERATIONS.md) for the exact runtime contract and environment variables.

## Idempotent jobs

Long-running submissions return a job handle. REST callers send `Idempotency-Key`; the Python SDK uses `ThinkroomClient.research(..., idempotency_key="demo-001")`; the CLI uses `--idempotency-key`; and MCP exposes `idempotency_key` on `thinkroom_research`.

Reusing the same key with a different request fails with `IDEMPOTENCY_CONFLICT`.

## Release-authorized production install

For the release-authorized production path, download the wheel, `requirements-production.txt`, `uv.lock`, and `verify_locked_runtime.py` from the **same GitHub Release**. Install the exact locked dependency closure before installing the wheel without dependency resolution:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-production.txt
uv pip install --python .venv/bin/python --no-deps thinkroom-0.2.5-py3-none-any.whl
.venv/bin/python verify_locked_runtime.py uv.lock --write-manifest runtime-lock-manifest.json
```

An ordinary dependency-resolving wheel install is convenient for evaluation, but it is not the locked production closure. The release-authorized deployment is the installed Python package running as a native POSIX process on Linux, or inside WSL on Windows.

## Operations and security boundary

Thinkroom v0.2 is intentionally a single-node service:

- production defaults bind to literal loopback;
- SQLite supports one service instance only;
- the database parent must already exist on a Linux/POSIX filesystem, be owned by the effective service user (or root), and not be group/world-writable;
- v0.2 has no authentication, RBAC, or multi-tenancy;
- wider access requires a separately secured, authenticated reverse proxy.

Docker is optional operator-owned reference material, not part of the v0.2 native release claim. If you use it, publish only on host loopback—for example `-p 127.0.0.1:8787:8787`—and complete the hardening checklist in [Operations](docs/OPERATIONS.md).

## Verification

After installing the wheel:

```bash
thinkroom verify package
thinkroom verify process
```

Repository contributors should run the complete gates documented in [AGENTS.md](AGENTS.md).

## Project docs

- [Product concept and design philosophy](thinkroom_ai_think_tank_product_concept.md)
- [Product specification](docs/specification.md)
- [Operations](docs/OPERATIONS.md)
- [Installation and agent integration](docs/INSTALLATION.md)
- [Security policy](SECURITY.md)
- [Architecture decision: modular monolith](docs/adr/0001-modular-monolith.md)
- [Architecture decision: native process release authority](docs/adr/0002-native-process-release-authority.md)
- [Architecture decision: agent host integration profiles](docs/adr/0004-agent-host-integration-profiles.md)
- [Architecture decision: bounded Prime Agent RLM RPC](docs/adr/0003-prime-agent-rlm-rpc.md)

## License

Thinkroom is open source under the [MIT License](LICENSE).
