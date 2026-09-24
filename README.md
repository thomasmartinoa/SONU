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

🚧 **Milestone M0 (foundations) is done** — SONU can inspect your system but does not act on it yet.
Milestone M1 (the verified window operator) is next. See the [roadmap](docs/PROPOSAL.md#8-roadmap).

## Try it

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/thomasmartinoa/SONU.git
cd SONU
uv sync
uv run sonu doctor                  # checks Hyprland, Ollama, models, GPU, tools, config, audit log
```

Other commands available today (all read-only):

```sh
uv run sonu desktop windows         # windows as SONU sees them (--no-titles for sharing)
uv run sonu desktop workspaces
uv run sonu desktop monitors
uv run sonu desktop events          # live Hyprland events (Ctrl+C to stop)
uv run sonu config init             # write a commented ~/.config/sonu/config.toml
uv run sonu config show             # effective configuration
uv run sonu audit tail              # recent audit records
uv run sonu audit verify            # check the audit log's hash chain
uv run sonu doctor --json           # machine-readable report
```

## Documentation

- [Design proposal](docs/PROPOSAL.md) — research, architecture, roadmap, decisions
- [Threat model](docs/threat-model.md)
- [Architecture decision records](docs/adr/)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)

## License

[Apache License 2.0](LICENSE)
