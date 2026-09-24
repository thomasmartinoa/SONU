# Contributing to SONU

Thanks for helping! SONU aims to show how a *safe*, verifiable desktop agent should be built, so correctness and
safety come before features.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12. You do **not** need Hyprland or Ollama to develop: the
test suite uses a fake compositor (`sonu.testing.fake_hyprland`) and a fake Ollama (`sonu.testing.fake_ollama`).

```sh
git clone https://github.com/thomasmartinoa/SONU.git
cd SONU
uv sync                 # creates .venv from uv.lock
uv run sonu doctor      # try it
```

## Checks (the same ones CI runs)

```sh
uv run ruff format --check .
uv run ruff check .
uv run mypy             # strict
uv run pytest           # hermetic unit tests
```

On a real Hyprland machine you can also run the opt-in, read-only integration tests:

```sh
uv run pytest -m hyprland
uv run pytest -m ollama
```

## Workflow

- Branch from `main`, open a pull request, keep it focused. CI must be green.
- Big changes (new capability, new dependency, protocol change) start with an issue or an ADR in `docs/adr/`.
- Commit messages: imperative mood, explain *why*.

## Rules that protect users

These are non-negotiable in review:

1. **No free-form shell.** Processes are spawned with argv lists, never `sh -c`, and never from model output.
2. **Every side-effecting tool declares its risk and effects, and has a verifier.** A step that cannot be verified is
   reported as unverified, never as done.
3. **Nothing from the screen, files, web or model output is interpolated into commands** (Hyprland Lua dispatch
   included). Build commands from typed, validated values.
4. **No secrets in logs, audit records or prompts.** Use the redaction helpers; never print API keys.
5. **Private by default.** Doctor output and diagnostics never contain window titles or file contents.
6. **Dependencies are a cost.** New runtime dependencies need a reason, a license check (Apache-2.0 compatible), and
   an entry in the PR description.
7. **Timeouts everywhere.** No external call (socket, HTTP, subprocess) without one.

## Code style

- Typed everything (`mypy --strict` passes), pydantic models at boundaries, `from __future__ import annotations`.
- Match the surrounding code; keep functions small; write tests with the fakes rather than mocking internals.
- Tests must not touch the real system unless marked `hyprland` / `ollama`.

## License

By contributing you agree that your contributions are licensed under the Apache License 2.0.
