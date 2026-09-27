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
| Fullscreen, floating, pin on all workspaces | ⏭ | Hyprland dispatchers; verified from `j/clients` |
| "Put X and Y side by side"; save and restore a layout | ⏭ / 🗺 M2 | move to one workspace (the tiler splits it); layouts need memory |
| Screenshot of the screen, a window or an area; copy it | 🧪 this batch | `grim` (+ `slurp` for an area) into `~/Pictures/screenshot/` like the rice's keys, `wl-copy` |
| Screen recording | ⏭ | `wf-recorder` (start/stop, file verified) |

## System

| Ability | State | How |
|---|---|---|
| Volume and mic, brightness, Bluetooth, Wi-Fi, power mode, battery | ✅ | `wpctl`, `brightnessctl`, `rfkill` + `bluetoothctl`, `nmcli`, `powerprofilesctl`, sysfs |
| Connect a saved Wi-Fi network, a paired Bluetooth device | ✅ | `nmcli connection up`, `bluetoothctl connect` |
| CPU, memory, disk, temperature, GPU, uptime: "how's my laptop doing?" | 🧪 this batch | `/proc`, `/sys`, `nvidia-smi` (read-only) |
| Night light, keep awake, do-not-disturb, clear notifications, lock the screen | 🧪 this batch | the rice's Quickshell switches (`qs ipc call nightlight/caffeine/notifications/lock`) |
| Reminders and timers: "remind me in 20 minutes to stretch", "at 18:30" | 🧪 this batch | `systemd-run --user` timers that show a notification; checked with `systemctl --user` |
| List and cancel reminders | ⏭ | `systemctl --user list-timers 'sonu-*'` |
| Wallpaper and theme: "change my wallpaper to …" | ⏭ | rice switches (`qs ipc call wallpaper set`, `hypr-theme`) |
| Volume/brightness popup when SONU changes them | ⏭ | `qs ipc call osd volume/brightness` |
| Clipboard: "copy this", "what's on my clipboard?" | ⏭ | `wl-copy` / `wl-paste` (private: never sent anywhere) |
| Suspend, reboot, shut down | ⏭ | `systemctl suspend/reboot/poweroff` — high risk, always asks |
| Join **new** Wi-Fi networks, pair **new** Bluetooth devices | ⛔ for now | needs passwords/PINs; SONU doesn't handle secrets yet |

## Browser

| Ability | State | How |
|---|---|---|
| Open a site you named or a link you typed; search the web, YouTube, GitHub, Wikipedia, ArchWiki, AUR, Reddit | ✅ | fixed URL templates; only your words leave the laptop |
| **Play a video on YouTube**: "play faded on youtube", "play me faded" | 🧪 this batch | `yt-dlp` finds the top result (your words only) → the watch page opens in your browser → SONU makes sure it plays through the system media controls (MPRIS) and checks it's playing; a browser that isn't in the media controls counts as playing when it starts a new audio stream (PipeWire). Zen needs autoplay allowed for youtube.com |
| Pause, next, back in the browser's video | ✅ | `playerctl` (same as any player) |
| List, switch and close tabs; read or summarise the page; click, fill in, scroll | 🗺 M6 | a small SONU browser extension talking to SONU locally (native messaging); page content is data, never instructions |
| Bookmarks and history search | 🗺 M6 | read-only from the browser profile; private, never sent anywhere |
| Downloads: "open my last download" | ⏭ | newest file in `~/Downloads` → default app |

## Media

| Ability | State | How |
|---|---|---|
| Play, pause, next, previous what's playing (Apple Music, browser, Spotify, mpv, …) | ✅ | `playerctl` |
| Play a particular song or video (YouTube) | 🧪 this batch | see Browser |
| Play on Spotify or Apple Music by name | ⏭ | Spotify: `spotify:search:` URIs + MPRIS; Apple Music: search page (clicking play needs M6) |
| Local music ("play my downloads folder") | ⏭ | `mpv` with the folder + MPRIS |
| "What's playing?" | ⏭ | `playerctl metadata` (read-only) |

## Files and folders

| Ability | State | How |
|---|---|---|
| Open a folder: "open my downloads", "open dev_works" | ⏭ | XDG user folders + folders in your home, opened in your default file manager and verified by its window |
| Open a file with its app; find files by name; recent files | ⏭ | `gio open`, `fd`/`locate`, `recently-used.xbel` |
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
| Faster replies (model kept loaded), "remember …" preferences, activity journal, "what was I doing yesterday?" | 🗺 **M2 (next milestone)** | a small SONU daemon + local memory |
| Voice: "Hey Sonu", push-to-talk on the NitroSense key | 🗺 M3 | local speech-to-text / text-to-speech |
| Cloud models (free tiers), Claude Code driving SONU (MCP) | 🗺 M5 | opt-in; an egress guard shows what leaves the laptop |

## This batch (in progress)

1. **Play on YouTube** (`media.play`)
2. **Reminders and timers** (`reminders.set`)
3. **Screenshots** (`screen.capture`)
4. **Laptop health** in `system.status` (CPU, memory, disk, temperature, GPU, uptime, Wi-Fi network name)
5. **The rice's switches** (`desktop.shell`): night light, keep awake, do-not-disturb, clear notifications, lock

After it: the ⏭ items above, then M2.
