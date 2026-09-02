# Provider resilience and transport accounting

This document extends the v0.2.5 [provider resilience and progress
contract](provider-resilience-v0.2.5.md). Retry, fallback, attempt-local circuit, deadlines, partial
results, and rollout-concurrency behavior remain unchanged except for the output-limit distinction
below.

## Cumulative Prime RPC telemetry

Prime RPC can repeat a growing assistant snapshot in both `message_update.message` and
`message_update.assistantMessageEvent.partial`. Those fields are telemetry: Thinkroom never derives
the research result, lifecycle, child custody, cleanup, or event ordering from them. Charging every
repeated snapshot against the same 64 MB budget as authoritative events made a small final result
fail because of cumulative transport amplification.

Thinkroom now keeps two independent totals per physical Prime call:

- `transport_bytes` is the exact raw JSONL wire volume and is capped at 512,000,000 bytes.
- `transport_accounted_bytes` applies the ordinary 64,000,000-byte budget. Non-telemetry events use
  their raw line length. A `message_update` uses a canonical JSON projection that removes exactly the
  top-level `message` and nested `assistantMessageEvent.partial` values while retaining all other
  fields. Every event costs at least 128 bytes, and a retained projection nested beyond 64 container
  levels is rejected as malformed provider output.

The 64 MB per-line ceiling, 20,000 semantic-event ceiling, 200,000 telemetry-event ceiling, provider
timeout, retained control limit, and terminal JSON limit remain independent. The projection is
measured and discarded; it is not a compacted result stream and does not reduce bytes already emitted
by Prime Agent.

`OUTPUT_LIMIT_ACCOUNTED_TRANSPORT` is terminal and never retries or crosses providers. A primary
`OUTPUT_LIMIT_RAW_TRANSPORT` at the 512 MB absolute ceiling retains the v0.2.5 bounded exception: it
does not retry primary, adds two attempt-local circuit points, and may use the configured fallback
once within the shared three-call budget. The same raw limit on fallback remains terminal.

## Audit and migration

Prime call rows retain content-free numeric counters only. In addition to the v0.2.5 counters, v0.2.6
adds `transport_accounted_bytes`. If a returned result fails phase-schema validation, its already
measured counters are retained on the `MALFORMED_PROVIDER_OUTPUT` row. No prompt, response, event,
provider error, credential, or telemetry value is persisted.

An exact canonical v0.2.5 database may migrate by appending that nullable integer column. The earlier
exact v0.2.4 schema may first perform the existing v0.2.5 migration and then the v0.2.6 step. Any
other shape still fails closed before live schema mutation. The complete multi-hop DDL chain and its
final attestation execute in one explicit SQLite transaction; any exception rolls the chain back.

This is a bounded Thinkroom-side mitigation. It does not claim to reduce upstream wire generation;
a future public Prime RPC compact-event option would be the direct volume-reduction boundary.
