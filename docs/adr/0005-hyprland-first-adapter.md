# ADR 0005 — Hyprland first, behind a compositor adapter, speaking the socket protocol directly

- **Status:** accepted
- **Date:** 2026-09-24

## Context

The first target machine runs Hyprland 0.56.2 with a Lua config. Hyprland moved from hyprlang to Lua in 0.55, and
the dispatcher syntax now depends on which config system is active. Hyprland's docs say dispatcher contents are "not
guaranteed to be stable at all". Other users will run other compositors.

## Decision

1. All compositor access goes through `sonu.desktop.<compositor>`; Hyprland is the first implementation.
2. SONU speaks Hyprland's Unix-socket protocol directly — no `hyprctl` subprocess, no shell.
3. The active config system is detected with `j/status` (`configProvider`), falling back to `hyprlang` when the
   request doesn't exist (< 0.55). Dispatch strings are produced by a dialect module from typed, validated values —
   never by interpolating model output or window titles (Lua `eval` would execute them).
4. Requests are single-line, ≤ 1000 bytes, sent in one write followed by a half-close, with client-side timeouts —
   the compositor reads this socket synchronously on its main thread (verified in the 0.56.2 source).
5. Windows are identified by `stableId` (`stableid:` selector) when available, not by memory address.
6. A `FakeHyprland` that serves real sockets makes the adapter — and later the whole agent loop — testable in CI.

## Consequences

- Supporting Sway/niri/KDE/GNOME later means writing one adapter each, not touching the engine.
- Hyprland protocol changes are contained in one small, well-tested module, and `sonu doctor` surfaces version and
  dialect so breakage is obvious.
