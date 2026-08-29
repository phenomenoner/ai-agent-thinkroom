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
3. Select a domain and the smallest branch count that covers distinct perspectives.
4. Use an idempotency key whenever a transport retry or duplicate submission is possible.

## Submit

CLI:

```bash
thinkroom research \
  --question "Should we adopt this design?" \
  --idempotency-key <stable-key>
```

MCP: call `thinkroom_research` with `question`, optional `context`, `domain`, `branch_count`, and `idempotency_key`.

Submission returns a job handle. It does not imply the research succeeded.

## Poll and inspect

CLI:

```bash
thinkroom get <job-id>
thinkroom list
```

MCP: use `thinkroom_get_research` or `thinkroom_list_research`.

Poll until the job reaches `succeeded`, `failed`, or `cancelled`. Interpret only a terminal result.

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
