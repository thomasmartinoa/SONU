# ADR 0001 — Python as the implementation language

- **Status:** accepted
- **Date:** 2026-09-24

## Context

SONU combines an agent loop, desktop IPC, local model inference clients, voice (wake word, speech-to-text,
text-to-speech), memory and, later, vision. It is an open-source portfolio project that other developers should be
able to read, run and extend.

Candidates were Python, Rust and Go.

## Decision

Use **Python ≥ 3.12**, managed with **uv** and a committed `uv.lock`.

## Rationale

- The local-AI ecosystem SONU needs is Python-first: faster-whisper, openWakeWord, Kokoro, sherpa-onnx, the official
  MCP SDK, and the vendor SDKs.
- Orchestration speed does not matter here: one model call costs hundreds of milliseconds to seconds, and the IPC
  layer does a handful of small socket requests per action.
- The contributor pool is large and the code stays approachable.
- Strictness comes from tooling instead of the compiler: `mypy --strict`, pydantic validation at every boundary,
  `ruff`, and a CI matrix on 3.12–3.14.

## Consequences

- Distribution needs a Python runtime (uv, pipx, or an AUR package).
- If profiling ever shows a hot path (unlikely), that component can be rewritten in Rust behind the same interface.
- Runtime dependencies are kept deliberately few (four in M0) because each one is supply-chain surface.
