# SONU — notes for AI coding sessions

SONU (System-Oriented Neural Utility) is a local-first, privacy-first AI operator for Linux — Arch + Hyprland first.
Owner: [@thomasmartinoa](https://github.com/thomasmartinoa). It is never called JARVIS.

## Start here (in this order)

1. [`docs/STATUS.md`](docs/STATUS.md) — what works on real hardware, what's next, open questions, every PR so far.
2. [`docs/DECISIONS.md`](docs/DECISIONS.md) — every decision and why. Don't re-open a decision without a new reason.
3. As needed: [`docs/PROPOSAL.md`](docs/PROPOSAL.md) (design, research, roadmap), [`docs/threat-model.md`](docs/threat-model.md),
   [`docs/evals.md`](docs/evals.md), [`docs/integrations/hypr-dotfiles.md`](docs/integrations/hypr-dotfiles.md), [`docs/adr/`](docs/adr/).

**Every PR updates `docs/STATUS.md`** (and `docs/DECISIONS.md` when something was decided), so a fresh session or a
fresh clone always has the full context.

## Non-negotiables (from the owner)

- The model never gets a shell. Every action goes through a typed tool in `src/sonu/tools/` with a risk level that
  the policy engine checks. Programs are started with argv lists, never through a shell string.
- Never claim success because a command ran. A step is *done* only when checks on a fresh snapshot passed;
  otherwise it is *unverified* or *failed*. Parts of a request no tool can do go in `Plan.unsupported` and are
  reported, never dropped.
- Risky actions need confirmation. When unsure, SONU asks (the choices are built by SONU; only an offered answer is
  accepted) instead of guessing.
- Local-first: basic use never needs a cloud model. Anything that leaves the machine must come from the user's own
  words (see `web_tools.py`), never from the model or from window/page content.
- The user can always see what SONU wants to do, why, with which tools, and what actually happened (audit log).

## Workflow

- Work on a feature branch, open a PR to `main`; the owner merges. Never push to `main`.
- After a PR merges, restart the branch from `origin/main` before new work.
- The owner tests on their laptop and pastes terminal output. Each real-hardware finding becomes a regression test
  (comment: "found on real hardware"), a fix, and a line in `docs/STATUS.md`.
- Every fix comes with a test that fails on the old code. Keep commits and PR descriptions factual.

## Commands

```sh
uv sync --locked
uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest
uv run pytest -m "hyprland or ollama"   # only on a real Hyprland/Ollama machine
sonu eval                               # real planner model on the eval suite (fake desktop)
```

`sonu eval` needs Ollama with `qwen3.5:4b`. On a small CPU-only machine don't run the test suite at the same time
(the planner times out); a timeout is an error, not a planning failure — re-run with `-c <case>`.

## Code map

```
src/sonu/
  cli/            typer commands: ask (default), prompt, eval, doctor, config, audit, desktop
  core/           engine.py (the loop), planner.py (prompt, examples, repair), planning.py, report.py
  tools/          base.py (tool contract), desktop_tools.py, web_tools.py, registry.py (plan schema),
                  targets.py (which window), context.py
  desktop/        hyprland/ (IPC, events, Lua/hyprlang dialects, desktop adapter), apps/ (XDG catalog, resolver)
  world/          snapshot.py, origin.py (the terminal SONU runs in)
  verify/ policy/ audit/ llm/ doctor/ config/ ui/ (notify-send, rofi/fuzzel/wofi) integrations/ (rice)
  testing/        FakeHyprland (simulates dispatch, apps, browsers), FakeOllama, sample apps, scripted planner
  evals/          cases.toml + runner
tests/unit, tests/integration
```

## Adding a tool

Typed pydantic `Args` (`extra="forbid"`) · `risk` + `effects` · `prepare()` resolves without side effects ·
`execute()` acts, then verifies with `Check`s on fresh state · register in `tools/registry.py` · planner example if
the model needs one (tests validate every example) · an eval case + golden plan (`tests/unit/test_evals.py`) · a
threat-model row if it adds a new risk · `docs/STATUS.md`.

## The owner's machine

Acer Nitro · Arch · Hyprland 0.56.2 (Lua config) · RTX 3070 Ti Laptop 8 GB · 32 GB RAM · Ollama (`qwen3.5:4b`) ·
kitty · Zen + Chromium · Code-OSS/VSCodium · [hypr-dotfiles](https://github.com/thomasmartinoa/hypr-dotfiles) rice
(Quickshell, rofi, wpctl, brightnessctl, nmcli, playerctl, powerprofilesctl, rfkill) · NitroSense key =
`XF86Launch1`.
