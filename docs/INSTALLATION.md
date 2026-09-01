# Installation and agent integration

Thinkroom has one runtime and two supported local agent-integration profiles. The research service, Prime Agent backend, database, and MCP server are identical in both profiles. Only the Agent Skills root and the host's MCP registry differ.

## Deployment topology

```text
Codex App / CLI / IDE                    Hermes Agent
  Skills: $HOME/.agents/skills             Skills: $HERMES_HOME/skills
  MCP: ~/.codex/config.toml                 MCP: Hermes profile registry
              \                              /
               +---- Thinkroom stdio MCP ---+
                            |
                 loopback Thinkroom service
                            |
              Prime Agent invocation-local RPC
                            |
                   one native RLM child
```

The host agent is a client of Thinkroom. It does not become Thinkroom's provider backend. Native RLM is released inside `PrimeAgentBackend`; Codex and Hermes use the same four Thinkroom MCP tools.

## 1. Install the common runtime

For source evaluation:

```bash
uv lock --check
uv sync --locked --all-extras --dev
install -d -m 0700 .data
```

For a production release, download the wheel, `requirements-production.txt`, `uv.lock`, and `verify_locked_runtime.py` from the same GitHub Release. Install the hash-locked dependency closure first and the wheel with `--no-deps`, as described in the README. A source checkout or an ordinary dependency-resolving wheel install is not the release-authorized production closure.

Set the service backend before starting `thinkroom serve`:

```bash
export THINKROOM_BACKEND=prime_agent
export THINKROOM_PRIME_AGENT_EXECUTABLE=/absolute/path/to/prime-agent
export THINKROOM_PRIME_AGENT_PROVIDER=openai-codex
export THINKROOM_MAX_CONCURRENCY=1
export THINKROOM_ROLLOUT_PROVIDER_CONCURRENCY=1
export THINKROOM_JOB_SOFT_TIMEOUT_SECONDS=900
export THINKROOM_BACKEND_TIMEOUT_SECONDS=180
export THINKROOM_JOB_TIMEOUT_SECONDS=1200
```

Prime Agent owns provider authentication and refreshes its own credentials. Thinkroom does not copy provider tokens. The v0.2 RLM path was exercised with Prime Agent 0.8.1. Other versions require the focused adapter suite and a real-provider smoke; Prime Agent 0.8.0 is not a release-supported RLM baseline.

Start and verify the loopback service:

```bash
uv run thinkroom serve
curl --fail --silent --show-error --noproxy '*' \
  -H 'Host: 127.0.0.1:8787' \
  http://127.0.0.1:8787/health/ready
```

Use the installed binary's absolute path instead of `uv run` in persistent MCP registrations and production services.

## 2A. Codex App profile

On Windows, configure the Codex App agent environment to **Windows Subsystem for Linux**, restart the app, and keep the Thinkroom runtime and database on a Linux/POSIX filesystem. Native Windows and DrvFS database paths are outside the Thinkroom production custody claim.

Install the common skills bundle into Codex's user skill root:

```bash
thinkroom skills install --profile codex
thinkroom skills status --profile codex
```

This resolves to `$HOME/.agents/skills` and installs optional `agents/openai.yaml` UI metadata for Codex App without forking the skill instructions.

Register the local stdio MCP server from Codex CLI in the same WSL environment:

```bash
codex mcp add thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  -- /absolute/path/to/thinkroom mcp
codex mcp list
```

The Windows Codex App uses `%USERPROFILE%\.codex`; Codex CLI inside WSL uses Linux `~/.codex` by default. To make the WSL command update the Windows App's shared configuration, point `CODEX_HOME` at the mounted Windows Codex home for that command:

```bash
CODEX_HOME=/mnt/c/Users/<windows-user>/.codex \
  codex mcp add thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  -- /absolute/path/to/thinkroom mcp
```

Start a new Codex chat after changing skills or MCP configuration. Keep the agent in WSL mode so the registered Linux executable path is meaningful.

## 2B. Hermes Agent profile

Select one Hermes profile before running either the Skills or MCP commands. Do not rely on Hermes's sticky active-profile state.

For the default profile, make both sides explicit:

```bash
unset HERMES_HOME
thinkroom skills install --profile hermes
thinkroom skills status --profile hermes
hermes --profile default mcp add thinkroom \
  --command /absolute/path/to/thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  --args mcp
hermes --profile default mcp test thinkroom
```

For a named profile, bind the filesystem home and Hermes registry to the same name:

```bash
export HERMES_PROFILE=<profile-name>
export HERMES_HOME="$HOME/.hermes/profiles/$HERMES_PROFILE"
thinkroom skills install --profile hermes
thinkroom skills status --profile hermes
hermes --profile "$HERMES_PROFILE" mcp add thinkroom \
  --command /absolute/path/to/thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  --args mcp
hermes --profile "$HERMES_PROFILE" mcp test thinkroom
```

The Thinkroom profile resolves to `$HERMES_HOME/skills`; when `HERMES_HOME` is unset it defaults to `~/.hermes/skills`. A configured `HERMES_HOME` must be a non-empty absolute path. `--args` must remain last in `hermes mcp add`.

`hermes mcp add` connects immediately and prompts for tool selection. Enable all four Thinkroom tools.
Start a fresh Hermes session or reload MCP discovery. The four tools appear with the `mcp_thinkroom_` prefix.

## 2C. Custom or repository-local profile

The installer remains agent-neutral. Use an explicit target when a host uses another compatible Agent Skills root:

```bash
thinkroom skills install --target /absolute/skill/root
thinkroom skills status --target /absolute/skill/root
```

Exactly one of `--profile` or `--target` is required. Installation remains receipt-owned, idempotent, and fail-closed on unmanaged or diverged files.

## 3. Verification and claim boundary

1. Read back skill status; every managed path must be `EXACT`.
2. Probe the host's MCP registry (`codex mcp list` or `hermes --profile <name> mcp test thinkroom`).
3. Run `thinkroom verify package` and `thinkroom verify process` from the installed wheel.
4. Run `scripts/smoke_prime_backend.py` and `scripts/smoke_prime_e2e.py` against the exact installed runtime for the Prime/RLM claim.
5. Treat a real-provider smoke as integration evidence, not proof of research correctness or model quality.

A Codex plugin may later package the same skills and MCP declaration. It is deliberately deferred until the project has a stable executable-location contract or a reviewed remote HTTPS MCP deployment; a plugin must not guess a machine-local virtual-environment path.
