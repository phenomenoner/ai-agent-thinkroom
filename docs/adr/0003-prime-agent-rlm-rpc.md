# ADR 0003: Prime Agent RLM uses bounded invocation-local RPC

Status: accepted for v0.2.0

## Context

Thinkroom's original Prime Agent adapter used one-shot text mode with `--no-tools`, `--no-session`,
and `--no-skills`. That was a safe subprocess adapter, but it disabled Prime Agent's RLM capability:
the parent had no persistent IPython control environment, could not call preloaded `rlm(...)`, and
could not receive a native child result through `agent_message`.

Thinkroom needs Prime Agent for bounded provider reasoning, not as a second workflow owner. The
application still owns phase order, retries, persistence, cancellation, deadlines, and final schema
validation. Prime Agent owns its executable, provider authentication, model execution, and native
RLM child.

## Decision

For every Thinkroom phase, `PrimeAgentBackend` starts one invocation-local Prime Agent JSONL RPC
session with:

- a temporary working directory and session store;
- only the built-in IPython tool;
- no context files, extensions, or prompt templates;
- optional provider, model, and thinking arguments from the namespaced Thinkroom settings;
- one predictably named `rlm(...)` child requested by an exact-once parent instruction.

Thinkroom sends one LF-terminated RPC prompt. It accepts phase JSON only when the same RPC stream
proves prompt admission and child custody. Legacy Prime streams may expose a child `agent_message`
whose relationship and session name match the requested child. Prime Agent 0.8.1 instead exposes
child lifecycle snapshots; Thinkroom binds one stable child ID to the requested session name and
requires completed status plus `repliedSinceTask=true`. Both paths still require confirmed cleanup
and a later successful assistant message. An early parent answer, wrong or replaced child, or JSON
without child reply proof is not a successful provider result.

This proves custody of a reply from the requested child through Prime's host-observed stream metadata,
not the semantic equivalence of the parent synthesis to the child's private text. Prime Agent remains
responsible for enforcing its own child admission and `repliedSinceTask` semantics.

## Bounds and failure semantics

The adapter independently bounds:

- configuration and aggregate argv bytes;
- RPC prompt bytes;
- aggregate raw JSONL transport bytes, each LF-delimited event, and total event count;
- retained control/result-event bytes;
- terminal assistant bytes;
- invocation time and process-settlement grace.

High-volume progress/token telemetry is parsed but not retained as result evidence; it still consumes
the independent raw transport budget. Stderr is also
drained without retention so it cannot create backpressure, disclose diagnostics, or race a valid
JSONL result; it is not provider-result evidence and does not decide success. Prompt rejection,
invalid JSONL, wrong or missing child proof, failed assistant turns, bounded JSONL/result overflow,
timeout, cancellation, or premature process exit becomes a typed backend failure. All paths close
stdin, terminate the owned process if necessary, cancel stderr draining, and remove the temporary
directory before returning capacity. The production `ProcessIsolatedBackend` remains the physical
process-group owner and reaps the provider process and every same-group descendant before releasing
capacity.

## Authentication and trust

Prime Agent retains custody of provider credentials, including `openai-codex` OAuth. Thinkroom stores
only the executable path and optional provider/model/thinking routing; it neither copies nor logs
tokens.

IPython and RLM execute model-generated Python with the service account's operating-system authority.
The temporary directory and disabled context loaders reduce accidental coupling; they are not a
sandbox. Operators must trust the Prime Agent binary, its installed Python skills, provider account,
and host. Thinkroom does not point the provider at a target repository or provide a repository write
port.

## Consequences

- Prime Agent can contribute native RLM reasoning to every Thinkroom phase without changing the
  research domain or persistence model.
- One RLM child per phase increases latency and provider usage. Operators should start with bounded
  concurrency and tune only from observed capacity.
- Compatibility depends on Prime Agent's JSONL RPC, IPython tool, `rlm(...)`, `agent_message`, child
  lifecycle snapshots, and transcript event contract. v0.2 was exercised with Prime Agent 0.8.1;
  other versions require the
  focused adapter tests and live smoke.
- A real-provider smoke proves integration and child admission, not research correctness or model
  quality.

## Rejected alternatives

- Keep one-shot text mode: rejected because it cannot exercise native RLM.
- Reimplement RLM inside Thinkroom: rejected because Prime Agent already owns that capability and
  duplicating it would add another child/session protocol.
- Reuse a long-lived Prime session across phases/jobs: rejected because it expands state leakage,
  cancellation, identity, and recovery obligations.
- Give Prime Agent a target repository as its working directory: rejected because research output is
  advisory data and the backend has no authority to mutate caller workspaces.
