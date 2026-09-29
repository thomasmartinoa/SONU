# What SONU can do — and everything it could do next

This is the map of SONU's abilities. It covers what works today, what comes next, and **how** each ability
would work. The owner asked for the whole picture instead of adding features one request at a time.

Every ability follows the same rules as the rest of SONU ([`CLAUDE.md`](../CLAUDE.md)):

- It is a typed tool, never a shell.
- Its result is read back and checked; only a confirmed result counts as done.
- Risky steps ask first.
- Anything that leaves the laptop comes from the user's own words.

Legend: ✅ done and verified on the owner's laptop · 🧪 built, to be tried on the laptop · ⏭ next batch ·
🗺 planned (milestone) · ⛔ deliberately not done.

## How SONU can reach into things

There are five ways SONU can act, listed from most reliable to least. Each ability uses the most reliable one that
works:

| Way | Reliability | Used for |
|---|---|---|
| **Hyprland IPC** | exact; verified from Hyprland's own state | windows, workspaces, monitors |
| **Small system programs** (`wpctl`, `nmcli`, `playerctl`, `grim`, `systemd-run`, `yt-dlp`, …) | exact; read back afterwards | settings, media, screenshots, reminders |
| **The rice's own switches** (`qs ipc call …`, Quickshell) | exact; each switch reports its state | night light, keep-awake, do-not-disturb, lock, wallpaper, the volume/brightness popup |
| **Apps' own interfaces** (URLs, CLIs such as `code`, `kitty @`, D-Bus, MPRIS) | good, but app-specific | YouTube, VS Code, terminals, media players |
| **Screen + accessibility** (AT-SPI, screenshots + a vision model, virtual keyboard/mouse) | works anywhere, but slower and error-prone, so it's high risk | anything with no other interface (M6) |

## Desktop and windows

| Ability | State | How |
|---|---|---|
| Open apps (on a workspace), reuse or move an open one, terminals always new | ✅ | XDG catalog + Hyprland exec rules + verification |
| Move, focus, close windows (asks first); "this window"; by title words | ✅ | exact `stableid:` selectors |
| Show a workspace; "what's open?" | ✅ | IPC |
| Fullscreen, maximize, floating/tiled, pin on all workspaces (`windows.state`) | ✅ | Hyprland 0.56 Lua dispatchers (`hl.dsp.window.fullscreen/float/pin`); read back from `j/clients`; pinning floats the window first (Hyprland only pins floating windows) |
| "Put X and Y side by side" (`windows.side_by_side`) | ✅ | both tiled on one workspace (the first window's), the tiler splits it; done only when their positions really are next to each other |
| Save and restore a layout | 🗺 M2 | layouts need memory |
| Screenshot of the screen, a window or an area; copy it | 🧪 this batch | `grim` (+ `slurp` for an area) into `~/Pictures/screenshot/` like the rice's keys, `wl-copy` |
| Screen recording with **OBS** (`obs.record`): start (opens OBS if needed), stop (file verified), pause, resume (not with OBS's Hybrid MP4 format — SONU says so) | ✅ | OBS's own WebSocket server (local only; the password comes from OBS's settings) — turned on once in OBS: Tools → WebSocket Server Settings |
| OBS scenes, streaming (always asks), virtual camera (only when `v4l2loopback` is loaded), replay buffer (when enabled in OBS), muting a source; "is OBS recording?"; names the dialog OBS is stuck on | ✅ | `obs.scene`, `obs.control`, `obs.mute`, `system.status` `obs` — every change read back from OBS |

## System

| Ability | State | How |
|---|---|---|
| Volume and mic, brightness, Bluetooth, Wi-Fi, power mode, battery | ✅ | `wpctl`, `brightnessctl`, `rfkill` + `bluetoothctl`, `nmcli`, `powerprofilesctl`, sysfs |
| Connect a saved Wi-Fi network, a paired Bluetooth device | ✅ | `nmcli connection up`, `bluetoothctl connect` |
| CPU, memory, disk, temperature, GPU, uptime: "how's my laptop doing?" | 🧪 this batch | `/proc`, `/sys`, `nvidia-smi` (read-only) |
| Night light, keep awake, do-not-disturb, clear notifications, lock the screen | 🧪 this batch | the rice's Quickshell switches (`qs ipc call nightlight/caffeine/notifications/lock`) |
| Reminders and timers: "remind me in 20 minutes to stretch", "at 18:30" | 🧪 this batch | `systemd-run --user` timers that show a notification; checked with `systemctl --user` |
| List and cancel reminders (`reminders.list`, `reminders.cancel`) | ✅ | `systemctl --user list-timers 'sonu-reminder-*'`; only SONU's own timers are stopped; several matches → you pick |
| Theme: "switch to tokyo night", "light mode", "next theme", "which theme?" (`desktop.theme`) | ✅ | the rice's `hypr-theme set/next/toggle`, read back from `hypr-theme json`; SONU picks the theme from the real list; "dark mode" when already dark changes nothing |
| Wallpaper: "change my wallpaper", "set my wallpaper to the mountain" (`desktop.wallpaper`) | ✅ | the rice's `hypr-wall next/set` (a picture SONU found in the theme's backgrounds or `~/Pictures`), read back from `hypr-wall current` |
| Volume/brightness popup when SONU changes them | ✅ | volume: the rice's OSD shows it by itself (it watches PipeWire); brightness: `qs ipc call osd brightness <percent>` like `brightness.sh` |
| Clipboard: "what's on my clipboard?", "clear my clipboard" (`clipboard.read`, `clipboard.clear`) | ✅ | `wl-paste` / `wl-copy --clear`; shown only to you — never to the model, the audit log or anywhere else |
| Copy something to the clipboard | ⏭ | needs a source SONU can read (a file, a screenshot — already copied) |
| Suspend, restart, shut down, log out (`system.power`) | ✅ | what the rice's power menu runs (`systemctl suspend/reboot/poweroff`, Hyprland `exit`); high risk: **always asks** and names the open windows; reported "requested", never "done" |
| Join **new** Wi-Fi networks, pair **new** Bluetooth devices | ⛔ for now | needs passwords/PINs; SONU doesn't handle secrets yet |

## Browser

| Ability | State | How |
|---|---|---|
| Open a site you named or a link you typed; search the web, YouTube, GitHub, Wikipedia, ArchWiki, AUR, Reddit | ✅ | fixed URL templates; only your words leave the laptop |
| **Play a video on YouTube**: "play faded on youtube", "play me faded" | 🧪 this batch | `yt-dlp` finds the top result (your words only) → the watch page opens in your browser → SONU makes sure it plays through the system media controls (MPRIS) and checks it's playing; a browser that isn't in the media controls counts as playing when it starts a new audio stream (PipeWire). Zen needs autoplay allowed for youtube.com |
| Pause, next, back in the browser's video | ✅ | `playerctl` (same as any player) |
| List, switch and close tabs; read or summarise the page; click, fill in, scroll | 🗺 M6 | a small SONU browser extension talking to SONU locally (native messaging); page content is data, never instructions |
| Bookmarks and history search | 🗺 M6 | read-only from the browser profile; private, never sent anywhere |
| Downloads: "open my last download" | ✅ | newest file in `~/Downloads` → the app set for its type (`xdg-mime`); programs/scripts never, archives and other types ask first |

## Media

| Ability | State | How |
|---|---|---|
| Play, pause, next, previous what's playing (Apple Music, browser, Spotify, mpv, …) | ✅ | `playerctl` |
| Play a particular song or video (YouTube) | 🧪 this batch | see Browser |
| Play on Apple Music by name | ⛔ for now | the owner's Apple Music app (apple-music-desktop, an Electron wrapper) ignores links passed to it, so a song can't be opened or started without clicking in it (M6). "Play X" plays it on YouTube |
| Play on Spotify by name: "play believer on spotify" (`media.play` service spotify) | ✅ | **no API key**: a `spotify:search:<your words>` link through the media controls (MPRIS OpenUri) makes Spotify play the top result; SONU starts Spotify if needed and checks it's playing a new song whose title or artist shares your words (else "unverified") |
| Local music ("play my downloads folder") | ⏭ | `mpv` with the folder + MPRIS |
| "What's playing?" | ✅ | `system.status` media: `playerctl metadata` (read-only; kept out of the audit log) |

## Files and folders

| Ability | State | How |
|---|---|---|
| Open a folder: "open my downloads", "open dev_works", "open the SONU folder" (`files.open`) | ✅ | XDG user folders, your screenshots folder, a path you typed, or a folder of that name in your home (3 levels) — found by SONU; opened in the rice's file manager (`fileManager` in `binds.lua`, Thunar) and confirmed by its window title |
| Open the newest file in a folder with its app | ✅ | see Downloads |
| Open a file by name; find files; recent files | ⏭ | `fd`/`locate`, `recently-used.xbel` |
| Move, rename | ⏭ | medium risk, asks first |
| Delete | ⏭ | **to the trash only** (`gio trash`: undoable), asks first; never permanent deletion |

## Apps with their own interfaces

| App | Ability | State | How |
|---|---|---|---|
| VS Code / Code - OSS / VSCodium | open a project folder or a file at a line; "open my SONU project" | 🗺 M4 | `code --goto`, project discovery |
| kitty | open a terminal in a folder; run an allowlisted project command | 🗺 M4 | `kitty --directory`; commands only from an allowlist (e.g. `package.json` scripts), asks first |
| git | "what did I change in SONU?" | 🗺 M4 | read-only `git status/log` |
| WhatsApp, mail, chat apps | open a chat; read unread counts | 🗺 M7 | URLs / window titles; **sending messages is high risk** and always asks |
| Any app without an interface | click, type, read the screen | 🗺 M6 | accessibility (AT-SPI) or screenshot + vision (`qwen3.5` can see images) + virtual input; high risk |

## Talking and remembering

| Ability | State | How |
|---|---|---|
| Faster replies: the model stays loaded, `sonu` hands requests to the daemon (~0.8 s instead of 1.6–2.5 s) | ✅ M2 part 1 | `sonud` (`sonu daemon install`), a thin client |
| Natural replies after everything ("Kitty's up on workspace 2.", "I couldn't find a recipes folder…"), small talk, one conversation ("move **it** to 4", "play it again") | ✅ M2 part 2 | a reply written from the verified results and checked in code; the last few turns go to the planner |
| "Remember …": what an app name means ("when I say editor I mean Visual Studio Code"), your name ("call me …"), notes; "what do you remember?", "forget …" | ✅ M2 part 3 | a local memory file; aliases feed the app resolver |
| Activity journal ("what was I doing yesterday?") | 🗺 later (after voice) | local, 30-day retention |
| Voice, push-to-talk: press the SONU key (`sonu listen`), SONU says "How can I help?", say the request, hear the reply (a notification shows what it heard and did); medium-risk steps by a spoken "yes"; pressing again stops SONU talking | 🧪 M3 part 1 | Whisper large-v3-turbo on the GPU (~0.2 s), Silero VAD for end of speech, Kokoro voice (CPU, ~0.8 s), `pw-record`/`pw-play`; high risk needs the picker ([D46](DECISIONS.md)) |
| Voice: "Hey Sonu" — off until you say "listen for Hey Sonu" (it asks first); "Hey Sonu, open kitty on 2" or "Hey Sonu" then the request; "stop listening for Hey Sonu" | 🧪 M3 part 2 | `sonud` keeps the mic open: Silero VAD finds speech, Whisper checks only its first ~1.8 s for the name at the start; everything else is dropped unheard ([D47](DECISIONS.md)) |
| Voice: a conversation — after each reply SONU keeps listening ~6 s for the next request; "thanks" ends it | 🧪 M3 | `[voice] follow_up_s` ([D48](DECISIONS.md)) |
| `sonu voice check`: read ten requests, see how well each speech model hears your voice | 🧪 M3 | Whisper large-v3-turbo vs large-v3 (or `-m` any), word errors and time; nothing kept |
| Voice: talk over SONU to stop it — it goes quiet and listens | 🧪 M3 | its own PipeWire echo canceller (WebRTC) removes SONU's voice from the mic; ≥ 0.4 s of speech that isn't SONU's own words ([D50](DECISIONS.md)) | faster-whisper (GPU) speech-to-text, Kokoro/Piper voice, wake word; high risk never approved by voice alone |
| Make a folder ("make a folder called shop-app in dev_works") | 🧪 M4 | `files.make_folder`: inside home only, the user's own name for it |
| Claude Code by voice: open it in a folder, type your words into it, `/model sonnet`, "is it done?" | 🧪 M4 part 1 | SONU-owned kitty windows (remote control over a private socket, those windows only), screen read back to confirm ([D51](DECISIONS.md)) |
| Claude Code: write the best prompt from an idea ("write the best prompt for my idea: …") | 🧪 M4 part 2 | the local model writes it; shown in full, asked, then pasted into Claude Code ([D52](DECISIONS.md)) |
| GitHub: "what are my open pull requests?" (with checks), "merge the pull request in SONU" | 🧪 M4 part 3 | `gh` (already signed in); SONU finds the PR, won't merge a draft/conflicting/failing/running one; merging is high risk ([D53](DECISIONS.md)) |
| Install software by name ("install Spotify") — verified by the owner (wtype, wlrctl) | ✅ M4.5 | official repos then the AUR (`yay`), explained and asked (high risk); installs in a SONU window with the system's own password prompt (pkexec); checked with `pacman -Q` ([D54](DECISIONS.md)) |
| "What's on my screen?", "read the error in this window", "what is open in zen?" | 🧪 M6 part 1 | grim + the local vision model (`qwen3.5:4b`); a hidden window shown first; the answer private ([D55](DECISIONS.md)) |
| The install password by voice too (opt-in: `[voice] password_by_voice = true`) | 🧪 | the prompt in SONU's install window: typed, or said ([D57](DECISIONS.md)) |
| "Press play in Spotify", "click Home in Thunar" — a button by its name | 🧪 M6 part 2 | accessibility (AT-SPI) via the system Python; asked; checked by what changed ([D56](DECISIONS.md)) |
| Operate any app by looking at the screen | 🗺 M6 | vision model (local `qwen3.5`/`gemma4` or opt-in cloud) + input; high risk, confirmed |
| Cloud models (free tiers), Claude Code driving SONU (MCP) | 🗺 M5 | opt-in; an egress guard shows what leaves the laptop |

## Batches

- **Batch 1** (done, verified on the laptop): play on YouTube, reminders and timers, screenshots, laptop health, the
  rice's switches.
- **Batch 2** (in progress): window states and side by side ✅ (#12); folders and the newest file, "what's playing?",
  list and cancel reminders ✅ (#13); the brightness popup, clipboard, wallpaper and theme ✅ (#14); power and OBS
  ✅ (#15). Apple Music by name isn't possible with the owner's app (see Media); local music waits for music in
  `~/Music`.

After batch 2: M2.
