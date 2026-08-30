# ADR 0005: Prime RLM child cleanup as a bounded interim workaround

Status: accepted for the unreleased v0.2 source line

## Context

A real Prime-backed Thinkroom end-to-end run reached SYNTHESIS repair and exceeded the independent 64,000,000-byte raw JSONL transport limit. Prime Agent 0.8.1 does not expose a supported producer-side event projection or ordinary RLM output budget. Removing or broadly raising Thinkroom's cap would discard a safety boundary while leaving producer volume unbounded.

Prime's parent-scoped RLM registry does expose `list_subagents()` and `delete_subagent()`. Deletion cancels or closes the selected child, writes a durable Prime-owned tombstone, and removes that child from messaging and observation. It does not erase transcript or artifact bytes on disk and cannot undo RPC bytes already transported.

CK explicitly accepts the bounded interim risk of adding lifecycle garbage collection while the upstream transport capability remains unavailable.

## Decision

For every Prime-backed phase, Thinkroom supplies one exact IPython cleanup recipe in the original provider prompt. After the matching child `agent_message` arrives and before terminal phase JSON, the parent must:

1. wait until the RPC stream exposes the matching child `agent_message`;
2. boundedly poll the invocation-local direct-child registry;
3. reject any unexpected sibling and select exactly one completed child with the requested Thinkroom session name;
4. delete that child through `rlm.delete_subagent(child)`;
5. boundedly poll the registry again and fail unless it becomes empty;
6. print the exact phase-bound cleanup marker;
7. treat successful cleanup as a session seal: reject any later tool execution or child custody;
8. only then emit terminal schema-only JSON.

`PrimeAgentBackend` accepts the terminal JSON only after the same RPC invocation contains, in order:

- the matching child message;
- exactly one IPython `tool_execution_start` whose normalized code exactly matches the supplied
  cleanup recipe; and
- a later, non-error IPython `tool_execution_end` with the same bounded `toolCallId` whose result
  content contains the expected marker as one complete output line;
- a later standalone terminal assistant `message_end`; and
- an `agent_end` transcript whose final assistant text matches that observed terminal and whose only
  child custody message is the one matching the requested child.

Cleanup before child custody, duplicate/replayed cleanup calls or results, and a marker without the
matching executed recipe are rejected. An aggregate-only terminal, a repeated or unexpected aggregate
child, and any mismatch between the post-cleanup terminal event and the final aggregate transcript are
also rejected. Existing prompt admission, child relationship/name,
child-before-parent, schema, transport, timeout, cancellation, and process-group settlement gates
remain in force.

## Risk boundary

This workaround may reduce retained live child state and later parent-session aggregation. It is not a producer-side stream projection and is not evidence that the 64 MB failure is fixed. If excessive telemetry crosses stdout before child completion, cleanup cannot recover that budget. The extra IPython call also consumes a small amount of transport and model work.

The cleanup receipt is host-observed behavior from the current Prime RPC/tool event contract, not an upstream cryptographic attestation. Prime version changes remain capability-gated by focused tests and a real-provider smoke.

Prime transcripts and artifacts remain invocation-local and are removed by Thinkroom's existing temporary-directory settlement after the provider process exits. Process-group cleanup remains the physical lifecycle authority.

## Verification and release claim

The narrow source claim requires:

- a RED regression showing valid child/final output without confirmed cleanup was previously accepted;
- acceptance of an exact recipe start plus cleanup marker;
- rejection of a forged marker without the exact recipe;
- process-isolation and full local test gates;
- installed package/process verification after deployment.

A real-provider smoke is required before claiming the workaround interoperates with a specific Prime version. Failure of that smoke does not justify removing existing caps or accepting unverified terminal JSON; the runtime must fail closed and the deployment must be reported as an explicitly accepted workaround with unresolved Real Prime evidence.

## Rollback

Rollback is package-level: restore the prior immutable Thinkroom installation target and service command, restart the user service, and verify the prior package/process identity. No database schema, credential, MCP registry, or public API migration is introduced by this decision.

## Rejected alternatives

- Wait indefinitely for upstream producer filtering: rejected as the only operational response because CK accepts an interim bounded workaround.
- Raise or remove the raw transport cap: rejected because it weakens containment without controlling source production.
- Add Headroom or a downstream compression sidecar: rejected because bytes have already crossed the Prime stdout boundary.
- Port the full v0.1.1 child-authored payload/provenance protocol: rejected because cleanup alone is the requested seam and the larger protocol remained live-blocked.
