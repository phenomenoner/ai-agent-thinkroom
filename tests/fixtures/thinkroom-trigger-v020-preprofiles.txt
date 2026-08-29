---
name: thinkroom-trigger
description: Choose Thinkroom for consequential uncertainty
version: 0.2.0
author: CK, Martin (Hermes Agent)
license: MIT
platforms: [linux]
tags: [thinkroom, research, decision-support]
---

# Thinkroom trigger

Use this skill to decide whether a question deserves a Thinkroom research job.

## Invoke Thinkroom when

All of these are true:

1. **The outcome matters.** A shallow answer could lead to a costly, hard-to-reverse, or strategically important decision.
2. **The answer is genuinely uncertain.** There are competing hypotheses, interpretations, designs, or risk models.
3. **Independent perspectives add value.** The question benefits from branches that form views without seeing sibling outputs.
4. **Traceability matters.** The user needs evidence, provenance, verification status, dissent, or an auditable synthesis.

Typical examples include architecture choices, product strategy, investment theses, incident hypotheses, policy trade-offs, due diligence, and research synthesis.

## Do not invoke Thinkroom when

- the task is a trivial lookup, deterministic calculation, simple rewrite, or direct file/tool operation;
- one authoritative source or one bounded verification can settle the question;
- the user needs execution rather than research;
- additional perspectives would add latency but no meaningful discrimination.

## Handoff

When the trigger passes:

1. Load `thinkroom-operate`.
2. Frame one explicit question plus only the context every branch should share.
3. Choose the smallest branch count that covers distinct hypotheses; the default is three.
4. Submit with an idempotency key when retries are possible.
5. Treat evidence verification status as authoritative and preserve unresolved dissent.

Thinkroom supports decisions; it does not replace live verification, domain authority, or approval for external effects.
