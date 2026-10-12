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
| Save and restore a layout | 🧪 #54 | which app is on which workspace (and a terminal's folder), never titles; restoring moves what's open and opens what's missing, closes nothing ([D67](DECISIONS.md)) |
| Screenshot of the screen, a window or an area; copy it | 🧪 this batch | `grim` (+ `slurp` for an area) into `~/Pictures/screenshot/` like the rice's keys, `wl-copy` |
| Screen recording with **OBS** (`obs.record`): start (opens OBS if needed), stop (file verified), pause, resume (not with OBS's Hybrid MP4 format — SONU says so) | ✅ | OBS's own WebSocket server (local only; the password comes from OBS's settings) — turned on once in OBS: Tools → WebSocket Server Settings |
| OBS scenes, streaming (always asks), virtual camera (only when `v4l2loopback` is loaded), replay buffer (when enabled in OBS), muting a source; "is OBS recording?"; names the dialog OBS is stuck on | ✅ | `obs.scene`, `obs.control`, `obs.mute`, `system.status` `obs` — every change read back from OBS |

## System

| Ability | State | How |
|---|---|---|
| Volume and mic, brightness, Bluetooth, Wi-Fi, power mode, battery | ✅ | `wpctl`, `brightnessctl`, `rfkill` + `bluetoothctl`, `nmcli`, `powerprofilesctl`, sysfs |
| Connect a saved Wi-Fi network, a paired Bluetooth device | ✅ | `nmcli connection up`, `bluetoothctl connect` |
| CPU, memory, disk, temperature, GPU, uptime: "how's my laptop doing?" | 🧪 this batch | `/proc`, `/sys`, `nvidia-smi` (read-only) |
| Research: "find out how kestrels hunt and summarise it", "look up X and tell me" | 🧪 #75 | your words searched; the top pages fetched by SONU (public addresses only, no cookies or scripts) and read by the local model; the answer names its sites ([D84](DECISIONS.md)) |
| A briefing: "brief me", "catch me up", "what's my day like?" — battery, reminders, messages, notifications, yesterday's work, anything that went wrong; once a day by itself if you turn it on | 🧪 #74 | SONU's own read-only tools put together; `[briefing] daily` ([D83](DECISIONS.md)) |
| What's busy: "what's using my CPU / memory / GPU?", "why is my fan loud?"; what went wrong: "did anything crash?"; "kill <program>" (asks) | 🧪 #72 | `/proc` (a program's processes together), fans, `nvidia-smi`, `systemctl --failed`, `coredumpctl`, `journalctl`; stopping: your own programs only, never the desktop's core, SIGTERM only ([D81](DECISIONS.md)) |
| Night light, keep awake, do-not-disturb, clear notifications, lock the screen | 🧪 this batch | the rice's Quickshell switches (`qs ipc call nightlight/caffeine/notifications/lock`) |
| Reminders and timers: "remind me in 20 minutes to stretch", "at 18:30" | 🧪 this batch | `systemd-run --user` timers that show a notification; checked with `systemctl --user` |
| List and cancel reminders (`reminders.list`, `reminders.cancel`) | ✅ | `systemctl --user list-timers 'sonu-reminder-*'`; only SONU's own timers are stopped; several matches → you pick |
| Theme: "switch to tokyo night", "light mode", "next theme", "which theme?" (`desktop.theme`) | ✅ | the rice's `hypr-theme set/next/toggle`, read back from `hypr-theme json`; SONU picks the theme from the real list; "dark mode" when already dark changes nothing |
| Wallpaper: "change my wallpaper", "set my wallpaper to the mountain" (`desktop.wallpaper`) | ✅ | the rice's `hypr-wall next/set` (a picture SONU found in the theme's backgrounds or `~/Pictures`), read back from `hypr-wall current` |
| Volume/brightness popup when SONU changes them | ✅ | volume: the rice's OSD shows it by itself (it watches PipeWire); brightness: `qs ipc call osd brightness <percent>` like `brightness.sh` |
| Clipboard: "what's on my clipboard?", "clear my clipboard" (`clipboard.read`, `clipboard.clear`) | ✅ | `wl-paste` / `wl-copy --clear`; shown only to you — never to the model, the audit log or anywhere else |
| Copy to the clipboard: "copy the path of my invoice", "copy my last screenshot", "copy this page's link", "copy hello" | 🧪 #64 | a file's path, a picture or text file, the newest screenshot, the page's address (extension), your own words; checked by reading it back ([D76](DECISIONS.md)) |
| Suspend, restart, shut down, log out (`system.power`) | ✅ | what the rice's power menu runs (`systemctl suspend/reboot/poweroff`, Hyprland `exit`); high risk: **always asks** and names the open windows; reported "requested", never "done" |
| Join **new** Wi-Fi networks, pair **new** Bluetooth devices | ⛔ for now | needs passwords/PINs; SONU doesn't handle secrets yet |

## Browser

| Ability | State | How |
|---|---|---|
| Open a site you named or a link you typed; search the web, YouTube, GitHub, Wikipedia, ArchWiki, AUR, Reddit | ✅ | fixed URL templates; only your words leave the laptop |
| **Play a video on YouTube**: "play faded on youtube", "play me faded" | 🧪 this batch | `yt-dlp` finds the top result (your words only) → the watch page opens in your browser → SONU makes sure it plays through the system media controls (MPRIS) and checks it's playing; a browser that isn't in the media controls counts as playing when it starts a new audio stream (PipeWire). Zen needs autoplay allowed for youtube.com |
| Pause, next, back in the browser's video | ✅ | `playerctl` (same as any player) |
| List, switch and close tabs | 🧪 #58 | SONU's own extension (`sonu browser install`), native messaging to a local socket; titles private; closing asks ([D70](DECISIONS.md)) |
| Read or summarise a page, ask about it or the selected text: "summarise this page", "what does the github tab say about installing?" | 🧪 #59 | the same extension (version 0.2.0, page access): the visible text to the **local** model only; the answer private ([D71](DECISIONS.md)) |
| Click, type into a field, scroll a page: "click sign in", "type red shoes in the search box and press enter", "scroll to the bottom" | 🧪 #60 | the same extension (0.3.0): the page's own links, buttons and fields by your words; buttons ask, "delete / buy / send …" high, never a password field; checked after ([D72](DECISIONS.md)) |
| Bookmarks and history: "find the page about lentil soup I saw yesterday", "what pages did I visit today?", "open my arch wiki bookmark" | 🧪 #61 | read-only copies of the browser's own files (Zen/Firefox, Chromium-family; no extension needed); private; opening a page from history asks ([D73](DECISIONS.md)) |
| Downloads: "open my last download" | ✅ | newest file in `~/Downloads` → the app set for its type (`xdg-mime`); programs/scripts never, archives and other types ask first |

## Media

| Ability | State | How |
|---|---|---|
| Play, pause, next, previous what's playing (Apple Music, browser, Spotify, mpv, …) | ✅ | `playerctl` |
| Play a particular song or video (YouTube) | 🧪 this batch | see Browser |
| Play on Apple Music by name | ⛔ for now | the owner's Apple Music app (apple-music-desktop, an Electron wrapper) ignores links passed to it, so a song can't be opened or started without clicking in it (M6). "Play X" plays it on YouTube |
| Play on Spotify by name: "play believer on spotify" (`media.play` service spotify) | ✅ | **no API key**: a `spotify:search:<your words>` link through the media controls (MPRIS OpenUri) makes Spotify play the top result; SONU starts Spotify if needed and checks it's playing a new song whose title or artist shares your words (else "unverified") |
| Local music: "play my music", "shuffle my music", "play <artist / album> from my music", "play the songs in my downloads"; pause, next, "what's playing?", stop | 🧪 #65 | SONU's own `mpv` over its IPC socket (no MPRIS plugin needed), checked by the song's position moving ([D77](DECISIONS.md)) |
| "What's playing?" | ✅ | `system.status` media: `playerctl metadata` (read-only; kept out of the audit log) |

## Files and folders

| Ability | State | How |
|---|---|---|
| Open a folder: "open my downloads", "open dev_works", "open the SONU folder" (`files.open`) | ✅ | XDG user folders, your screenshots folder, a path you typed, or a folder of that name in your home (3 levels) — found by SONU; opened in the rice's file manager (`fileManager` in `binds.lua`, Thunar) and confirmed by its window title |
| Open the newest file in a folder with its app | ✅ | see Downloads |
| Read your documents: "summarise my invoice pdf", "what does notes.txt say about parking?" | 🧪 #73 | text files as they are, PDFs with `pdftotext`, Word / OpenDocument with LibreOffice (SONU's own profile); the local model only, private ([D82](DECISIONS.md)) |
| Open a file by name; find files; recent files: "open my resume", "where is my invoice pdf?", "what files did I open yesterday?" | 🧪 #62 | the home tree by your words (hidden folders and dependencies skipped), `recently-used.xbel`; private lists; programs never opened ([D74](DECISIONS.md)) |
| Move, rename: "move my invoice to documents", "rename notes.txt to ideas" | 🧪 #63 | asks first; never overwrites; never hidden files or your main folders ([D75](DECISIONS.md)) |
| Delete — to the trash only: "delete my last download"; "restore notes.txt" | 🧪 #63 | `gio trash` (undoable; "restore …" or the file manager), asks first (a folder: high); never permanent deletion ([D75](DECISIONS.md)) |

## Apps with their own interfaces

| App | Ability | State | How |
|---|---|---|---|
| VS Code / Code - OSS / VSCodium | open a project folder or a file at a line; "open my SONU project" | 🧪 #52 | `code <folder> --goto file:line`; SONU finds the project and the file ([D65](DECISIONS.md)) |
| kitty | run one of a project's own commands ("run the tests"), "did it pass?" | 🧪 #52 | only commands the project lists (`package.json`, `justfile`, `Makefile`) or uv/cargo/go's own; asks with the exact command; a SONU window; the exit code decides ([D65](DECISIONS.md)) |
| kitty (and foot, alacritty, wezterm, ghostty, konsole, gnome-terminal) | open a terminal in a folder | 🧪 #53 | the terminal's own folder flag; checked on its shell's working folder ([D66](DECISIONS.md)) |
| git | "what did I change in SONU?", "my last commits" | 🧪 #52 | read-only `git status/diff/log` (no repository hooks); the answer is private ([D65](DECISIONS.md)) |
| WhatsApp, Telegram, Discord, mail | read unread counts ("do I have new messages?") | 🧪 #55 | the count in an open app's window title, nothing else of it ([D68](DECISIONS.md)) |
| Notifications | "what was that notification?", "read my notifications", "forget my notifications" | 🧪 #56 | opt-in; sonud watches the session bus (`busctl monitor`), keeps the latest in memory only; a private result ([D69](DECISIONS.md)) |
| WhatsApp, mail, chat apps | open a chat | 🗺 M7 | URLs; **sending messages is high risk** and always asks |
| Any app without an interface | click, type, read the screen | 🗺 M6 | accessibility (AT-SPI) or screenshot + vision (`qwen3.5` can see images) + virtual input; high risk |

## Talking and remembering

| Ability | State | How |
|---|---|---|
| **The HUD**: an island at the top centre while SONU works — one line (listening, the step running, what you said), a glow sweeping its edge while it works, voice bars, dots for the steps; it grows for the answer or a question with Yes / No buttons; stays until SONU stops talking | 🧪 #71, #86, #89 | SONU's own Quickshell window (your shell untouched), your rice's colours; sonud starts it; `[hud]` in the config ([D80](DECISIONS.md)) |

| Ability | State | How |
|---|---|---|
| Faster replies: the model stays loaded, `sonu` hands requests to the daemon (~0.8 s instead of 1.6–2.5 s) | ✅ M2 part 1 | `sonud` (`sonu daemon install`), a thin client |
| Natural replies after everything ("Kitty's up on workspace 2.", "I couldn't find a recipes folder…"), small talk, one conversation ("move **it** to 4", "play it again") | ✅ M2 part 2 | a reply written from the verified results and checked in code; the last few turns go to the planner |
| "Remember …": what an app name means ("when I say editor I mean Visual Studio Code"), your name ("call me …"), notes; "what do you remember?", "forget …" | ✅ M2 part 3 | a local memory file; aliases feed the app resolver |
| Conversations remembered: "what did we talk about yesterday?", "what did I ask you this morning?", "what did you tell me about kestrels?", "forget our conversations" | 🧪 #77 | your words and what SONU told you, kept 30 days on the laptop (0600); read back privately; forgetting asks ([D86](DECISIONS.md)) |
| Activity journal: "what was I doing yesterday / this morning?", "when did I last use btop?", "pause the journal", "delete today's journal" | 🧪 M2 part 4 | `sonud` records the focused window from Hyprland's events into a local SQLite file (30 days; password managers and private windows never); a window counts at most 30 minutes at a time; `sonu journal show/pause/resume/forget` ([D60](DECISIONS.md)) |
| Voice, push-to-talk: press the SONU key (`sonu listen`), SONU says "How can I help?", say the request, hear the reply (a notification shows what it heard and did); medium-risk steps by a spoken "yes"; pressing again stops SONU talking | 🧪 M3 part 1 | Whisper large-v3-turbo on the GPU (~0.2 s), Silero VAD for end of speech, Kokoro voice (CPU, ~0.8 s), `pw-record`/`pw-play`; high risk needs the picker ([D46](DECISIONS.md)) |
| Voice: "Hey Sonu" — off until you say "listen for Hey Sonu" (it asks first); "Hey Sonu, open kitty on 2" or "Hey Sonu" then the request; "stop listening for Hey Sonu" | 🧪 M3 part 2 | `sonud` keeps the mic open: Silero VAD finds speech, Whisper checks only its first ~1.8 s for the name at the start; everything else is dropped unheard ([D47](DECISIONS.md)) |
| A second try: when a step fails, SONU tries another way once ("move zen to 4" with Zen closed → opens it on 4) and says so | 🧪 #85 | same words, rules and questions; never after your no ([D92](DECISIONS.md)) |
| One-click modes: Fully local / Balanced / Full cloud (Control Center → Brain, `sonu settings preset`); API keys typed in the window | 🧪 #92 | more cloud asks first; keys in the keyring or a private file ([D99](DECISIONS.md)) |
| SONU's voice from the cloud (opt-in): Sarvam Bulbul (Indian English voices) or Gemini | 🧪 #91 | anything private is spoken by Kokoro on the laptop ([D98](DECISIONS.md)) |
| Hearing in the cloud (opt-in): Sarvam (Indian English, Hinglish), Groq, Gemini — compare them on your voice with `sonu voice check -m sarvam` | 🧪 #90 | only your request goes; "Hey Sonu", talking over and passwords stay local ([D96](DECISIONS.md)) |
| Cloud brains from any provider: Anthropic, Gemini, Groq, OpenRouter (free models), OmniRoute, Ollama Cloud — for planning, prompts, the screen, reading and replies, each switchable | 🧪 #87 | keys in your keyring; every send audited; local when the cloud can't answer ([D94](DECISIONS.md), [MODELS.md](MODELS.md)) |
| The brain switch: local only, a bigger local model for harder requests, or the cloud for them (Brain page) | 🧪 #84 | all local unless you allow the cloud; `sonu eval --harder MODEL` measures a pair ([D91](DECISIONS.md)) |
| SONU's Control Center: `sonu control`, "open your settings" — the brain (local models, cloud on/off), voice, heads-ups, briefing, privacy, the HUD; and your data: memory, conversations, the activity journal, the audit log — seen and deleted | 🧪 #82, #83 | every change checked, asked when it loosens, audited; SONU can't click in it ([D90](DECISIONS.md)) |
| Settings without the file: `sonu settings list`, `sonu settings set heads_up.enabled on`, `sonu settings models` | 🧪 #81 | comments kept, checked first, a loosening change needs your OK, audited, sonud restarts by itself ([D90](DECISIONS.md)) |
| Corrections mid-sentence: "open kitty on 3, no, 4", "set the volume to 30, sorry, 40", "play lofi on spotify, actually on youtube" | 🧪 #80 | corrected once before planning; unclear → left as said ([D89](DECISIONS.md)) |
| Heads-ups by itself (opt-in): "Battery's at 14% — time to plug in.", the disk nearly full, the CPU staying hot | 🧪 #79 | once each; HUD, and said only when you're free (no call, nothing fullscreen, not at night) ([D88](DECISIONS.md)) |
| Replies that sound like a person: brief when you're brief, plain when you're annoyed, your name now and then (not every time), no "anything else?" offers, the time of day known | 🧪 #78 | SONU decides the tone, the name and the length; the model phrases ([D87](DECISIONS.md)) |
| Voice: answers sooner — a reply starts after its first sentence is made (not the whole reply); "One moment." when a step takes a while | 🧪 #76 | sentence by sentence into one player; a lock keeps one thing said at a time ([D85](DECISIONS.md)) |
| Voice: a conversation — after each reply SONU keeps listening ~6 s for the next request; "thanks" ends it | 🧪 M3 | `[voice] follow_up_s` ([D48](DECISIONS.md)) |
| `sonu voice check`: read ten requests, see how well each speech model hears your voice | 🧪 M3 | Whisper large-v3-turbo vs large-v3 (or `-m` any), word errors and time; nothing kept |
| Voice: talk over SONU to stop it — it goes quiet and listens | 🧪 M3 | its own PipeWire echo canceller (WebRTC) removes SONU's voice from the mic; ≥ 0.4 s of speech that isn't SONU's own words ([D50](DECISIONS.md)) | faster-whisper (GPU) speech-to-text, Kokoro/Piper voice, wake word; high risk never approved by voice alone |
| Make a folder ("make a folder called shop-app in dev_works") | 🧪 M4 | `files.make_folder`: inside home only, the user's own name for it |
| Claude Code by voice: open it in a folder, type your words into it, `/model sonnet`, "is it done?" | 🧪 M4 part 1 | SONU-owned kitty windows (remote control over a private socket, those windows only), screen read back to confirm ([D51](DECISIONS.md)) |
| Claude Code: write the best prompt from an idea ("write the best prompt for my idea: …") | 🧪 M4 part 2 | the local model writes it; shown in full, asked, then pasted into Claude Code ([D52](DECISIONS.md)) |
| GitHub: "what are my open pull requests?" (with checks), "merge the pull request in SONU" | 🧪 M4 part 3 | `gh` (already signed in); SONU finds the PR, won't merge a draft/conflicting/failing/running one; merging is high risk ([D53](DECISIONS.md)) |
| Install software by name ("install Spotify") — verified by the owner (wtype, wlrctl) | ✅ M4.5 | official repos then the AUR (`yay`), explained and asked (high risk); installs in a SONU window with the system's own password prompt (pkexec); checked with `pacman -Q` ([D54](DECISIONS.md)) |
| Remove software by name ("uninstall vlc") | 🧪 | `pacman -Rsp` preview: the package and exactly what only it needed, in the question (high risk); refused when other software needs it or the system/SONU does; `pkexec pacman -Rs` in a SONU window (password typed or said); checked with `pacman -Q` ([D59](DECISIONS.md)) |
| "What's on my screen?", "read the error in this window", "what is open in zen?" | 🧪 M6 part 1 | grim + the local vision model (`qwen3.5:4b`); a hidden window shown first; the answer private ([D55](DECISIONS.md)) |
| The install password by voice too (opt-in: `[voice] password_by_voice = true`) | 🧪 | the prompt in SONU's install window: typed, or said ([D57](DECISIONS.md)) |
| "Press play in Spotify", "click Home in Thunar" — a button by its name | 🧪 M6 part 2 | accessibility (AT-SPI) via the system Python; asked; checked by what changed ([D56](DECISIONS.md)) |
| Click a button in an app that exposes nothing, by looking; type words into an app | 🧪 M6 part 3 | vision (0–1000 scale) + `wlrctl` pointer; `wtype` keys; high risk, checked by a fresh picture ([D58](DECISIONS.md)) | vision model (local `qwen3.5`/`gemma4` or opt-in cloud) + input; high risk, confirmed |
| Claude Code using SONU's tools (MCP): "open kitty on 3", "what's my battery?", "search the arch wiki for …" from a Claude Code session | 🧪 M5 part 1 | `sonu mcp serve` (`sonu mcp setup` prints the `claude mcp add` line); SONU's policy and checks; SONU asks on screen for words, links, typing, writing; window titles and other personal content never go to Claude ([D61](DECISIONS.md)) |
| Ollama Cloud models for prompts, harder requests (no window titles) and vision — opt-in | 🧪 M5 part 2 | `[privacy] cloud = "allow"` + `[cloud] use_for`; `ollama signin`; falls back to local; every send in the audit log (`cloud.send`); `sonu doctor` shows it and the day's use; a usage limit pauses the cloud and SONU stays local ([D62](DECISIONS.md), [D64](DECISIONS.md)) |
| Other programs' tools through MCP — local (files, notes), web (fetch, search), accounts (GitHub, …): "fetch https://example.com", "read note.txt with the files server", "list the open pull requests in thomasmartinoa/SONU" | 🧪 M5 part 3 | `[mcp.servers.<name>]` (argv, `reaches`, `read_only`, `tools`, `env`, `env_commands`); `sonu mcp check`; high risk and asked with every argument shown, unless listed read-only; answers shown to you, never to a model ([D63](DECISIONS.md)) |

## Batches

- **Batch 1** (done, verified on the laptop): play on YouTube, reminders and timers, screenshots, laptop health, the
  rice's switches.
- **Batch 2** (in progress): window states and side by side ✅ (#12); folders and the newest file, "what's playing?",
  list and cancel reminders ✅ (#13); the brightness popup, clipboard, wallpaper and theme ✅ (#14); power and OBS
  ✅ (#15). Apple Music by name isn't possible with the owner's app (see Media); local music waits for music in
  `~/Music`.

After batch 2: M2.
