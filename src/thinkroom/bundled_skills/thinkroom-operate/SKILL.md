---
name: thinkroom-operate
description: Operate Thinkroom research jobs
version: 0.2.0
author: CK, Martin (Hermes Agent)
license: MIT
platforms: [linux]
tags: [thinkroom, research, operations]
---

# Thinkroom operate

Use this skill after `thinkroom-trigger` passes, or whenever a user asks to submit, poll, inspect, cancel, or interpret a Thinkroom job.

## Before submission

1. Verify the service is ready on its configured loopback endpoint.
2. Frame one explicit question and bounded shared context.
3. Select one supported domain: `generic`, `coding`, or `trading`. Omit the field for
   the `generic` default; do not send a descriptive natural-language domain label.
4. Use a branch count from 2 through 6, choosing the smallest count that covers
   distinct perspectives.
5. Keep `deadline_seconds`, when provided, between 30 and 7200.
6. Use an idempotency key whenever a transport retry or duplicate submission is possible.

## Submit

CLI:

```bash
thinkroom research \
  --question "Should we adopt this design?" \
  --idempotency-key <stable-key>
```

MCP: call `thinkroom_research` with `question`, optional `context`, `domain`, `branch_count`, and `idempotency_key`.

Submission returns a job handle. It does not imply the research succeeded.

If MCP returns `INVALID_ARGUMENT` without field-level details, treat it as a
request-schema rejection. Recheck the strict fields above and the callable tool schema,
then make at most one schema-corrected retry. Preserve the same question, context,
branch count, and idempotency intent so correction does not silently become a different
research request. Do not poll or cancel until a submission returns a job handle.

## Poll and inspect

CLI:

```bash
thinkroom get <job-id>
thinkroom list
```

MCP: use `thinkroom_get_research` or `thinkroom_list_research`.

Prefer one deadline-bound blocking wait or supervisor task instead of repeated
main-agent polling. If the available API has no blocking wait, place a finite
terminal-state observation loop inside that one supervised process; do not turn each
observation into a new agent turn. Interpret only `succeeded`, `failed`, or `cancelled`
as terminal, and stop when the declared wait budget is exhausted.

## Cancel

CLI:

```bash
thinkroom cancel <job-id>
```

MCP: use `thinkroom_cancel_research`.

Read back the job afterward; a cancellation request is not proof that execution has stopped.

## Interpretation rules

- Treat evidence verification status as authoritative.
- Separate verified evidence, unverified claims, synthesis, and preserved dissent.
- A `scripted` backend proves orchestration mechanics, not model quality.
- A real-provider smoke proves integration, not correctness of the research.
- Thinkroom output is advisory until the relevant live source, domain owner, or effect boundary is independently checked.
