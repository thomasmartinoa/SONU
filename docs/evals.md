# Evals: how well does a planner model drive SONU?

`sonu eval` runs a fixed set of realistic requests through the **complete** SONU loop — a real planner model, the real
tools, policy and verifier — against a fake Hyprland desktop with a standard set of apps, then checks the final
desktop state. Your real windows are never touched.

```sh
sonu eval                              # your configured planner
sonu eval -m qwen3.5:4b -m qwen3:8b    # compare models
sonu eval -c move-by-title             # one case
sonu eval --json                       # machine-readable
```

## The cases (v4, 40 cases)

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
