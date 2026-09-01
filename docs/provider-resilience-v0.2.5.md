# Provider resilience and progress contract

Thinkroom treats provider latency and provider output as untrusted. This contract bounds the time
and physical calls spent on one research attempt while preserving useful partial evidence.

## Deadlines

- Every job has a hard deadline and an earlier soft deadline.
- A new logical phase or queued rollout branch does not start after the soft deadline.
- Work already admitted before the soft deadline may finish within the hard deadline.
- When the soft deadline prevents the remaining research phases from starting, Thinkroom persists a
  `partial` artifact, records which phases or branches were skipped, and settles the job successfully
  with `completion_status: "partial"`. A partial result is evidence recovery, not a completed research
  synthesis.
- A physical call does not start unless its configured effective timeout fits inside the remaining
  hard deadline. The phase fails with `DEADLINE_INSUFFICIENT` instead of starting work that cannot
  finish in time.

Production defaults reserve 300 seconds between the 900-second soft deadline and the 1200-second
hard deadline. A fallback call is limited to 180 seconds and a primary call to 90 seconds.

## Physical-call budget and precedence

One `(attempt, phase, branch)` may consume at most three physical calls. Retry, fallback, and repair
limits are ceilings, not entitlements. The following precedence is evaluated after each outcome:

1. `OUTPUT_LIMIT_EXCEEDED`, cancellation, stale fencing, or an exhausted deadline is terminal.
2. Schema-invalid output may use one repair on the route that produced it, if a call slot remains.
3. `BACKEND_TIMEOUT` never retries the same route and may use the fallback once.
4. A fast transient provider error may retry the same route once after bounded jitter, then may use
   the fallback once.
5. An unclassified error may use the fallback once, without same-route retry or repair.

For example, `initial + retry + fallback` consumes all three calls; malformed fallback output cannot
then be repaired. `primary timeout + fallback malformed + fallback repair` is exactly three calls.

HTTP 429 is rate limiting: it is retryable but does not contribute to the primary circuit score.
No deterministic output-limit failure is retried, repaired, or failed over.

## Job-attempt-local primary circuit

The primary circuit score is reconstructed from persisted physical-call evidence for the current
attempt. `BACKEND_TIMEOUT` contributes two points, a fast transient provider error contributes one,
and rate limiting contributes zero. The circuit opens at two points and never closes within the same
attempt. It prevents new primary calls but does not cancel a primary call already in flight. A new
attempt starts with a fresh score.

Each provider-call record stores its route role, effective timeout, normalized error code, start/end
times, attempt fence, and monotonic database id. An unfinished call past its persisted timeout plus a
small observation grace is `PRESUMED_DEAD`, never active.

## Derived progress

Research detail responses include a derived progress observation with `observed_at`, an evidence
watermark, and per-phase observations. Counts and classifications are explicitly `as_of` the
watermark and are not transactionally exact.

Classification precedence is:

`PRESUMED_DEAD > STALLED > DEGRADED > SLOW > ACTIVE > SETTLING > terminal`.

Waiting behind a known active physical call is queued work, not a stall. Fallback or repair activity
is degraded progress. `SLOW` means a live call crossed its warning threshold but remains inside its
persisted timeout. `STALLED` requires an active job with neither a live call nor runnable queued work
and no recent progress.

## Rollout concurrency canary

Rollout physical-call concurrency is configured separately and defaults to one. Version 0.2.5 admits
at most two independent rollout calls when the operator enables the canary. Frame, fork, critique,
and synthesis remain at one, and the production service continues to admit one active research job.

Promote concurrency two only when executed evidence shows two overlapping rollout calls, no logical
phase exceeding three physical calls, no duplicate branch artifacts, no circuit opening attributable
to rate limiting, and an improved job-duration distribution. Otherwise restore concurrency one.
