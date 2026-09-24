# SONU — System-Oriented Neural Utility

A local-first, privacy-first AI operator for the Linux desktop.

SONU understands a request like *"open VS Code and Chromium, put VS Code on workspace 3 and Chromium on workspace 2"*,
plans it, checks it against a permission policy, executes it through controlled tools, **observes and verifies** the
real desktop state, recovers when something goes wrong, and reports only what it actually verified.

```
Understand → Plan → Policy → Execute → Observe → Verify → Recover / Replan → Report
```

- **Local-first:** runs on local models via Ollama; cloud models are optional and consent-gated.
- **No unrestricted shell:** every action goes through a typed, policy-checked tool.
- **Verification over assumption:** SONU never reports success because a command merely ran.
- **You stay in control:** you can always see what SONU wants to do, why, with which tools, and what actually happened.

First target: Arch Linux · Hyprland (Wayland) · NVIDIA · Ollama.

## Status

🚧 **Design phase.** Read the [design proposal](docs/PROPOSAL.md) — architecture, research findings,
security model and roadmap. Open decisions are listed in §10 of the proposal.

## License

To be decided (see the proposal, open question 2).
