# Project status

The living record of where SONU is: what works on real hardware, what's next, what's open, and every change so far.
**Updated in every PR.** Design and research live in [`PROPOSAL.md`](PROPOSAL.md); decisions in
[`DECISIONS.md`](DECISIONS.md).

_Last update: 2026-09-25_

## Where we are

| Milestone | State |
|---|---|
| M0 Foundations | ✅ done — doctor, config, audit log, Hyprland IPC, Ollama client |
| M1 Verified window operator | ✅ done and **in daily use on the owner's laptop** |
| M1.5 System settings (PC control) | 🧪 **built, waiting for the owner's test** — see below ([D21](DECISIONS.md), [D24](DECISIONS.md), [D26](DECISIONS.md)) |
| M2 Daemon + memory | after M1.5 — faster replies (model stays loaded), journal, "remember …" |
| M3 Voice · M4 Projects · M5 Connect · M6 Perception · M7 Beyond | see [roadmap](PROPOSAL.md#8-roadmap) |

### Verified on the owner's laptop (Arch · Hyprland 0.56.2 · RTX 3070 Ti · `qwen3.5:4b`)

- Plans in ~1 s (3 s on the first request while the model loads).
- `sonu open kitty on workspace 3` (new terminal), `open zen/chrome [on workspace N]` (reuses and moves an open
  window), `move … to workspace N` (asks only when it can't tell which window), `close …` (asks for approval; "no"
  leaves it open), `open VS Code`, `--dry-run`.
- Pick lists for "which one?" questions, in the terminal.
- `search youtube for …`, `open chrome on workspace 3 and search youtube for …`, `search for …` (DuckDuckGo),
  `open github.com/…` — confirmed from the browser's window title.
- "open bluetooth manager and turn on bluetooth" → opens it and reports "✖ Can't do this yet: turn on bluetooth".
- NitroSense key sends `XF86Launch1`.

### Eval suite

`qwen3.5:4b`: **34 / 34** on the v3 suite, incl. 8 settings cases (CPU container; pass/fail only). History in
[`evals.md`](evals.md).

## Now: M1.5 — system settings (built; to test on the laptop)

Goal: "whole PC control" for everyday settings, each change **read back and verified**, using the same programs as
the owner's rice ([D24](DECISIONS.md)). Built with a fake system in tests and evals (nothing real is changed there);
**not yet run on real hardware.** To test after merging:

```sh
sonu doctor                                   # "Settings programs": what's installed
sonu turn on bluetooth                        # rfkill unblock + bluetoothctl power on, then reads "Powered: yes"
sonu set the volume to 30 and mute the mic
sonu turn the sound down a little
sonu set the screen brightness to 70 percent
sonu pause the music                          # with something playing
sonu next song
sonu switch to battery saver mode
sonu turn off the wifi                        # asks first
sonu "how much battery do i have left?"
sonu "what's my volume and brightness?"
```

| Tool | Program | Verified by | Risk |
|---|---|---|---|
| volume: set / up / down / mute / unmute (output and mic) | `wpctl` (`@DEFAULT_AUDIO_SINK@`, capped at 100%) | `wpctl get-volume` | low |
| brightness: set / up / down | `brightnessctl` (first non-NVIDIA backlight) | `brightnessctl -m` | low |
| Bluetooth on / off | `rfkill unblock bluetooth` + `bluetoothctl power on/off` | `bluetoothctl show` → `Powered:` | low |
| Wi-Fi on / off | `nmcli radio wifi on/off` | `nmcli radio wifi` | on: low · **off: medium (asks)** |
| media: play / pause / next / previous | `playerctl` | `playerctl status` / metadata change | low |
| power mode: performance / balanced / power-saver | `powerprofilesctl set` | `powerprofilesctl get` | low |
| status: "what's my volume / battery / Wi-Fi?" | the same programs + `/sys/class/power_supply` | — (read-only) | read-only |

Also built: `sonu doctor` lists which of these programs are installed; a missing one is reported with the package to
install. Brightness never goes below 5% (the screen stays visible); volume never above 100%.
Later candidates: night light (`hyprsunset`), do-not-disturb, keyboard backlight, connecting to a named Wi-Fi network,
pairing a Bluetooth device.

## Open questions and known issues

- **Zen and background tabs**: once, a page SONU sent to Zen didn't show in any window title; SONU now says "didn't
  see it open (may be in a background tab)". Worth checking what Zen does with links from other apps.
- **Planner variance**: once, "open zen on workspace 3 and …" was planned as "show workspace 3"; the same request
  worked on the next try.
- "search for username X in github" searches repositories for those words; `open github.com/X` opens the profile.
- Deep planner: `qwen3.5:9b` isn't pulled yet; `sonu eval --deep -m qwen3:8b` would measure the installed alternative.
- NitroSense key hold-to-talk (M3): check in `wev` that *released* arrives when a 2-second hold ends.
- M1 exit criterion not yet measured formally: the VS Code + Chromium placement example in ≥ 9/10 runs.

## History

### Pull requests

| PR | Merged | What |
|---|---|---|
| — | 2026-09-24 | M0 on the feature branch; `main` created by the owner from it (`5fa95e4`) |
| [#1](https://github.com/thomasmartinoa/SONU/pull/1) | 2026-09-25 | Dependabot: rich 15, hatchling 1.32 |
| [#2](https://github.com/thomasmartinoa/SONU/pull/2) | 2026-09-25 | **M1**: dialects, app resolver, tools, policy, verifier, planner, CLI, evals; first real-desktop fixes |
| [#3](https://github.com/thomasmartinoa/SONU/pull/3) | 2026-09-25 | Terminals open new windows; moves skip windows already there; "declined" status |
| [#4](https://github.com/thomasmartinoa/SONU/pull/4) | 2026-09-25 | Eval note |
| [#5](https://github.com/thomasmartinoa/SONU/pull/5) | 2026-09-25 | Web search/open; `unsupported` parts; SONU's own terminal ignored; eval suite v2 |
| [#6](https://github.com/thomasmartinoa/SONU/pull/6) | 2026-09-25 | Web steps use the browser the request opened; unobserved pages are "unverified"; Ctrl+C message |
| #7 | open | Two-phase page check (slow pages), `site:` operator, `CLAUDE.md` / `STATUS.md` / `DECISIONS.md`, **M1.5 system settings** |

### Found on real hardware → fixed

| Date | What happened on the laptop | Fix (PR) |
|---|---|---|
| 09-24 | `doctor` suggested an embedding model and an "Uncensored" model as planner | never suggest those (M0) |
| 09-24 | "open kitty" asked "kitty or kitty URL Launcher?" (a hidden helper entry) | app name beats program name; hidden entries only by exact name (#2) |
| 09-24 | `sonu --dry-run open kitty …` was rejected | ask options may come before the request (#2) |
| 09-24 | SONU asked "which one?" with no way to answer | pick lists (#2) |
| 09-25 | "open kitty on workspace 2" planned to reuse the terminal in use | terminals always open a new window (#3) |
| 09-25 | "move kitty to 3" asked although one kitty was already on 3 | windows already there aren't candidates (#3) |
| 09-25 | Saying "no" reported "Not done" | "Nothing changed — not approved" (#3) |
| 09-25 | "…and turn on bluetooth" dropped silently, reported done | `unsupported` parts reported, task partial (#5) |
| 09-25 | "…go to youtube" focused the terminal running SONU | the origin terminal is never title evidence (#5) |
| 09-25 | Websites were launched as apps ("youtube is not installed") | `web.search` / `web.open` (#5) |
| 09-25 | "open chrome … search … in github" searched in Zen | web steps use the browser the request opened (#6) |
| 09-25 | A slow page (loaded after ~10 s) was reported unconfirmed | wait for the browser to react, then up to 30 s for the page (#7) |
| 09-25 | The model wrote `… site:github.com` and the search was refused | a `site:` operator for a named site becomes that site's search (#7) |
| 09-25 | (eval) "…and mute the mic" muted the speaker — and verified it | speaker/mic must always be stated (#7) |
| 09-25 | "next song" (before #7) focused the Apple Music window and said done | `media.control`; eval case with a player window open — passes (#7) |
