# Project status

The living record of where SONU is: what works on real hardware, what's next, what's open, and every change so far.
**Updated in every PR.** Design and research live in [`PROPOSAL.md`](PROPOSAL.md); decisions in
[`DECISIONS.md`](DECISIONS.md).

_Last update: 2026-09-27_

## Where we are

| Milestone | State |
|---|---|
| M0 Foundations | ✅ done — doctor, config, audit log, Hyprland IPC, Ollama client |
| M1 Verified window operator | ✅ done and **in daily use on the owner's laptop** |
| M1.5 System settings (PC control) | ✅ done and **verified on the owner's laptop** (2026-09-25); connect to saved Wi-Fi / paired Bluetooth devices added after ([D21](DECISIONS.md), [D24](DECISIONS.md), [D26](DECISIONS.md), [D27](DECISIONS.md)) |
| Capability batch 1 | ✅ done and **verified on the owner's laptop** (2026-09-27) — play on YouTube (Zen and Chromium), reminders, screenshots, rice switches, laptop health ([`CAPABILITIES.md`](CAPABILITIES.md), [D34](DECISIONS.md)) |
| Capability batch 2 | 🚧 in progress — window states (fullscreen, maximize, float/tile, pin) and side by side ✅ **verified on the laptop** (#12); folders/files, media, reminders, clipboard, … next |
| M2 Daemon + memory | ⏭ after batch 2 — faster replies (model stays loaded), journal, "remember …" |
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
- Batch 1 (2026-09-27, Claude Code on the laptop, #10): reminder (timer fired, notification sent), screenshot of the
  window (a real capture in `~/Pictures/screenshot/`), night light and do-not-disturb on/off, "how is my laptop
  doing?" (CPU, memory, temperatures, GPU, disk, uptime), power mode. `play faded on youtube` found and opened the
  right video but reported "couldn't confirm it's playing" — correctly: see the open questions.

### Eval suite

`qwen3.5:4b`: **58 / 58** on the v7 suite (2026-09-27, the laptop's GPU, ~1.1 s mean planning; window states and
side by side added). Pass/fail only. History, including the runs that failed while the prompt wording
settled, in [`evals.md`](evals.md).

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

Not possible yet, and reported as such: joining a **new** Wi-Fi network (needs its password), pairing a **new**
Bluetooth device. Playing a particular song, the night light and do-not-disturb came in batch 1 (below).

Verified on the laptop after #8: connecting a saved network by a loose name ("paulkt_5g" → "Paul_KT_5G", asked
first), refusing an unknown one ("hithere").

## Capability batch 1 (tried on the laptop)

The owner asked for the full map of what SONU could do — it's [`CAPABILITIES.md`](CAPABILITIES.md) — and for SONU
to cover things without being asked one by one. Batch 1:

| Tool | What | How it's checked |
|---|---|---|
| `media.play` | "play faded on youtube", "play me faded", "put on some lofi" | `yt-dlp` finds the top result (the user's words only; nothing downloaded) → the video opens in the browser → SONU finds the browser in the media controls (MPRIS), presses play if autoplay held it back, and checks **it's playing** |
| `reminders.set` | "remind me in 15 minutes to …", "at 18:30", "set a timer for 5 minutes" | a `systemd-run --user` timer that shows a notification; checked active with `systemctl --user` |
| `screen.capture` | "screenshot this window / the screen / an area" | `grim` (+ `slurp`) into `~/Pictures/screenshot/` like the rice's keys, copied with `wl-copy`; the file must be a real PNG |
| `desktop.shell` | night light, keep awake, do-not-disturb, clear notifications, lock | the rice's Quickshell switches (`qs ipc call …`), each state read back |
| `system.status` | + "how's my laptop?": CPU, memory, temperature, GPU, disk, uptime; Wi-Fi shows the network | `/proc`, `/sys`, `nvidia-smi` (read-only) |

Commands used to try it (all helper programs are installed on the laptop):

```sh
sonu play faded on youtube                    # should say "Playing '…Faded…' on YouTube in Zen Browser"
sonu remind me in 2 minutes to drink water    # a notification in 2 minutes
sonu take a screenshot of this window
sonu turn on the night light and do not disturb me
sonu "how is my laptop doing?"
```

## Next steps (in order)

1. ~~Play on YouTube, again on the laptop~~ — **works** (2026-09-27, after autoplay was allowed in Zen): Zen and
   Chromium both "✔ Playing 'Alan Walker - Faded'". The audit log shows Zen confirmed through the media controls
   (`firefox.instance_1_1470`, once autoplay let it play) and Chromium through a new audio stream from its own process
   (it still never registers with MPRIS). "in firefox" played in Zen → fixed in #11 (below).
2. **Batch 2** — the ⏭ items in [`CAPABILITIES.md`](CAPABILITIES.md), roughly in this order:
   - ~~windows: fullscreen, floating, pin; "put X and Y side by side"~~ — done (#12, [D36](DECISIONS.md)); verified on
     the laptop: fullscreen/exit, maximize/unmaximize, float, pin, unpin, tile, zen + kitty side by side;
   - open a folder or file ("open my downloads"), "open my last download";
   - "what's playing?", list and cancel reminders, the rice's volume/brightness popup after SONU changes them;
   - clipboard (read-only answer, never sent anywhere), wallpaper and theme through the rice;
   - Spotify by name; local music with `mpv`;
   - suspend / reboot / shut down (high risk, always asks); screen recording.
3. **M2 — daemon + memory**: faster replies (the model stays loaded; no process start per request), the activity
   journal (30-day retention), "remember …" preferences (e.g. "use Code - OSS from now on"), layouts. Plan in
   [`PROPOSAL.md`](PROPOSAL.md#8-roadmap).

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
- **Play on YouTube doesn't work on the laptop yet** (2026-09-27). Zen: the video opens **paused** (Firefox blocks
  audible autoplay for pages opened from outside) and Firefox registers with the media controls (MPRIS) only while
  something plays, so SONU can neither press play nor confirm it. Chromium: the video **plays** (it has a live audio
  stream) but Chromium never appeared in the media controls either (`playerctl -l`: "No players found"). The owner
  chose (a) allow autoplay for youtube.com in Zen and (b) confirm playback from the browser's new PipeWire audio
  stream — (b) is built ([D34](DECISIONS.md)), (a) is the owner's step in Zen. Still unknown: why Chromium never
  registers with MPRIS (does it show in `playerctl -l` when YouTube is played by hand?).
- **"turn off the night light and do not disturb"** is read by `qwen3.5:4b` as "…and turn do-not-disturb **on**",
  even with that exact sentence as a prompt example (tried and removed). The English is ambiguous; the report says
  "Do-not-disturb is on", so it's visible. "turn off do not disturb" works (eval `dnd-off-keeps-notifications`).
- The rice's night light: `Night.qml` polls `pgrep hyprsunset` the instant `hypr-nightlight off` returns, while
  hyprsunset takes ~0.2 s to exit, so it shows "on" until its next 10-s poll. SONU now works around it (below); the
  rice could wait for the process to exit in `off()` (for the owner's dotfiles — not edited from SONU).

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
| [#9](https://github.com/thomasmartinoa/SONU/pull/9) | 2026-09-27 | `CAPABILITIES.md`; batch 1: play on YouTube, reminders, screenshots, rice switches, laptop health; eval suite v5; notes for Claude Code on the laptop |
| #10 | 2026-09-27 | Batch 1 tried on the laptop: planner re-asks when a query isn't the user's words; night light off re-asked when the rice lags; helpers run without SONU's virtualenv; planner rules for laptop health, do-not-disturb off, play queries; playback confirmed by sound when the browser isn't in the media controls; eval suite v6 |
| #11 | 2026-09-27 | The browser named in the request is used when the plan leaves it out; the model's summary isn't printed for steps |
| #12 | open | Batch 2, windows: `windows.state` (fullscreen, maximize, float/tile, pin) and `windows.side_by_side`; "on all workspaces" no longer means all windows; eval suite v7 |

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
| 09-27 | `uv run sonu` → "Power mode: unknown (powerprofilesctl failed: No module named 'gi')": the venv's Python ran the system script | system programs run without SONU's virtualenv on PATH (#10) |
| 09-27 | "how is my laptop doing?" reported volume, brightness, Wi-Fi… instead of the laptop's health | planner rule → `what: ["health"]`; eval `how-is-my-laptop` (#10) |
| 09-27 | "turn off the night light" → "✖ night light is on" although hyprsunset had stopped (the rice's status lags) | re-send on/off once if the state hasn't changed after 1 s (#10) |
| 09-27 | "turn off … do not disturb" cleared all notifications (and a second time turned DND on) | planner rule: DND off is `do_not_disturb_off`, clearing only when asked; eval `dnd-off-keeps-notifications` (#10) |
| 09-27 | "play faded on youtube in chromium" used "Alan Walker - Faded" (from Zen's window title) and was refused | the planner checks the user-words arguments and asks the model again ([D33](DECISIONS.md)); eval `play-with-a-title-on-screen` (#10) |
| 09-27 | Chromium played the video but wasn't in the media controls → "couldn't confirm it's playing" | a new audio stream from the browser's process confirms it ([D34](DECISIONS.md)) (#10) |
| 09-27 | The plan's message said "Playing 'faded' by The Smashing Pumpkins" (made up) | prompt: media.play's query is the user's words only — the same request now says "Playing faded on YouTube" (a broader "never add facts" rule broke "go to youtube" in the evals and was dropped) (#10) |
| 09-27 | After #10, the summary still said "by The 1975" / "by Faded" (the step itself played Alan Walker's Faded) | the model's summary isn't printed for steps; SONU's own step lines say what happens ([D35](DECISIONS.md)) (#11) |
| 09-27 | (dry run) "pin kitty on all workspaces" would have pinned both kitty windows ("all" was read as all windows) | "all/every workspaces, desktops, screens" isn't "all windows"; SONU asks which one (#12) |
| 09-27 | "maximize this window" was planned as fullscreen | prompt: the state is the user's word; eval `maximize-this-window` (#12) |
| 09-27 | "play faded on youtube in firefox" played in Zen and reported done (the plan left the browser out) | a browser the request names (exactly one) wins over the default ([D35](DECISIONS.md)); eval `play-in-the-named-browser` (#11) |
