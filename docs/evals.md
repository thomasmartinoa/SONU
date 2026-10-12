# Evals: how well does a planner model drive SONU?

`sonu eval` runs a fixed set of realistic requests through the **complete** SONU loop — a real planner model, the real
tools, policy and verifier — against a fake Hyprland desktop with a standard set of apps, then checks the final
desktop state. Your real windows are never touched.

```sh
sonu eval                              # your configured planner
sonu eval -m qwen3.5:4b -m qwen3:8b    # compare models
sonu eval -c move-by-title             # one case
sonu eval --json                       # machine-readable
sonu eval --cloud                      # also your [cloud] model (Ollama Cloud), planning as SONU will: no titles
```

## The cases (v39, 159 cases)

Defined in [`src/sonu/evals/cases.toml`](../src/sonu/evals/cases.toml). They cover launching (plain, on a workspace,
by role: "browser", "terminal"), placing two apps on two workspaces, moving windows by app / focus / title, switching
workspaces, going to an app, closing one or all windows of an app, reusing an already-open app, describing the
desktop, chit-chat, out-of-scope requests, an uninstalled app, **an ambiguous name that must lead to a question**,
**a prompt-injection attempt in a window title**, and a four-step "set up my workspace" request.

v2 adds six cases from the author's real-desktop runs: a request that is only **partly possible** (open Bluetooth
Manager *and turn on Bluetooth* — SONU must say what it can't do, not report done), a website by name, a site search,
a search in the browser the user named, a link the user typed, and **an injected title that tries to make SONU search
for (and so leak) a secret**.

v3 adds eight **system settings** cases on a fake system (nothing real is changed): Bluetooth on (the adapter starts
rfkill-blocked, as on the owner's laptop), volume and mic together, "a little" quieter, a brightness level, pausing
media, battery-saver mode, **turning Wi-Fi off (must ask first)**, and a battery question. The partly-possible case
now asks to *pair headphones* (Bluetooth itself is supported). One more came from real use: **"next song" with an
Apple Music window open** must skip the track, not focus the window (the pre-settings planner did the latter).

v4 adds five more from the owner's first settings session: **"play me faded"** (SONU can't pick a song — it must
not report done), "play faded on youtube" (search, and say it can't start the video), connecting to a **saved** Wi-Fi
network (asks, because it leaves the current one), refusing an **unknown** network, and connecting headphones found
by kind while Bluetooth starts off.

v5 (capability batch 1) turns the two "play" cases into real playback (the browser holds the video back, SONU
must press play and confirm it) and adds reminders, a timer, a window screenshot, night light + do-not-disturb, keep
awake and a laptop-health question.

v6 (batch 1 tried on the laptop) adds three findings from real use: "turn off night light and turn off do not
disturb" must not clear the notifications, "how is my laptop doing?" must report the laptop's health, and "play
faded on youtube in chromium" while Zen shows "Alan Walker - Faded" must still search for the user's words and play
in Chromium. A 50th case plays in a browser that isn't in the media controls but autoplays: SONU must confirm it
from the browser's new audio stream.

v7 (batch 2, windows) adds fullscreen / exit fullscreen / maximize, floating, pinning to every workspace, and side by
side (two apps; two windows of one app). Cases can now start windows floating or fullscreen and expect a window's
fullscreen/floating/pinned state or two windows next to each other (`expect_side_by_side`).

v8 adds opening folders ("my downloads", "the SONU folder" — found by SONU) and the newest download, "what's
playing?", and listing and cancelling reminders. Every case's home has `Downloads` (with `cat.png`), `Documents`,
`Pictures/screenshot`, `dev_works/SONU` and `hypr-dotfiles`; the file manager is Thunar, like the owner's.

v9 adds the rice's theme (by name, light mode, "which theme?"), the next wallpaper and reading the clipboard. The
fake system has the owner's theme names (hyprmono, hyprmono-light, tokyo-night, catppuccin-mocha).

v10 adds power (the step asks; SONU can't watch a suspend, so the task ends "unverified") and OBS. Every case runs a
fake OBS WebSocket server with the owner's scenes ("Scene Main", "Scene only screen", "Scene 3"); cases can set
OBS's state (`obs = {…}`) and expect it (`expect_obs = {…}`).

A golden-plan test (`tests/unit/test_evals.py`) proves every case is achievable with SONU's real tools, so a failing
case is a model (or prompt) problem, not an impossible test. The planner's few-shot examples deliberately use
*different* apps than the cases, so the score measures generalisation rather than memorisation.

## Results

Measured in a CPU-only cloud container (4 cores, no GPU) with Ollama 0.34.4, temperature 0. **Planning times from
this environment are meaningless for your laptop** (GPU inference is much faster); only pass/fail is comparable.

| Date | Model | Change | Passed | Remaining failures |
|---|---|---|---|---|
| 2026-09-24 | `qwen3.5:4b` | first prompt | **15 / 20** | ignored title words; dropped "all"; "take me to X" planned as a move; rewrote "code" as "VS Code"; decided "there is no browser" in the injection case (it did *not* close anything) |
| 2026-09-24 | `qwen3.5:4b` | clearer rules, app labels, request-word safety nets | **19 / 20** | shortened "VS Code" to "code" and opened Code - OSS without asking |
| 2026-09-24 | `qwen3.5:4b` | resolve the user's own fuller phrase ("VS Code") over the model's shortened one | **20 / 20** | — |
| 2026-09-25 | `qwen3.5:4b` | after the first real-desktop fixes (hidden helper entries, terminals open new windows, answers to questions) | **18 / 20**, then 2 / 2 re-run | the first two cases timed out while the model loaded on a CPU busy with the test suite; both passed when re-run — no planning failures |
| 2026-09-25 | `qwen3.5:4b` | v2 suite (26 cases): web tools, `unsupported` parts, SONU's own terminal ignored | **26 / 26** | — |
| 2026-09-25 | `qwen3.5:4b` | v3 suite (34 cases): system settings | **33 / 34** | "set the volume to 30 and mute the mic" muted the *speaker* (`target` defaulted to speaker) — verified as a speaker mute, so it would have looked done |
| 2026-09-25 | `qwen3.5:4b` | `target` required (speaker or mic), a mic example | **34 / 34** | — |
| 2026-09-25 | `qwen3.5:4b` | v4 suite (40 cases): honest "play <song>", Wi-Fi/Bluetooth connect | **38 / 40** | refused to open Bluetooth Manager ("cannot"); added an unasked "can't join a new network" to a working connect |
| 2026-09-25 | `qwen3.5:4b` | "do every part you can; list only what was asked" | **37 / 40** | decided by itself that Wi-Fi networks weren't saved; left `site: youtube` out of a YouTube search |
| 2026-09-25 | `qwen3.5:4b` | never decide a network/device is missing; search the one site the user named | **39 / 40** | opened Bluetooth Manager for "pair my headphones" and reported done (pairing wasn't listed as impossible any more) |
| 2026-09-25 | `qwen3.5:4b` | pairing listed again; "opening an app is not doing the task in it" | **40 / 40** | — |
| 2026-09-25 | `qwen3.5:4b` | v5 suite (46 cases): play on YouTube, reminders, screenshots, the rice's switches, laptop health | **45 / 46**, then 3 / 3 re-run | "search youtube for networkchuck" searched for "NetworkChuck" — right, but the eval compared window titles case-sensitively. The check now ignores case; the case and both play cases passed when re-run (after two timeouts while the model loaded) |
| 2026-09-27 | `qwen3.5:4b` | v6 suite (49 cases) on the laptop's GPU; rules for laptop health, do-not-disturb off, play queries; a "message: never add facts the user didn't give" rule | **48 / 49** | "go to youtube" launched the browser app instead of `web.open`. Dropping the message rule fixed it (the play-query rule alone already stopped the made-up artist) |
| 2026-09-27 | `qwen3.5:4b` | v6 without the message rule; the planner sends back queries that aren't the user's words | **49 / 49** | — (the repair round is visible as `attempts: 2` in `play-with-a-title-on-screen`). Not in the suite: "turn off the night light and do not disturb" stays "…and turn DND on", even with that exact sentence as an example |
| 2026-09-27 | `qwen3.5:4b` | + `song-in-a-browser-without-media-controls` (playback confirmed by sound) | **50 / 50** | — |
| 2026-09-27 | `qwen3.5:4b` | + `play-in-the-named-browser` (the named browser is used even if the plan leaves it out) | **51 / 51** | — |
| 2026-09-27 | `qwen3.5:4b` | v7 (57 cases): window states and side by side | **57 / 57** | — |
| 2026-09-27 | `qwen3.5:4b` | + `maximize-this-window` (found on the laptop: "maximize" was planned as fullscreen); rule "`maximize` is maximized (not fullscreen)" | **1 / 2** of the two | "make this window fullscreen" became *maximized*. Reworded: "the state is the user's word" |
| 2026-09-27 | `qwen3.5:4b` | v7 (58 cases) with that wording | **58 / 58** | — |
| 2026-09-27 | `qwen3.5:4b` | v8: six cases for folders, the newest download, "what's playing?", reminders | **5 / 6** of the six | "open my last download" → `place: "my last download"`, no `newest`. Fixed in the tool (the user's "last" means the newest file), not the prompt |
| 2026-09-27 | `qwen3.5:4b` | v8 (64 cases) | **64 / 64** | — |
| 2026-09-27 | `qwen3.5:4b` | v9: five cases for theme, wallpaper, clipboard | **4 / 5** of the five | "switch to the tokyo night theme" → `{"action": "next"}`. Fixed in the tool (a theme the user's words name wins over next) |
| 2026-09-27 | `qwen3.5:4b` | v9 (69 cases) | **69 / 69** | — |
| 2026-09-27 | `qwen3.5:4b` | v10 (77 cases): power (suspend asks, mute then shut down) and OBS (record, stop, scene, mute, go live asks, status) | **77 / 77** | — |
| 2026-09-27 | `qwen3.5:4b` | + play on Spotify, play on Apple Music (explained, not played elsewhere); Spotify rule in the prompt | **78 / 79** | "change my wallpaper" asked which wallpaper. Rule: it's the next one (and the stale "light/dark is toggle" wording fixed) |
| 2026-09-27 | `qwen3.5:4b` | v10 (79 cases) | **79 / 79** | — |
| 2026-09-27 | `qwen3.5:4b` | + the rule "it / that / again refer to the recent conversation" (M2 part 2) | **78 / 79**, then **79 / 79** | the first run's "go to youtube" launched the browser app instead of `web.open`; it passed alone twice and in the next full run (variance, seen before after prompt changes) |
| 2026-09-27 | `qwen3.5:4b` | v11 (83 cases): "remember …" (alias, name, note, list) + memory rules and a "call me" example | **3 / 4** of the new four, then **81 / 83** | "call me Martin" copied the prompt's reminder example (fixed: reminder text must be the user's words + an example); "go to youtube" → the browser app and "play faded on youtube" → browser "youtube" (both fixed in code, not the prompt) |
| 2026-09-27 | `qwen3.5:4b` | v11 with those fixes | **83 / 83** | — |
| 2026-09-28 | `qwen3.5:4b` | v12 (85 cases): "listen for / stop listening for Hey Sonu" (`voice.wake`), with a rule in the prompt | **84 / 85**, then **85 / 85** twice | the one failure of the first run wasn't recorded (the output scrolled past); the next two runs passed every case |
| 2026-09-28 | `qwen3.5:4b` | v13 (86): + `wake-word-misheard` ("Listen for his own." — found on the laptop), and a rule "listen for his own … also means Hey Sonu" | **84 / 86** | the new case turned the wake word *off*; "what do you remember about me?" → reminders.list |
| 2026-09-28 | `qwen3.5:4b` | the rule reworded, then shortened | **85 / 86** | both fixed, but `close-one` ("close kitty") now closed the *focused* window, every time — the rule itself (any wording) changed that plan; with no misheard-name rule the model already plans `wake-word-misheard` right |
| 2026-09-28 | `qwen3.5:4b` | v13 with no misheard-name rule; "what do you remember (about me)? is memory.list, not reminders.list" | **86 / 86** twice | — |
| 2026-09-28 | `qwen3.5:4b` | v14 (95): nine complex multi-step requests (apps + search + volume; night light + brightness + pause + reminder; side by side + DND + dark; an unsupported install; three questions; move + show + maximize; a dev setup; two spoken, rambling requests from real use); a "play …" in unsupported is sent back | **9 / 9** of the new ones, then **95 / 95** twice | — |
| 2026-09-28 | `qwen3.5:4b` | v15: four Claude Code cases (make a folder + open it; open with the trust question; open + "tell it: …"; open + switch to Sonnet) | **2 / 4** | "open claude code in the SONU folder" also made a folder (copying the example); "…and tell it: …" dropped the typing step |
| 2026-09-28 | `qwen3.5:4b` | the example shows make + open + tell; a rule "make a folder only when asked" | **4 / 4**, then + `claude-code-is-not-the-code-app` (found on the laptop: the app "code" was launched) with a check that sends such a plan back | **100 / 100** twice |
| 2026-09-28 | `qwen3.5:4b` | v16: + `claude-code-prompt-from-an-idea`; a rule for the prompt writer | **101 / 101**, then **100 / 101** | `remember-app-alias` ("…I mean code oss") planned with app "code" — every time after the rule; fixed in code (the user's longer app name wins), then **101 / 101** twice |
| 2026-09-28 | `qwen3.5:4b` | v17: three GitHub cases (list; merge asks; failing checks not merged) | **1 / 3** | "merge the pull request …" was planned as only listing them (github.prs) |
| 2026-09-28 | `qwen3.5:4b` | + a merge example | **104 / 104** twice | — |
| 2026-09-28 | `qwen3.5:4b` | v18: three install cases (an app; "obs studio" in the user's words; already installed); `complex-with-an-unsupported-part` now orders a pizza (installing is possible) | **107 / 107** twice | — |
| 2026-09-28 | `qwen3.5:4b` | v19: reading the screen (the screen; this window; Zen on another workspace — found on the laptop) with a stand-in vision model; a rule "focus it first, then read" | **108 / 110** | the rule broke `three-step-setup` (partial), and the model still planned only the read; the rule was dropped and `screen.read` now shows the window itself |
| 2026-09-28 | `qwen3.5:4b` | v19 without the rule | **110 / 110** twice | — |
| 2026-09-28 | `qwen3.5:4b` | v20: + `press-a-button-sonu-doesnt-know`; a rule for screen.press | **109 / 111** | `three-step-setup` ("the browser" → "firefox") and `maximize-this-window` — then `three-step-setup` failed on **main** too, 3/3: the model had drifted; the rule was dropped, and an app name that isn't the user's word is now sent back in code |
| 2026-09-28 | `qwen3.5:4b` | v20 with that check (and a sent-back unsupported "press …") | **111 / 111** twice | — |
| 2026-10-04 | `qwen3.5:4b` | v21: + `type-into-this-window` (screen.type, not Claude Code's dev.type) | **112 / 112** twice | — |
| 2026-10-04 | `qwen3.5:4b` | v22: + `type-here-no-claude`, `open-terminal-and-type` (found by voice: typing went to Claude Code's dev.type) | **114 / 114** twice | — |
| 2026-10-10 | `qwen3.5:4b` | v23: + `uninstall-an-app`, `get-rid-of-an-app` (packages.remove) | **116 / 116** twice | — |
| 2026-10-11 | `qwen3.5:4b` | v24: + `what-was-i-doing-yesterday`, `the-last-hour-from-the-journal`, `when-did-i-last-use`, `pause-the-journal` (the activity journal) | **120 / 120** twice | a first run, with other requests on the GPU at the same time: 119 — `claude-code-and-a-prompt` once (passed 3/3 on its own) |
| 2026-10-11 | `gpt-oss:120b-cloud` (Ollama Cloud, free plan, no window titles) | v24, first cloud run (~1.4 s mean) | **115 / 120** | SONU's: `folder-and-claude-code`, `claude-code-is-not-the-code-app` (the folder named by its path), `website-by-name` ("youtube.com") — fixed in #48, then passed on both models; the model's: `partly-unsupported` (connected the paired headphones, no unsupported part), `github-open-prs` (asked which repository) |
| 2026-10-11 | `qwen3.5:4b` | v25: + `resume-song`, `night-light-off-and-dnd`, `github-user-search` (open questions from the laptop) and planner checks for resuming, "off … and do not disturb", pairing, "my pull requests" | **123 / 123** (~1.0 s mean) | — ; `two-apps-two-workspaces` (the M1 example) 10/10; `gpt-oss:120b-cloud` passes its two former misses and the three new cases, twice |
| 2026-10-11 | `qwen3:8b` (`--deep`, thinking on) | v25 | **113 / 123** (10.9 s mean) | `close-one`, `prompt-injection-in-title`, `headphones-connect`, `list-reminders`, `what-do-you-remember`, `wake-word-misheard`, `claude-code-prompt-from-an-idea`, `whats-on-my-screen`, `read-a-window-on-another-workspace`, `the-last-hour-from-the-journal` — not a better deep planner than `qwen3.5:4b` |
| 2026-10-11 | `qwen3.5:4b` | v26: + `open-project-in-editor`, `open-file-at-a-line`, `run-project-tests`, `what-did-i-change`, `last-commits` (D65); planner checks for "open the <name> folder" (the file manager), closing the focused window only when the user means it, maximize ≠ fullscreen | **128 / 128**, twice (~1.2 s mean) | — ; with a second new prompt example `read-a-window-on-another-workspace` failed every time (kept to one example); before the checks `close-one` closed the focused window, `maximize-this-window` went fullscreen, `open-a-project-folder` opened VSCodium |
| 2026-10-11 | `qwen3.5:4b` | v27: + `terminal-in-a-folder`, `folder-and-a-terminal-there` (D66); plan checks: a terminal with no folder is apps.launch, no file manager for a terminal's folder | **130 / 130**, twice (~1.2 s mean) | — |
| 2026-10-11 | `qwen3.5:4b` | v28: + `save-a-layout`, `restore-a-layout`, `list-layouts` (D67) | **133 / 133**, twice (~1.2 s mean) | — |
| 2026-10-11 | `qwen3.5:4b` | v29: + `unread-messages`, `unread-in-one-app` (D68) | **135 / 135**, twice (~1.2 s mean) | — |
| 2026-10-11 | `qwen3.5:4b` | v30: + `what-was-that-notification`, `read-my-notifications` (D69) | **137 / 137**, twice (~1.2 s mean) | — |
| 2026-10-11 | `qwen3.5:4b` | v31: + `switch-to-a-tab`, `close-a-tab`, `list-tabs` (D70, a fake browser) | **140 / 140**, twice (~1.2 s mean) | — ; before two tool fixes `open-last-download` opened the folder and `read-a-window-on-another-workspace` read the whole screen (the model's plans shifted with the new rule) |
| 2026-10-11 | `qwen3.5:4b` | v32: + `summarise-this-page`, `ask-about-a-tab` (D71, the fake browser's pages, a stand-in page reader) | **142 / 142**, twice (~1.1 s mean) | — ; before a tool fix `unread-messages` failed (the model named "whatsapp desktop", not the user's words) |
| 2026-10-11 | `qwen3.5:4b` | v33: + `click-a-link-on-the-page`, `search-on-the-page`, `scroll-the-page` (D72, the fake page's link, button and fields) | **145 / 145**, twice (~1.1 s mean) | — ; before a tool fix `remember-a-note` failed (planned as an alias for an app) |
| 2026-10-11 | `qwen3.5:4b` | v34: + `find-a-page-in-history`, `open-a-bookmark` (D73, a fake Zen profile in the fake home) | **147 / 147**, twice (~1.1 s mean) | — |
| 2026-10-11 | `qwen3.5:4b` | v35: + `open-a-file-by-name`, `find-a-file` (D74, two older files in the fake home) | **149 / 149**, twice (~1.1 s mean) | — |
| 2026-10-11 | `qwen3.5:4b` | v36: + `move-a-file`, `rename-a-file`, `delete-the-last-download` (D75, a fake `gio trash`) | **152 / 152**, twice (~1.0 s mean) | — |
| 2026-10-12 | `qwen3.5:4b` | v39: + `why-is-my-fan-loud`, `did-anything-crash`, `kill-a-program` (D81, two fake processes, a fake kill) | **159 / 159**, twice (~1.3 s mean) | — ; before four fixes `why-is-my-fan-loud` read only the temperature, `get-rid-of-an-app` stopped the app, `resume-song` paused (the model's plans shifted with the new rule) |
| 2026-10-11 | `qwen3.5:4b` | v38: + `play-my-music`, `shuffle-an-artist` (D77, a fake mpv) | **156 / 156**, twice (~1.2 s mean) | — ; before three fixes `complex-alone-music` played the user's own files, `read-a-window-on-another-workspace` read a page, `github-user-search` searched the web (the model's plans shifted with the new rule) |
| 2026-10-11 | `qwen3.5:4b` | v37: + `copy-a-files-path`, `copy-the-last-screenshot` (D76) | **154 / 154**, twice (~1.2 s mean) | — ; before two tool fixes `copy-a-files-path` copied nothing (no name in the plan) and `dnd-off-keeps-notifications` cleared the notifications (the model's plans shifted with the new rule) |

Every failure in these runs was SONU **asking or stopping**, except the last one, which is why it was fixed with a
deterministic guard rather than more prompting: an assistant that guesses between two apps is worse than one that
asks.

## Reading the results honestly

- Small models react strongly to wording: each rule change above fixed one case and broke another until the
  wording settled. Always re-run the **whole** suite after touching the prompt, never only the failing case.

- 20 cases is a smoke test, not a benchmark. Passing all of them means "no known gaps", not "always right".
- Temperature 0 makes runs close to deterministic, but not guaranteed identical across Ollama versions or hardware.
- A timeout is reported as an error, not a pass: on slow hardware, re-run the failing cases (`-c <id>`) before
  reading anything into them.
- The fake desktop models real Hyprland behaviour (single-instance hand-off, misplaced windows, silent "ok" on an
  unmatched selector), but real apps can still surprise it — that's what `pytest -m hyprland` and real use are for.
- To add a case: append it to `cases.toml`, add its golden plan to `tests/unit/test_evals.py`, and run the eval
  before and after your change.
