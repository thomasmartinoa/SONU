# SONU — System-Oriented Neural Utility

A local-first, privacy-first AI operator for the Linux desktop.

SONU understands a request like *"open VS Code and Chromium, put VS Code on workspace 3 and Chromium on workspace 2"*,
plans it, checks it against a permission policy, executes it through controlled tools, **observes and verifies** the
real desktop state, recovers when something goes wrong, and reports only what it actually verified.

```
Understand → Plan → Policy → Execute → Observe → Verify → Recover / Replan → Report
```

- **Local-first:** runs on local models via Ollama; cloud models are optional and off by default.
- **No unrestricted shell:** every action goes through a typed, policy-checked tool.
- **Verification over assumption:** SONU never reports success because a command merely ran.
- **You stay in control:** you can always see what SONU wants to do, why, with which tools, and what actually happened.
- **Auditable:** every run is recorded in an append-only, hash-chained audit log.

First target: Arch Linux · Hyprland 0.55+ (Lua or hyprlang config) · NVIDIA · Ollama.
Works on any distro running Hyprland; other compositors can be added behind the same adapter interface.

## Status

✅ **Milestone M1 — the verified window operator — is implemented** and in daily use on the author's laptop.
SONU can open apps (on a chosen workspace), move, focus and close windows, switch workspaces, and open websites and
searches in your browser — each step verified on the real desktop. When part of a request is beyond its tools
("…and turn on Bluetooth"), it says so instead of reporting done. Voice (M3) and memory (M2) come next. See the
[roadmap](docs/PROPOSAL.md#8-roadmap).

## Try it

Requires Linux with Hyprland, Python ≥ 3.12, [uv](https://docs.astral.sh/uv/) and [Ollama](https://ollama.com).

```sh
git clone https://github.com/thomasmartinoa/SONU.git
cd SONU
uv tool install --editable .        # puts `sonu` on your PATH (~/.local/bin)
ollama pull qwen3.5:4b              # the default local planner
sonu doctor                         # checks Hyprland, Ollama, models, GPU, tools, config, audit log
```

Then talk to it:

```sh
sonu --dry-run open kitty on workspace 2      # show the plan, change nothing
sonu open kitty on workspace 2
sonu open VSCodium and Chromium, put VSCodium on workspace 3 and Chromium on workspace 2
sonu close kitty                              # asks first: closing is a medium-risk action
sonu open VS Code                             # two builds installed? SONU asks which one — pick by number
sonu move kitty to workspace 3                # several kitty windows? only asks if it can't tell which
sonu search youtube for networkchuck          # web searches and sites open in your browser, verified
sonu turn on bluetooth and set the volume to 40
sonu pause the music · sonu switch to battery saver mode · sonu "how much battery is left?"
sonu play faded on youtube · sonu remind me in 20 minutes to stretch · sonu screenshot this window
sonu open chrome on 3 and go to github.com
sonu prompt                                   # a text box for keybinds; result as a notification
```

A run looks like this (illustrative output) — every ✔ is a check SONU made on the real desktop *after* acting:

```
plan by qwen3.5:4b in 1.3s · 2 steps
Opening VSCodium on workspace 3 and Chromium on workspace 2.
✔ Opened VSCodium on workspace 3
✔ Opened Chromium on workspace 2 (it appeared on 1; moved it)
Done — every step verified
```

More commands:

```sh
sonu eval                           # score your planner model on 163 realistic requests (fake desktop)
sonu desktop windows                # windows as SONU sees them (--no-titles for sharing)
sonu desktop events                 # live Hyprland events
sonu audit tail                     # everything SONU did; `sonu audit verify` checks the hash chain
sonu journal show yesterday         # the activity journal (local); also pause, resume, forget
sonu browser install                # SONU's browser extension: tabs by voice (then add it in the browser)
sonu mcp setup                      # let Claude Code use SONU's tools (MCP); SONU asks you on screen
sonu mcp check                      # start the MCP servers you added ([mcp.servers]) and list their tools
sonu eval --cloud                   # score your Ollama Cloud model too (opt-in: [privacy] and [cloud] in the config)
sonu config init                    # write a commented ~/.config/sonu/config.toml
```

Using the hypr-dotfiles rice? See [the integration guide](docs/integrations/hypr-dotfiles.md) for the keybind,
the `SUPER+SPACE` menu entry and notifications.

## Documentation

- [Project status](docs/STATUS.md) — what works, what's next, open questions, history
- [Decision log](docs/DECISIONS.md) — every decision and why
- [Capabilities](docs/CAPABILITIES.md) — everything SONU can do, and everything planned, with how
- [Design proposal](docs/PROPOSAL.md) — research, architecture, roadmap
- [hypr-dotfiles integration](docs/integrations/hypr-dotfiles.md) — keybind, menu entry, notifications
- [Evals](docs/evals.md) — how planner models are scored, and the results so far
- [Threat model](docs/threat-model.md)
- [Architecture decision records](docs/adr/)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)

## License

[Apache License 2.0](LICENSE)
