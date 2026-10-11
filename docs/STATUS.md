# Project status

The living record of where SONU is: what works on real hardware, what's next, what's open, and every change so far.
**Updated in every PR.** Design and research live in [`PROPOSAL.md`](PROPOSAL.md); decisions in
[`DECISIONS.md`](DECISIONS.md).

_Last update: 2026-10-11_

## Where we are

| Milestone | State |
|---|---|
| M0 Foundations | ✅ done — doctor, config, audit log, Hyprland IPC, Ollama client |
| M1 Verified window operator | ✅ done and **in daily use on the owner's laptop** |
| M1.5 System settings (PC control) | ✅ done and **verified on the owner's laptop** (2026-09-25); connect to saved Wi-Fi / paired Bluetooth devices added after ([D21](DECISIONS.md), [D24](DECISIONS.md), [D26](DECISIONS.md), [D27](DECISIONS.md)) |
| Capability batch 1 | ✅ done and **verified on the owner's laptop** (2026-09-27) — play on YouTube (Zen and Chromium), reminders, screenshots, rice switches, laptop health ([`CAPABILITIES.md`](CAPABILITIES.md), [D34](DECISIONS.md)) |
| Capability batch 2 | ✅ done and **verified on the laptop** — windows (#12), folders/files, media, reminders (#13), theme/wallpaper/clipboard (#14), power and OBS (#15), OBS fixes and Spotify by name (#16). Apple Music by name isn't possible with the owner's app |
| M2 Daemon, conversation, memory | 🚧 part 1 ✅ **`sonud`**: requests in ~0.8 s (#18); part 2 ✅ **natural replies + one conversation** ("move it to 4", "play it again"), ~1.2–1.6 s with the reply (#19); part 3 ✅ **"remember …"** (#20); part 4 🧪 **the activity journal** ("what was I doing yesterday?", #44, [D60](DECISIONS.md)) |
| M3 Voice | 🚧 part 1 ✅ **push-to-talk** on the SONU key, verified by the owner: greeting → hears (Whisper on the GPU) → acts → **says the reply** (Kokoro); medium risk by a spoken yes (#21, #22, [D46](DECISIONS.md)); part 2 🧪 **"Hey Sonu"**, opt-in (#23, [D47](DECISIONS.md)) — the owner: "working perfectly" (#24); a conversation after each reply (#25, [D48](DECISIONS.md)); talk over SONU to stop it (#27, [D50](DECISIONS.md)) |
| M4 Developer operator | 🚧 part 1 🧪 **Claude Code by voice**: make a folder, open Claude Code there, type your words into it, `/model sonnet`, "is it done?" (#28, [D51](DECISIONS.md)); part 2 🧪 **the prompt writer** (#29, [D52](DECISIONS.md)); part 3 🧪 **GitHub** (#31, [D53](DECISIONS.md)) |
| M4.5 Installs | 🧪 **"install <app>"**: official repos then the AUR, the system's own password prompt, verified with pacman (#32, [D54](DECISIONS.md)) |
| M6 Screen control | 🚧 part 1 ✅ **reading the screen** (#33, [D55](DECISIONS.md)); part 2 🧪 **pressing buttons by name** through accessibility (#34, [D56](DECISIONS.md)); part 3 🧪 **clicking by looking + typing** (#37, [D58](DECISIONS.md)) |
| M5 Connect | 🚧 part 1 🧪 **Claude Code uses SONU's tools (MCP)**: `sonu mcp serve`, SONU's policy, asked on screen, personal content kept back (#45, [D61](DECISIONS.md)); next: cloud models (opt-in) |

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

`qwen3.5:4b`: **120 / 120** twice on the v24 suite (2026-10-11, the laptop's GPU, ~1.3 s mean planning), including nine
complex multi-step requests and five for Claude Code. Pass/fail only. History, including the runs that failed while the prompt wording
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

The goal ([PROPOSAL §8.1](PROPOSAL.md#81-the-goal-a-voice-operator-for-the-whole-pc), [D42](DECISIONS.md)): control
the whole PC by natural voice, with SONU talking back — "Hey Sonu", open Claude Code in a new folder, type a prompt it
wrote, switch it to Sonnet, merge the PR on GitHub, install software, and screen control where there's no API.

1. **M2 — daemon, conversation, memory**: ~~`sonud`~~ (#18, installed on the laptop); ~~natural replies and one
   conversation~~ (#19, [D44](DECISIONS.md)) — verified on the laptop through the daemon: "hi sonu", "open kitty on
   workspace 2" → "Kitty is up on workspace two.", "move it to workspace 4" (the window SONU opened), "play faded on
   spotify", "pause it", "play it again", small talk. ~~"Remember …"~~ (#20, [D45](DECISIONS.md)) — on the laptop:
   "remember that when I say editor I mean visual studio code" → "open my editor" opens Visual Studio Code; notes
   listed back. ~~The activity journal~~ (#44, [D60](DECISIONS.md)): sonud records the focused window; "what was I
   doing yesterday?", "when did I last use …?", pause, delete.
2. **M3 — voice** ([D46](DECISIONS.md)): ~~push-to-talk~~ (#21) — tried on the laptop through a virtual microphone
   (a PipeWire null sink with Kokoro speaking into it): "What's my battery?" → "✔ Battery 100%" and the reply spoken;
   "Remember that when I say editor, I mean Visual Studio Code" → stored, reply spoken; ~2 s from the end of speech
   to the reply. **The owner used it with the real mic and the SONU key** (2026-09-28, after #22). ~~"Hey Sonu"~~
   (#23, opt-in) — tried through the virtual mic with the branch daemon: turned on by a typed request (asked
   first); "Hey Sonu, what's my battery?" → answered (~2 s after the sentence ended); "I told Sonny the song was
   great" → ignored; "Hey Sonu." → greeting, then "How is my laptop doing?" → answered; "Hey Sonu, stop listening
   for Hey Sonu" → off, reply spoken. The owner's first try (the key): the name was misheard every time → #24.
   After #24 the owner: "it's working perfectly" — asked for a conversation flow (no "Hey Sonu" before every
   request) and better accuracy → #25: SONU keeps listening ~6 s after each reply (tried through the virtual mic:
   battery → laptop health → "thank you" → "You're welcome.", one key press); `sonu voice check` measures the
   speech models on the owner's own voice. The owner's `sonu voice check`: large-v3-turbo and large-v3 both 96 % of words
   (turbo 0.22 s vs 0.35 s) — turbo stays; its one real miss, "Listen for his sonu", is fixed in #26. Complex requests
   (#26): eight multi-step requests dry-run on the real desktop — all planned right; nine eval cases pass (fake
   desktop, every step checked); one ran for real (volume 30 % + night light + battery → done, verified, then set back).
   ~~Talk over SONU to stop it~~ (#27) — on the laptop, acoustically (the real speakers and mic, another voice
   playing the user): "Stop." 3.5 s into a long reply → SONU quiet and listening ~1.2 s later; then "What's my
   battery?" → answered; "Thank you." → done. **M3 is complete.** The activity journal comes later.
3. **M4 — developer operator** ([D51](DECISIONS.md)): ~~folders + Claude Code in SONU-owned terminals~~ (#28) —
   on the laptop: "make a folder called sonu-m4-test in dev_works, open claude code there … and tell it: reply with
   just the word ready" → folder made, Claude Code opened (the new folder trusted without asking), the exact text
   shown and asked, sent — Claude answered "ready"; "what is claude doing?" → "waiting for you"; "switch it to
   sonnet" → "Set model to Sonnet 5 for this session only", and back to Haiku (the default untouched). Test
   folders and windows removed. ~~The prompt writer~~ (#29) — on the laptop: "write the best prompt for my idea: a file
   called hello.txt that says hi" → SONU wrote a structured prompt (Requirements / Before you start / Done when), showed
   all of it, asked; on yes it was pasted (Claude Code shows "[Pasted text #1 +N lines]") and confirmed sent; Claude
   made the file; "what is claude doing?" → "waiting for you". ~~GitHub~~ (#31): "what are my open pull requests?",
   "merge the pull request in …" — on the laptop against the owner's real GitHub: the list found SONU #31 and
   angelpsalu/smartchefai #4 with their checks; while #31's CI ran, "merge …" was refused ("checks are still running");
   once green, SONU offered it (17 files, +600 −10, checks passing) and asked — declined, #31 stayed open.
   **M4 is complete** with this; next: M4.5 installs.
4. **M4.5 — installs** ([D54](DECISIONS.md)): ~~"install <app>"~~ (#32) — dry runs on the laptop: "install vlc" / "install
   obs studio" → already installed; "install cowsay" → "Install cowsay 3.8.4-1 from the official extra repo (…, 19.40 KiB
   to download). Your password is asked by the system itself."; "install spotify" → a list (official first,
   spotify-launcher marked installed, the AUR's spotify last). A real install needs the owner's password — the
   owner's first try is the check. **The owner installed `wtype` (extra) and `wlrctl` (AUR) by voice** (2026-09-28,
   typing the password in the system dialog) — both verified by SONU. The owner then asked to also **say** the
   password: #35 ([D57](DECISIONS.md), opt-in, risks explained and accepted). On the laptop, without the owner's
   password: "install cowsay" with it on → the prompt in SONU's window ("Authenticating as: martin / Password:"),
   cancelled with Ctrl+C → "it stopped before installing (cancelled?)". The spoken path is the owner's to try.
5. **M6 — screen control** (the owner's choice over M5): ~~part 1, reading the screen~~ (#33) — on the laptop:
   "what does this window say at the bottom?" → the terminal's status line quoted exactly; "what's on my screen?" →
   described; "what is open in zen?" (Zen on another workspace) → shown, then read; 1.2 s per look (9 s the first
   time); the picture deleted after. Next: **part 2, pressing buttons by name through accessibility (AT-SPI)** — the
   accessibility bus is off on the laptop (`org.a11y.Status IsEnabled = false`), so browsers and Electron apps don't
   expose their buttons yet; turning it on is the owner's call. ~~Part 2, pressing buttons~~ (#34) — the owner said
   yes to accessibility (2026-09-28): `org.a11y.Status IsEnabled` → true, saved as gsettings
   `org.gnome.desktop.interface toolkit-accessibility true` (off again: `gsettings set org.gnome.desktop.interface
   toolkit-accessibility false`); apps pick it up when they start. On the laptop: Thunar exposed 60 named controls in
   0.05 s; "press the home button in thunar" → asked, pressed, verified (its title /tmp → home); "press the frobnicate
   button" → "No “frobnicate button” in thunar — it has: Open Parent, Home, …". ~~Part 3, vision + input~~ (#37) —
   measured: asked for pixels, `qwen3.5:4b` missed 5/5 Thunar controls; on its native 0–1000 scale it hit 4/5 within
   5–12 px (the miss: the look-alike button next to it). On the laptop: the Home button found by looking (3.4 s) and
   clicked with the real pointer (`hl.dsp.cursor.move` + `wlrctl`) — Thunar went /tmp → home; `wtype` typed into a
   focused test terminal. Test windows closed, view and cursor restored. By voice the owner's "type hello" went to
   Claude Code (fixed, see the findings table); on the laptop: "open a terminal in workspace 9 and type hello" typed
   into the new terminal (closed afterwards).
5. **M5 — connect** ([D61](DECISIONS.md)): ~~part 1, SONU as an MCP server~~ (#45) — on the laptop: the branch's
   `sonu mcp serve` on the real desktop answered 53 tools and "battery, volume, power mode" in under half a second;
   **real Claude Code** (2.1.296, `claude -p` with `--mcp-config`, the owner's settings untouched) found the tools and
   answered "Battery is 80% (not charging) and power mode is balanced" through `mcp__sonu__system_status`; "what's
   open?" (`desktop_describe`) → the window list shown in a notification, the client told only that it's personal;
   the audit log names the caller (`mcp:claude-code`). Next: cloud models (opt-in, free tiers) — needs the owner's
   choice of provider and keys.

Waiting on music in `~/Music`: local music with `mpv`.

## Open questions and known issues

- **Zen and background tabs**: once, a page SONU sent to Zen didn't show in any window title; SONU now says "didn't
  see it open (may be in a background tab)". Worth checking what Zen does with links from other apps.
- **Planner variance**: once, "open zen on workspace 3 and …" was planned as "show workspace 3"; the same request
  worked on the next try.
- "search for username X in github" searches repositories for those words; `open github.com/X` opens the profile.
- Deep planner: `qwen3.5:9b` isn't pulled yet; `sonu eval --deep -m qwen3:8b` would measure the installed alternative.
- NitroSense key: voice part 1 is press-to-talk (one press, SONU hears until you stop), so key release isn't needed.
  Hold-to-talk would need `wev` to show *released* arriving when a 2-second hold ends.
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
| #12 | 2026-09-27 | Batch 2, windows: `windows.state` (fullscreen, maximize, float/tile, pin) and `windows.side_by_side`; "on all workspaces" no longer means all windows; eval suite v7 |
| #13 | 2026-09-27 | Batch 2: `files.open` (folders, the newest file), "what's playing?", `reminders.list` / `reminders.cancel`; the rice's file manager; eval suite v8 |
| #14 | 2026-09-27 | Batch 2: `desktop.theme` (by name, next, light/dark), `desktop.wallpaper`, the brightness popup, `clipboard.read` / `clipboard.clear`; theme and wallpaper in `system.status`; eval suite v9 |
| #15 | 2026-09-27 | Batch 2: `system.power` (always asks); OBS (`obs.record`, `obs.scene`, `obs.control`, `obs.mute`, status) through its WebSocket server; tasks can end "unverified" ([D39](DECISIONS.md)); eval suite v10 |
| #16 | 2026-09-27 | OBS verified on the laptop and its failures explained (Hybrid MP4 pause, virtual camera without `v4l2loopback`, replay buffer off, dialogs OBS waits on); Spotify by name without a key; Apple Music explained |
| #17 | 2026-09-27 | Roadmap: the goal is a voice operator for the whole PC ([D42](DECISIONS.md)); M2 gains conversation, M4 becomes the developer operator, M4.5 installs, M6 screen control |
| #18 | 2026-09-27 | M2 part 1: `sonud` (model kept loaded and warmed, private socket, questions relayed to the client, Ctrl+C cancels), `sonu` as a thin client with a fallback, `sonu daemon install/status/uninstall` |
| #19 | 2026-09-27 | M2 part 2: natural replies after every request (checked in code), small talk, one conversation ("it", "again"); `[assistant]` settings |
| #20 | 2026-09-27 | M2 part 3: "remember …" (app aliases, name, notes), "forget …", "what do you remember?"; reminder text must be the user's words; a site given as the browser is ignored; "go to <site>" isn't the browser app |
| #21 | 2026-09-28 | M3 part 1: push-to-talk voice — `sonu listen`, Whisper (GPU) + Silero VAD + Kokoro, spoken replies, medium risk by a spoken yes (high never), `sonu voice setup/test/status`, `[voice]` settings |
| #22 | 2026-09-28 | Voice: SONU greets you when the key is pressed ("How can I help?", taking turns, prepared in advance) and a desktop notification shows listening → what it heard → the result; `[voice] greet` |
| #23 | 2026-09-28 | M3 part 2: "Hey Sonu", opt-in (`voice.wake`, asked first, remembered): the name at the start wakes SONU, the rest is the request; `state.json` for a bar; `sonu voice status` shows it; eval suite v12 |
| #24 | 2026-09-28 | Voice accuracy from the owner's first real use: a misheard name after a greeting ("Hey Soin", "He Sonu") is SONU's name; "yes"/"no" in Whisper's hint; beam size 5; a question isn't shown twice |
| #25 | 2026-09-28 | Voice: a conversation (SONU keeps listening ~6 s after each reply; `[voice] follow_up_s`); `sonu voice check` (the owner's voice through each speech model, scored); "HeySanu" as one word; eval suite v13 (86) |
| #26 | 2026-09-28 | Complex requests: nine multi-step eval cases (suite v14, 95 cases); a "play …" part listed as unsupported is sent back to the model; "his sonu" is the name |
| #27 | 2026-09-28 | Voice: talk over SONU to stop it — its own PipeWire echo canceller (WebRTC) while sonud runs; `[voice] barge_in` |
| #28 | 2026-09-28 | M4 part 1: make folders; Claude Code in SONU-owned terminals (kitty remote control over a private socket) — open (asks to trust a folder), type the user's words (asks), `/model` for this session, what it's doing; eval suite v15 (100) |
| #29 | 2026-09-28 | M4 part 2: the prompt writer — an idea → a clear prompt (the local model), shown in full, asked (by voice: shown on screen, a short question), sent as one paste; the user's longer app name wins in aliases; eval suite v16 (101) |
| #30 | 2026-09-28 | CI green again: mypy knows numpy is optional (the voice extra); main's CI had failed since #16 (an OBS test that read the host, fixed in #21) and #21 (numpy) — every CI step reproduced locally on 3.12–3.14 |
| #31 | 2026-09-28 | M4 part 3: GitHub through `gh` — "what are my open pull requests?" (with checks), "merge the pull request" (SONU finds it; never when a draft, conflicting, failing or still running; high risk; done only when GitHub says merged); eval suite v17 (104) |
| #32 | 2026-09-28 | M4.5: "install <app>" — official repos first, then the AUR (with a warning); SONU picks or asks (installed ones marked); high risk; installed in a SONU window with the system's own password prompt (pkexec); done when `pacman -Q` shows it; eval suite v18 (107) |
| #33 | 2026-09-28 | M6 part 1: reading the screen — "what's on my screen?", "read the error in this window", "what is open in zen?" (shown first if hidden) with the local vision model only; the answer private, read out as-is by voice; replies never promise after a failure; eval suite v19 (110) |
| #34 | 2026-09-28 | M6 part 2: "press <button> in <app>" through accessibility (AT-SPI, the system Python's bindings; no mouse) — asked (high risk for delete/send/buy …), checked by what changed; invented app names sent back; eval suite v20 (111) |
| #35 | 2026-09-29 | The install password by voice (opt-in, `[voice] password_by_voice`): the prompt in SONU's install window (polkit's text agent) — typed there, or said and entered by SONU into its own window; never shown, logged or kept |
| #37 | 2026-10-04 | M6 part 3: the ladder's last rung — an app without accessibility: SONU finds the button by looking (0–1000 scale) and clicks with the pointer (high risk, "it can miss"; done only if the window changed); `screen.type` types the user's words into an app (high risk; focus checked right before; text through stdin); eval suite v21 (112) |
| #38 | 2026-10-04 | Found by voice: typing goes to your window, never Claude Code unless asked (plan check); "open a terminal on 3 and type hello" types into that terminal; the rice's default apps read from `~/.config/orrery` (it moved; "open a terminal" asked "Alacritty or kitty?"); eval suite v22 (114) |
| #39 | 2026-10-04 | The rice renamed Orrery: theme and wallpaper through `orrery-theme` / `orrery-wall` (falling back to `hypr-theme` / `hypr-wall`), backgrounds from `~/.config/orrery`; "next wallpaper" in a one-picture theme says so; doctor says "Orrery rice" |
| #41 | 2026-10-11 | Removing software by voice (`packages.remove`, [D59](DECISIONS.md)): the exact package and what only it needed shown and asked; refused when other software or the system needs it; the install window shared (password typed or said); verified with `pacman -Q`; eval suite v23 (116) |
| #44 | 2026-10-11 | The activity journal (M2 part 4, [D60](DECISIONS.md)): sonud records which window has the focus (local SQLite, 30 days, password managers and private windows never); "what was I doing yesterday / this morning?", "when did I last use …?", pause/resume, delete (asks); `sonu journal`; eval suite v24 (120) |
| #45 | open | M5 part 1: SONU as an MCP server ([D61](DECISIONS.md)) — `sonu mcp serve` / `tools` / `setup`; every tool but `voice.wake` and `dev.prompt`; `Engine.call_tool` (policy, checks, audit); asked on screen for words, links, typing, writing; private results shown to the user, never sent to the client |

### Found on real hardware → fixed

| Date | What happened on the laptop | Fix (PR) |
|---|---|---|
| 10-04 | (voice) "type hello into this window" / "write hello here in the kitty terminal" → dev.type, "Claude Code isn't open" (5 times) | code check: dev.type without Claude in the request → sent back as screen.type; "open a terminal on 3 and type hello" types into the terminal that request opened (#38) |
| 10-04 | (voice) "open a terminal" → "Alacritty or kitty?" — the rice moved to `~/.config/orrery`, SONU read only `hypr-theme` | default apps read from `~/.config/orrery/defaults` first (#38). Theme and wallpaper fixed the same way in #39 (`orrery-theme`/`orrery-wall`, falling back to the old names) |
| 10-04 | "next wallpaper" → "The wallpaper is still 1-mountain.png": Orrery's Eclipse theme has one picture | says "This theme has only one wallpaper — name a picture from ~/Pictures" (#39) |
| 10-11 | (journal, recording the real desktop) Claude Code's spinner (◑ ◐ …) changed its terminal's title every second: a new row every ~2 s | a spinner or an unread count ("(3) Inbox") in front of a title is the same title (#44) |
| 10-11 | `sonu journal show today` went to the planner as a request | `journal` routed as a command; a test checks every subcommand is (#44) |
| 10-10 | (dry run) "remove visual studio code" asked "1 installed packages could be …" | one installed match goes straight to the yes/no, naming `visual-studio-code-bin` (#41) |
| 10-04 | (M6) vision coordinates in pixels missed 5/5 Thunar controls | Qwen's own 0–1000 scale: 4/5 hits; every click asked as high risk, "it can miss" (#37) |
| 09-28 | (M6) "press the home button in thunar" → control "home button"; Thunar's is "Home" | words like button/icon/tab/menu are ignored when matching (#34) |
| 09-28 | (M6) "press the frobnicate button in thunar" → listed as unsupported, and the reply said "I pressed Back in Thunar" (the request before) | a press/click part in unsupported is sent back to screen.press; replies may not claim pressed/clicked/typed without a result line (#34) |
| 09-28 | (evals, main too) "…the browser on 2…" planned as "firefox" (not installed, not said) — 3/3 after weeks of passing | an app name that isn't the user's word (roles like browser/terminal aside) is sent back (#34) |
| 09-28 | (M6) "what is open in zen?" with Zen on another workspace: planned as only a read (refused: not on screen) or only a focus (never answered); the reply promised "I'll get it onto your screen" | screen.read shows the window itself, then reads it; after a failure a reply may not promise to act (#33) |
| 09-28 | (M4.5) `pacman -Ss spotify` lists alphabetically — gst-plugin-spotify before spotify-launcher | SONU ranks: the name said, names starting with it, all words, descriptions; the AUR by votes (#32) |
| 09-28 | (M4.5) `pacman -Q code` answers "visual-studio-code-bin" — the package providing it | "code is already there — visual-studio-code-bin provides it"; installed options are marked in the list (#32) |
| 09-28 | (M4) "merge the pull request in SONU" was planned as only listing it, and the **reply said "I merged PR #31"** — nothing was merged | replies may not claim a consequential action (merged, deleted, installed, sent, closed, shut down, …) that no result line shows — SONU says the plain result instead; a merge request without a merge step is sent back with the exact step (after a declined merge the model kept listing: 3/3 right after the change) (#31) |
| 09-28 | (CI) GitHub's CI had failed on every PR since #16 — first the host-dependent OBS test (fixed in #21), then mypy without numpy (CI doesn't install the voice extra); checks were only run on the laptop, where both passed | a mypy override for numpy; then, on CI's 3.13/3.14 only, a recorder pipe left to the garbage collector ("unclosed transport") — SONU's audio pipes are now closed explicitly; CI's steps reproduced in a clean env on 3.12–3.14 (with ResourceWarnings as errors); each PR's CI is checked before it's reported ready (#30) |
| 09-28 | (M4) the prompt writer added things never said (prices, a checkout, backups) | its instructions: Requirements are only the user's words; anything missing becomes a question for Claude Code — and the full prompt is always shown before it's sent (#29) |
| 09-28 | (M4) a multi-line prompt shows in Claude Code as "[Pasted text #1 +4 lines]", not its text | typed = the text or that marker in the input box; sent = it left the input box (#29) |
| 09-28 | (eval, after a prompt change) "…I mean code oss" was planned with app "code" → "Code - OSS or VSCodium?" | the longer installed app name in the user's words wins (#29) |
| 09-28 | (M4) "/model sonnet" + Enter also **saved Sonnet as the user's default** for new Claude Code sessions (set back to Haiku at once); typed fast, Enter could open Claude Code's model menu instead | SONU drives the menu on purpose and presses "s" (this session only); the user's default stays (#28) |
| 09-28 | (M4) "…open claude code there on workspace 9 and tell it …" was planned without the workspace — Claude Code opened on the focused one | the one workspace the user's words name is used (#28) |
| 09-28 | (M4) "make a folder … and open claude code there on workspace 9" was planned as launching the app "code": **Visual Studio Code** opened and restored the owner's last project (left open, on 9) | a plan that launches "code"/"claude" when the user said "Claude Code" is sent back; eval `claude-code-is-not-the-code-app` (#28) |
| 09-28 | (M4) after "tell claude: reply with just the word ready", SONU's reply said "Claude is ready." | replies never say what Claude did or will answer (#28) |
| 09-28 | (voice) SONU's own voice from the built-in speaker reached the mic as speech the whole time (RMS 2467) — talking over it couldn't be told apart | SONU plays through its own echo canceller and listens on its source (RMS 214, one 0.38 s blip that transcribes to nothing); ≥ 0.4 s of speech whose words aren't SONU's sentence stops it (#27) |
| 09-28 | (voice) "…and browser open in workspace 4 and play me alone music there" → the song went to unsupported, task partial; the same words dry-run twice: right once, wrong once (the conversation history differed) | a "play …" part in unsupported with no media.play step is sent back to the model; eval `complex-alone-music` (#26) |
| 09-28 | (`sonu voice check`) "Listen for Hey Sonu" → "Listen for his sonu" | "his" counts as a greeting before a word like "Sonu" (#26) |
| 09-28 | (voice) after one request the owner had to say "Hey Sonu" again for the next | SONU keeps listening ~6 s after each reply; "thanks" / "that's all" / silence ends it (#25) |
| 09-28 | (voice) "listen for Hey Sonu" was heard as "Listen for his own" (twice) | the model now plans it as the wake word (eval `wake-word-misheard`); a `sonu voice check` to measure models on the owner's voice (#25) |
| 09-28 | (eval, after a prompt change) "what do you remember about me?" → reminders.list | "…, not reminders.list" in the rule (#25) |
| 09-28 | (voice, the owner's own voice) "listen for Hey Sonu" was heard as "…Hey Sovenue", "…Hey Soinu", "…He Sonu", "…Hey Soenu", "…Hey Soin"; the model asked "did you mean Hey Sonu?" each time | after a greeting, a word close to "Sonu" is the name ("Sonny", "son", "sunny" aren't) — for the key and for "Hey Sonu"; Whisper beam size 5 (+15 ms) (#24) |
| 09-28 | (voice) the answer "yes" was heard as "Peace" | "Yes. No." in Whisper's hint (#24) |
| 09-28 | A question ("Did you mean …?") was shown twice, in the terminal and the notification | the planner's line is left out when the reply is the same (#24) |
| 09-28 | Pressed the SONU key after #21 merged: nothing happened. The everyday copy wasn't reinstalled yet (it planned "listen" as a request), and from the key there's no terminal, so nothing showed that SONU was listening | reinstalled with the voice extra; SONU says a greeting and a notification shows each stage (#22) |
| 09-28 | (voice) `pw-record` wrote an AU header and `pw-play` rejected raw samples ("Format not recognised"); a failing player looked like success | `--raw` for both; the speaker raises when the player fails (#21) |
| 09-28 | (voice) Ending the recording on loudness cut sentences short — the built-in mic's noise floor is ~450 RMS | Silero VAD decides the end of speech; loudness is only the fallback (#21) |
| 09-28 | (voice) "open kitty" was heard as "open KIDDI" | the installed apps' names go to Whisper as a hint (#21) |
| 09-28 | (voice) kokoro-onnx's bundled espeak data points at its build machine's path | the system's `espeak-ng` (#21) |
| 09-28 | (tests) after a reboot `v4l2loopback` was loaded and an OBS test changed result | the test no longer reads the host's `/sys/module` (#21) |
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
| 09-27 | (eval) "call me Martin" was planned as the prompt's reminder example (set a reminder "stretch" and dim the screen) | reminder text must be the user's words (the planner sends it back); a "call me" example (#20) |
| 09-27 | (eval) "play faded on youtube" → browser "youtube" → "not installed"; "go to youtube" → the browser app | a site given as the browser is ignored; a plan that opens "the browser" for a named website is sent back (#20) |
| 09-27 | A dry run's reply repeated the "would" line | no reply for dry runs (#20) |
| 09-27 | "open kitty on 2" then "move it to 3" → the model picked the *focused* window (another terminal, already on 3) and SONU said "Moved" | "it/that" means the windows the last request acted on; a window already there is reported "was already on workspace 3" (#19) |
| 09-27 | Replies padded and invented: "…and confirmed. Everything looks good.", "running smoothly", "I can … run commands" | filler trimmed in code; number words checked like digits; small talk knows SONU's real abilities (#19) |
| 09-27 | "what's playing?" → "It's playing, but I can't see the song name" (the result is private) | no reply when every result is private — the line on screen is the answer (#19) |
| 09-27 | Measured: each request spent ~1.1 s starting Python and the CLI, and 6 s whenever the model had unloaded | `sonud` + a thin client: ~0.8 s (#18) |
| 09-27 | (daemon) the first request after starting the daemon took 12 s: the model was loading and had to read SONU's instructions | the daemon warms up with a practice plan and holds requests until it's ready (#18) |
| 09-27 | The owner's everyday `sonu` was still the pre-#15 copy: "record my screen" took a screenshot, "stop recording" **cleared the notifications** | the copy is reinstalled right after each merge (in the workflow now); with #15 both go to OBS (evals `record-the-screen`, `stop-recording`) (#16) |
| 09-27 | "pause the recording": OBS answered ok and kept recording — the format is Hybrid MP4, which can't pause | SONU says why and how to change the format (#16) |
| 09-27 | "turn on the obs virtual camera": `v4l2loopback` wasn't loaded, OBS ran `pkexec modprobe` (a password prompt), and every later OBS request was silently queued ("record my screen" → didn't start) | SONU doesn't start the virtual camera without the module and explains; when OBS doesn't act it names the dialog OBS waits on (#16) |
| 09-27 | "save a replay" → "Replay buffer is not available." (off in OBS's settings) | says where to turn it on (#16) |
| 09-27 | Spotify: a `spotify:search:` link through the media controls **played** the top result | `media.play` on Spotify with no API key (#16) |
| 09-27 | (eval) "play faded on apple music" was planned with browser "apple music" → "not installed" | SONU explains Apple Music can't play by name yet (#16) |
| 09-27 | (eval) after a prompt change, "change my wallpaper" asked which one | rule: it's the next one; also fixed the stale "light/dark is toggle" wording (#16) |
| 09-27 | (dry run) "shut down" listed the open windows without the terminal SONU runs in, which closes too | every window is listed (#15) |
| 09-27 | (dry run) "switch to light mode" was planned as "next theme" | `light` / `dark` are the theme tool's own actions; eval `light-mode` (#14) |
| 09-27 | (eval) "switch to the tokyo night theme" was planned as "next theme" | a theme the user's words name wins over "next" (#14) |
| 09-27 | (dry run) "open my downloads" → "Open ~/Downloads in Bulk Rename": "thunar" is also the program of Thunar's Bulk Rename entry | a default command means the entry named after it, else the one with the fewest extra arguments (#13) |
| 09-27 | (dry run) "open my screenshots folder" found `~/hypr-dotfiles/Screenshots`, not the rice's `~/Pictures/screenshot` | "screenshots" is the folder SONU and the rice's keys save to; singular/plural names match (#13) |
| 09-27 | (eval) "open my last download" was planned as the folder "my last download" | "last/latest/newest" in the user's words means the newest file (#13) |
| 09-27 | (dry run) "pin kitty on all workspaces" would have pinned both kitty windows ("all" was read as all windows) | "all/every workspaces, desktops, screens" isn't "all windows"; SONU asks which one (#12) |
| 09-27 | "maximize this window" was planned as fullscreen | prompt: the state is the user's word; eval `maximize-this-window` (#12) |
| 09-27 | "play faded on youtube in firefox" played in Zen and reported done (the plan left the browser out) | a browser the request names (exactly one) wins over the default ([D35](DECISIONS.md)); eval `play-in-the-named-browser` (#11) |
