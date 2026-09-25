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
| M1.5 System settings (PC control) | ✅ done and **verified on the owner's laptop** (2026-09-25); connect to saved Wi-Fi / paired Bluetooth devices added after ([D21](DECISIONS.md), [D24](DECISIONS.md), [D26](DECISIONS.md), [D27](DECISIONS.md)) |
| M2 Daemon + memory | ⏭ **next** — faster replies (model stays loaded), journal, "remember …" |
| M3 Voice · M4 Projects · M5 Connect · M6 Perception · M7 Beyond | see [roadmap](PROPOSAL.md#8-roadmap) |

### Verified on the owner's laptop (Arch · Hyprland 0.56.2 · RTX 3070 Ti · `qwen3.5:4b`)

- Plans in ~1 s (3 s on the first request while the model loads).
- `sonu open kitty on workspace 3` (new terminal), `open zen/chrome [on workspace N]` (reuses and moves an open
  window), `move … to workspace N` (asks only when it can't tell which window), `close …` (asks for approval; "no"
  leaves it open), `open VS Code`, `--dry-run`.
- Pick lists for "which one?" questions, in the terminal.
- `search youtube for …`, `open chrome on workspace 3 and search youtube for …`, `search for …` (DuckDuckGo),
  `open github.com/…` — confirmed from the browser's window title.
- Settings (M1.5): Bluetooth on (through the rfkill block the laptop boots with), volume 30% + mic mute, "a little"
  quieter, brightness 70/10/50%, pause / resume / next / previous (Apple Music), battery-saver mode, Wi-Fi off
  (asked first) and on, battery status; `sonu doctor` finds all seven settings programs.
- Partial requests are honest: "connect to wifi named hithere" turned Wi-Fi on and reported the rest as not possible
  (before `settings.wifi_connect` existed).
- NitroSense key sends `XF86Launch1`.

### Eval suite

`qwen3.5:4b`: **40 / 40** on the v4 suite (CPU container; pass/fail only). History, including the runs that failed
while the prompt wording settled, in [`evals.md`](evals.md).

## M1.5 — system settings (done)

Every change is **read back and verified**, using the same programs as the owner's rice ([D24](DECISIONS.md)).

| Tool | Program | Verified by | Risk |
|---|---|---|---|
| volume: set / up / down / mute / unmute (speaker or mic — always stated) | `wpctl` (capped at 100%) | `wpctl get-volume` | low |
| brightness: set / up / down | `brightnessctl` (first non-NVIDIA backlight; never below 5%) | `brightnessctl -m` | low |
| Bluetooth on / off | `rfkill unblock bluetooth` + `bluetoothctl power` | `bluetoothctl show` → `Powered:` | low |
| Bluetooth device connect / disconnect (already paired; by name or kind: "headphones") | `bluetoothctl connect` | `bluetoothctl info` → `Connected:` | low |
| Wi-Fi on / off | `nmcli radio wifi` | `nmcli radio wifi` | on: low · **off: asks** |
| Wi-Fi connect to a **saved** network | `nmcli connection up id …` | active connection | **asks when it drops the current network** |
| media: play / pause / next / previous / stop (what's already in the player) | `playerctl` | status / track change | low |
| power mode | `powerprofilesctl` | `powerprofilesctl get` | low |
| status: volume, mic, brightness, Bluetooth, Wi-Fi, media, power mode, battery | the same + sysfs | — | read-only |

Not possible yet, and reported as such: picking a particular song or video ("play Faded" — SONU can search YouTube
for it but not start it), joining a **new** Wi-Fi network (needs its password), pairing a **new** Bluetooth device.
Later candidates: night light (`hyprsunset`), do-not-disturb, keyboard backlight.

Verified on the laptop after #8: connecting a saved network by a loose name ("paulkt_5g" → "Paul_KT_5G", asked
first), refusing an unknown one ("hithere").

## Now: capability batch 1 (built; to try on the laptop)

The owner asked for the full map of what SONU could do — it's [`CAPABILITIES.md`](CAPABILITIES.md) — and for SONU
to cover things without being asked one by one. Batch 1:

| Tool | What | How it's checked |
|---|---|---|
| `media.play` | "play faded on youtube", "play me faded", "put on some lofi" | `yt-dlp` finds the top result (the user's words only; nothing downloaded) → the video opens in the browser → SONU finds the browser in the media controls (MPRIS), presses play if autoplay held it back, and checks **it's playing** |
| `reminders.set` | "remind me in 15 minutes to …", "at 18:30", "set a timer for 5 minutes" | a `systemd-run --user` timer that shows a notification; checked active with `systemctl --user` |
| `screen.capture` | "screenshot this window / the screen / an area" | `grim` (+ `slurp`) into `~/Pictures/screenshot/` like the rice's keys, copied with `wl-copy`; the file must be a real PNG |
| `desktop.shell` | night light, keep awake, do-not-disturb, clear notifications, lock | the rice's Quickshell switches (`qs ipc call …`), each state read back |
| `system.status` | + "how's my laptop?": CPU, memory, temperature, GPU, disk, uptime; Wi-Fi shows the network | `/proc`, `/sys`, `nvidia-smi` (read-only) |

To try after merging (`sudo pacman -S --needed yt-dlp slurp wl-clipboard` first if `sonu doctor` lists them missing):

```sh
sonu play faded on youtube                    # should say "Playing '…Faded…' on YouTube in Zen Browser"
sonu remind me in 2 minutes to drink water    # a notification in 2 minutes
sonu take a screenshot of this window
sonu turn on the night light and do not disturb me
sonu "how is my laptop doing?"
```

## Next: M2 — daemon + memory

Faster replies (the model stays loaded; no process start per request), the activity journal (30-day retention),
"remember …" preferences (e.g. "use Code - OSS from now on"), layouts. Plan in [`PROPOSAL.md`](PROPOSAL.md#8-roadmap).

## Open questions and known issues

- **Zen and background tabs**: once, a page SONU sent to Zen didn't show in any window title; SONU now says "didn't
  see it open (may be in a background tab)". Worth checking what Zen does with links from other apps.
- **Planner variance**: once, "open zen on workspace 3 and …" was planned as "show workspace 3"; the same request
  worked on the next try.
- "search for username X in github" searches repositories for those words; `open github.com/X` opens the profile.
- Deep planner: `qwen3.5:9b` isn't pulled yet; `sonu eval --deep -m qwen3:8b` would measure the installed alternative.
- NitroSense key hold-to-talk (M3): check in `wev` that *released* arrives when a 2-second hold ends.
- M1 exit criterion not yet measured formally: the VS Code + Chromium placement example in ≥ 9/10 runs.
- **`sonu resume song` printed nothing at all** (2026-09-25, not reproduced yet): every path prints at least a line;
  `sonu audit tail` after running it again should show what happened.
- Whether Zen/Firefox exposes YouTube to the media controls (MPRIS) on the owner's laptop decides if `media.play` can
  confirm playback; if not, it reports "couldn't confirm it's playing" rather than done.

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
| [#7](https://github.com/thomasmartinoa/SONU/pull/7) | 2026-09-25 | Two-phase page check (slow pages), `site:` operator, `CLAUDE.md` / `STATUS.md` / `DECISIONS.md`, **M1.5 system settings** |
| [#8](https://github.com/thomasmartinoa/SONU/pull/8) | 2026-09-25 | Honest "play <song>"; connect to saved Wi-Fi networks and paired Bluetooth devices; M1.5 verified on the laptop |
| #9 | open | `CAPABILITIES.md`; batch 1: play on YouTube, reminders, screenshots, rice switches, laptop health |

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
| 09-25 | "next song" (before #7) focused the Apple Music window and said done | `media.control`; eval case with a player window open — passes (#8) |
| 09-25 | "play me faded" pressed play on whatever was loaded and said done | media.control can't pick songs: the planner must say so; eval cases (#8) |
| 09-25 | "connect to wifi named hithere" — not possible yet (reported honestly) | `settings.wifi_connect` for saved networks; `settings.bluetooth_device` for paired devices (#8) |
| 09-25 | "play faded on youtube" opened YouTube, then "No media player is running" | `media.play`: find, open, press play, check it's playing (#9) |
