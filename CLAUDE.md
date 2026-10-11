# SONU — notes for AI coding sessions

SONU (System-Oriented Neural Utility) is a local-first, privacy-first AI operator for Linux — Arch + Hyprland first.
Owner: [@thomasmartinoa](https://github.com/thomasmartinoa). It is never called JARVIS.

## Start here (in this order)

1. [`docs/STATUS.md`](docs/STATUS.md) — what works on real hardware, what's next, open questions, every PR so far.
   [`docs/TRY.md`](docs/TRY.md) — what's built but not yet tried by the owner, with the exact words to say.
2. [`docs/DECISIONS.md`](docs/DECISIONS.md) — every decision and why. Don't re-open a decision without a new reason.
3. [`docs/CAPABILITIES.md`](docs/CAPABILITIES.md) — the map of abilities (done / next / planned) and how each works.
4. As needed: [`docs/PROPOSAL.md`](docs/PROPOSAL.md) (design, research, roadmap), [`docs/threat-model.md`](docs/threat-model.md),
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
- After a PR merges, restart the branch from `origin/main` before new work, and on the owner's laptop reinstall
  their everyday copy at once (`uv tool install --reinstall ~/dev_works/SONU`): a stale copy made "stop recording"
  clear the notifications.
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
  cli/            entry.py (thin client first), typer commands: ask (default), prompt, eval, doctor, config,
                  audit, desktop, daemon, voice (+ `sonu listen` through the daemon), journal, mcp, browser
  daemon/         sonud: server.py (model kept loaded, one request at a time), client.py (stdlib only), protocol
  browser/        extension/ (SONU's own, Chromium + Zen: tabs, a page's text, clicks, fields, scrolling), host.py (native messaging → a local socket), install.py
  mcp/            server.py: SONU's tools for Claude Code over MCP (stdio, Engine.call_tool, asked on screen);
                  client.py + outside.py + schema.py: other MCP servers' tools for SONU (untrusted, D63)
  core/           engine.py (the loop), planner.py (prompt, examples, repair), planning.py, report.py,
                  responder.py (replies), conversation.py, memory.py, layouts.py, notifications.py,
                  prompt_writer.py, screen_reader.py, page_reader.py (a page's text → the local model)
  tools/          base.py (tool contract), desktop_tools.py, window_tools.py, web_tools.py, media_tools.py,
                  settings_tools.py, everyday_tools.py, file_tools.py (folders, files by name, recent),
                  file_change_tools.py (move, rename, the trash, restore), clipboard_tools.py, music_tools.py,
                  rice_tools.py, power_tools.py, obs_tools.py, voice_tools.py, dev_tools.py (folders, Claude
                  Code), project_tools.py (VS Code, a project's commands, git, a terminal in a folder),
                  layout_tools.py, message_tools.py, browser_tools.py, history_tools.py (bookmarks, history),
                  github_tools.py, package_tools.py, screen_tools.py,
                  naming.py (a control by its name), grounding.py, registry.py (plan schema), targets.py, context.py
  system/         commands.py (argv runner), control.py (wpctl, nmcli, playerctl, yt-dlp, qs ipc, /proc, …),
                  github.py (gh), packages.py (pacman, yay), projects.py (a project's commands), git.py
  desktop/        hyprland/ (IPC, events, Lua/hyprlang dialects, desktop adapter), apps/ (XDG catalog, resolver)
  world/          snapshot.py, origin.py (the terminal SONU runs in)
  voice/          audio.py (pw-record/pw-play, end of speech), engines.py (Whisper, Kokoro, Silero), session.py,
                  wake.py ("Hey Sonu")
  llm/            ollama.py, chat.py, cloud.py (Ollama Cloud, opt-in: what may go, audited first)
  verify/ policy/ audit/ doctor/ config/ ui/ (notify-send, rofi/fuzzel/wofi) integrations/ (rice, OBS,
                  kitty = SONU-owned terminals, run_task = a command's exit code, browser = the extension's sockets,
                  browser_data = history and bookmarks from profile folders, mpv = SONU's music player,
                  claude_code = reading its screen, a11y + a11y_helper = AT-SPI)
  testing/        FakeHyprland (simulates dispatch, apps, browsers), FakeOllama, sample apps, scripted planner,
                  a fake MCP server, a fake browser, fake browser profiles, a fake mpv
  evals/          cases.toml + runner
tests/unit, tests/integration
```

## Adding a tool

Typed pydantic `Args` (`extra="forbid"`) · `risk` + `effects` · `prepare()` resolves without side effects ·
`execute()` acts, then verifies with `Check`s on fresh state · register in `tools/registry.py` · planner example if
the model needs one (tests validate every example) · an eval case + golden plan (`tests/unit/test_evals.py`) · a
threat-model row if it adds a new risk · `docs/STATUS.md`.

## Working on the owner's laptop

Claude Code also runs on the owner's laptop (the real Arch + Hyprland machine), which can check what the cloud
container can't:

- `uv run pytest -m "hyprland or ollama"` (read-only checks against the real desktop and Ollama), and `sonu eval` on
  the GPU (minutes instead of an hour).
- `uv run sonu doctor`, `uv run sonu --dry-run <request>`, `uv run sonu audit tail`, `hyprctl -j clients`. Use
  `uv run sonu` for the branch's code; the owner's everyday `sonu` is the `uv tool install` copy of `main`.
- Real-hardware findings directly: reproduce, then write the regression test ("found on real hardware").
- The rice's real names (Quickshell IPC functions, Hyprland config): read `~/.config/quickshell`, `~/.config/hypr` or
  the hypr-dotfiles clone. Never edit the owner's dotfiles from a SONU session.

Rules there:

- A real `sonu` request acts on the owner's real desktop. `--dry-run` first. Run one for real only when the owner
  asked, or when it's harmless and undone at once (open an app, change the volume and set it back). Never turn off
  Wi-Fi, close windows, lock the screen, suspend or send anything anywhere unless the owner said so in the session.
- No `sudo`: when a package is missing, tell the owner the `pacman` command.
- Same workflow as everywhere: a feature branch, a PR, the owner merges. `.claude/settings.json` pre-approves the
  build/test commands and refuses `sudo` and pushes to `main`.

## The owner's machine

Acer Nitro · Arch · Hyprland 0.56.2 (Lua config) · RTX 3070 Ti Laptop 8 GB · 32 GB RAM · Ollama (`qwen3.5:4b`) ·
kitty · Zen + Chromium · Visual Studio Code (2026-09-27; Code-OSS/VSCodium removed) · OBS · Spotify · [hypr-dotfiles](https://github.com/thomasmartinoa/hypr-dotfiles) rice
(Quickshell, rofi, wpctl, brightnessctl, nmcli, playerctl, powerprofilesctl, rfkill) · NitroSense key =
`XF86Launch1`.
