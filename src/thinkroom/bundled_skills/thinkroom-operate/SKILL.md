---
name: thinkroom-operate
description: Operate Thinkroom research jobs
license: MIT
metadata:
  version: "0.2.5"
  author: "CK, Martin (Hermes Agent)"
  platforms: "linux"
  tags: "thinkroom, research, operations"
---

# Thinkroom operate

Use this skill after `thinkroom-trigger` passes, or whenever a user asks to submit, poll, inspect, cancel, or interpret a Thinkroom job.

## Before submission

1. Verify the service is ready on its configured loopback endpoint.
2. Frame one explicit question from 10 through 10,000 characters and shared context no longer
   than 100,000 characters.
3. Select one supported domain: `generic`, `coding`, or `trading`. For product design,
   organizational strategy, policy, due diligence, or general research synthesis, omit the field
   and use the `generic` default. Use `coding` only when the branches must reason about software or
   source code, and `trading` only for market/trading decision support. Never send a descriptive
   label such as `product_design`, `architecture`, or `strategy`.
4. Use an integer branch count from 2 through 6, choosing the smallest count that covers
   distinct perspectives.
5. Use an idempotency key whenever a transport retry or duplicate submission is possible. It must
   be 1 through 128 printable non-space ASCII characters.
6. For MCP, send only fields exposed by the callable tool schema. In particular, do not invent a
   `deadline_seconds` field when the current MCP tool does not expose one.

## Submit

CLI:

```bash
thinkroom research \
  --question "Should we adopt this design?" \
  --idempotency-key <stable-key>
```

MCP: call `thinkroom_research` with `question`, optional `context`, `domain`, `branch_count`, and `idempotency_key`.

Submission returns a job handle. It does not imply the research succeeded.

If MCP returns `INVALID_ARGUMENT` without field-level details, first compare the exact submitted
payload with the strict fields above and the callable tool schema. The most common correction for a
natural-language research category is to remove `domain` and accept `generic`. Make at most one
retry only when that comparison produces a concrete schema correction. Preserve the question,
context, branch count, and exact idempotency key so the correction cannot silently become a new
research request. If the submitted payload was already schema-correct, do not retry unchanged;
report the opaque rejection and inspect the service boundary. Do not poll or cancel until a
submission returns a job handle.

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

Use the derived `progress` observation to distinguish active provider work, queued rollout work,
fallback/repair degradation, and a presumed-dead call. Its `observed_at` and
`evidence_watermark` fields make the observation boundary explicit; it is not transactionally
exact. Do not describe a live fallback call or work waiting behind a known call as stalled.

A succeeded job can have `completion_status: partial` when its soft deadline prevents more phases
from starting. Preserve the `partial` artifact and branch failures, but do not present it as a
completed synthesis or as evidence that every requested perspective ran.

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
