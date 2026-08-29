# ADR 0004: One runtime, host-specific agent integration profiles

Status: accepted for the v0.2 source line

## Context

Thinkroom can be called from Codex App, Codex CLI/IDE, Hermes Agent, or any MCP-capable client. The provider-side RLM lifecycle is independent of that caller: `PrimeAgentBackend` owns an invocation-local Prime Agent RPC session and accepts output only after matching native child evidence.

Codex and Hermes share the Agent Skills format but use different user skill roots and MCP registries. On Windows, Thinkroom's production runtime also requires Linux/POSIX filesystem custody, while Codex App can run its agent inside WSL2.

A second backend or a forked skill bundle per host would duplicate policy and let the integrations drift. A single magic installer that edits every host's private configuration would instead acquire unnecessary cross-product ownership.

## Decision

Thinkroom keeps one runtime, one Prime/RLM adapter, one MCP server, and one canonical skills bundle.

The managed Skills installer exposes thin profiles:

- `codex` resolves to `$HOME/.agents/skills` and includes Codex App `agents/openai.yaml` presentation/invocation metadata;
- `hermes` resolves to `$HERMES_HOME/skills`, defaulting to `~/.hermes/skills`;
- `--target` remains the explicit universal path for repository-local or other compatible agents.

Exactly one profile or target is required. Every path still uses the same manifest, receipt, hash verification, divergence refusal, and uninstall ownership rules.

MCP registration remains host-specific:

- Codex writes its shared MCP configuration through `codex mcp add` or `~/.codex/config.toml`;
- Hermes writes an explicitly named registry through `hermes --profile <name> mcp add`; the default
  registry is selected as `--profile default` rather than inherited from sticky profile state;
- both launch the same absolute `thinkroom mcp` executable and point it at the same loopback service.

For Codex App on Windows, the supported production topology selects the WSL2 agent environment and runs the runtime, service, Prime Agent, and database inside WSL on a POSIX filesystem. Native Windows execution is not added to the release claim.

The v0.2 remote RLM flow exercised with Prime Agent 0.8.1 is the product baseline. The abandoned v0.1.1 experimental branch is an evidence reservoir, not a second implementation. Only discriminating hardening is ported: bounded raw RPC transport is added outside the existing retained semantic/result budget. Prompt-driven child deletion and child-authored exact payload protocols are not merged because they did not establish a deterministic Prime Agent 0.8.0 live gate.

## Consequences

- Codex App and Hermes receive tailored installation commands without forking runtime behavior or skills.
- Codex-specific UI metadata is inert to Hermes and remains hash-managed by the same installer.
- Named Hermes profiles remain correct through `HERMES_HOME`.
- Codex Windows/WSL config sharing must be explicit because Windows App and WSL CLI Codex homes differ by default.
- Prime Agent 0.8.1 is the exercised compatibility baseline; other versions remain capability-gated by focused tests and a live smoke.
- Plugin packaging is deferred until Thinkroom has a stable executable locator or reviewed remote HTTPS MCP deployment. A plugin must not embed one machine's virtual-environment path.

## Rejected alternatives

- Separate Codex and Hermes runtime packages: rejected because the host is only an MCP client.
- Duplicate skills per host: rejected because trigger and operation policy must remain one authority.
- Merge the entire local v0.1.1 experimental adapter: rejected because its strict prompt-driven child reply/delete transaction remained live-blocked.
- Native Windows production runtime: rejected because the current SQLite and service custody contract is POSIX-only.
