# Prime progress snapshot accounting

## Scope

The Prime RPC adapter discounts known non-authoritative progress payloads from its accounted transport budget:

- `message_update`: existing `message` and `assistantMessageEvent.partial` snapshots.
- `tool_execution_update`: repeated `args` and callback `partialResult` snapshots.
- `rlm_child_update.child`: `answerPreview` and `recap` snapshots only.

This is an accounting projection, not wire compression, event coalescing, or a claim that snapshots are identical. Every event is still consumed and validated. The original input object is not mutated. Unknown fields, child identity, lifecycle status, and reply evidence remain charged. Tool start/end, control messages, child custody messages, and final messages remain fully charged.

The per-event accounted floor, nesting bound, original wire-byte ceiling, single-event limit, semantic/telemetry event-count limits, final result limits, and child cleanup checks are unchanged. No database schema, provider route, deadline, retry, or RLM completion policy changes are included.

## Evidence and limits

Prime 0.8.1 and 0.9.4 source emit complete tool arguments and callback partial results for `tool_execution_update`. Prime 0.9.4 deduplicates identical direct-child projections but still emits changing snapshots. The adapter previously charged these snapshots in full.

Constructed no-provider JSONL fixtures reproduce small final answers failing the accounted budget when preceded by repeated progress snapshots. The same fixtures complete with this change. Negative cases retain the raw/accounted/count ceilings, final size limit, identity-change rejection, lifecycle regression rejection, and missing-cleanup rejection.

These fixtures are not recorded historical provider traffic. Historical failure metrics identify the accounted ceiling but do not contain per-event-type attribution; they cannot prove this change resolves every failed research job. Actual raw stream volume is not reduced. The historical raw-byte-ceiling failure remains a distinct concern.

## Installation boundary

This candidate is based on the deployed Thinkroom 0.2.7 source commit, not the older remote default branch. Candidate code/package verification does not activate it. The separately prepared Prime 0.9.4 CLI is not a demonstrated drop-in replacement for the current 0.8.1 daemon/runtime: keep versions isolated until RPC/RLM compatibility is exercised. No new provider call or formal comparison run is authorized by this document.
