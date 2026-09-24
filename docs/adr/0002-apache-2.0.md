# ADR 0002 — Apache License 2.0

- **Status:** accepted
- **Date:** 2026-09-24

## Context

The author asked for "the best license" for a fully open-source project that should attract contributors and serve
as a portfolio piece. Options considered: MIT, Apache-2.0, GPL-3.0, AGPL-3.0.

## Decision

License SONU under **Apache-2.0**.

## Rationale

- **Permissive**: anyone can use, study, modify and ship SONU, which maximises adoption and contributions.
- **Explicit patent grant** and patent-retaliation clause — the main advantage over MIT for an AI project.
- Compatible with the permissive licenses of SONU's dependencies and of the likely voice/ML components
  (openWakeWord and Kokoro are Apache-2.0; Qwen3.5 model weights are Apache-2.0).
- Widely understood by companies and reviewers.

Copyleft (GPL/AGPL) would force forks to stay open, but would also keep many individuals and companies from building
on SONU. For a project whose goal is to show how a *safe* desktop agent should be built, reach matters more.

## Consequences

- GPL-licensed components must stay **optional and out-of-process** (e.g. the Piper TTS fork `piper1-gpl`, GPL-3.0),
  so they are not combined with SONU's code into a single work.
- Each new dependency's license is checked before it is added.
