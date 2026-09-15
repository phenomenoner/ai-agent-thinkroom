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

## Backend upgrades

Prime Agent is installed separately from the Thinkroom Python package. Select an explicit executable
with `THINKROOM_PRIME_AGENT_EXECUTABLE`; do not assume that updating an unrelated global CLI or an
embedded agent plugin also updates the backend used by the running service.

When moving from Prime 0.8.1 to 0.9.4:

1. Verify the official release asset checksum and install its dependency closure in a separate
   versioned directory. Preserve the previous executable and service configuration for rollback.
2. Keep client and daemon versions paired. Thinkroom supplies a unique `--daemon-socket` within
   each temporary session directory, while retaining the configured agent/authentication home.
   Do not replace the authentication home merely to isolate a daemon: doing so may hide saved
   provider credentials. Version/help output alone does not prove RPC or RLM compatibility.
3. Keep the provider/model route unchanged while testing the backend change. Run a bounded
   single-stage adapter check and inspect its schema-validated result and cleanup outcome before
   expanding to a complete research workflow.
4. Verify the running service's selected executable after cutover. If activation fails, restore the
   previous selector through the existing service manager rather than starting a second owner.

A successful single-stage check establishes that integration path only, not general research quality
or multi-branch completion rates. Snapshot accounting also does not reduce physical wire volume.
