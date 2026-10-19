# SONU — the plan, phase by phase (from the owner's testing, 2026-10-18)

The owner tested everything on the test page (161 marks: 122 ✔, 39 ✖), rated 32 benchmark requests, benchmarked
three more Ollama Cloud models, and wrote down where SONU should go next. This file turns all of it into phases.
**Each phase starts with its own research or design session, then PRs, then the owner tests it** before the next
one starts. Every ✖ below is quoted (shortened) from the owner's notes, so a fresh session knows what "fixed" means.

Order: make what exists work reliably first (1–4), then grow (5–7). The Claude Code mod (side track) is separate:
it changes how the owner works on SONU, not SONU itself.

## Decisions made from the testing

- **Planner: `qwen3.5:4b`** (the owner: "fast, less resources, best in answering"). Fine-tuning it for SONU comes
  in phase 7. (The owner wrote "qwen3.6 4b"; there is no Qwen3.6 at 4B on Ollama — the 4B in use is qwen3.5.)
- **No 9B for hard requests** (`models.harder = "same"`): the 4B alone passes 171/171; adding the 9B made plans
  slower (2.8 s) and got 170. The owner didn't want it either.
- **Cloud providers kept:** Ollama Cloud (works), Anthropic (kept, not yet tested), Gemini (to test once it
  works — see phase 1), **Sarvam for hearing only** ("perfect, understands my Indian English better than
  Whisper"); SONU's voice stays Kokoro (local) to keep Sarvam's cost down. **Removed from the UI:** Groq,
  OpenRouter, OmniRoute.
- More Ollama Cloud planners measured by the owner: `nemotron-3-super:cloud` 159/171 (9.6 s),
  `nemotron-3-nano:30b-cloud` 157/171 (9.6 s), `gemma4:31b-cloud` 81/171 — most of its misses were "not valid plan
  JSON", a format problem on SONU's side to look at, not the model's intelligence.

## Phase 1 — Reliable: nothing stale, every switch really switches

The bugs that make SONU feel broken everywhere. Nothing else is trustworthy until these are fixed.

- ✅ (#102, D103) **Old results leak into new answers.** Seen many times, with every model: "play something relaxing" said
  "Copied the path of parking-notes"; "hi sonu, long day huh" answered with CPU usage; "can you book a train
  ticket" first answered with system usage; research said "No media player is running" three times in a row;
  "search the arch wiki" showed the folder list from an earlier "make a folder" question; "how are you" showed
  folder paths. "It needs a restart to be normal again." → find where a past step, reply or pending question
  survives into the next request (conversation follow-ups, pending choices, the reply writer) and test it.
- ✅ (#103, D104: cloud failures said, Gemini works; #106: the old planner unloaded) **Switching models doesn't fully switch.** Choosing Gemini or Groq still planned with the local model;
  `qwen3.6:35b-a3b` stayed loaded in the GPU after switching away; "changing models in the settings is fully
  bugging". → a switch must take effect, say so, and unload the model it replaced.
- ✅ (#105: they did stick — the window didn't show it) **API keys don't stick.** A key pasted in the Control Center is gone after a restart, and long keys spill out
  of the input box. Blocks Sarvam and Gemini.
- ✅ (#106) **`sonu voice check` crashed** with `CUBLAS_STATUS_ALLOC_FAILED` loading Whisper large-v3 after turbo (GPU
  memory): free one model before the next, or fall back to the CPU.
- ✅ (#106) **Times:** the line says "plan in 1.1 s", but the answer took ~3 s. Show the whole time, not only planning.
- ✅ (#106) **Wi-Fi off in Balanced:** it worked but was too slow. Notice there's no network and go local at once.

## Phase 2 — Voice first: hear right, answer by voice

The owner: "drive everything by voice — don't make me click the HUD."

**Research: [`research/phase-2-voice.md`](research/phase-2-voice.md) (2026-10-19).** Hearing is chosen by
measuring, not by patching words: `sonu voice bench` records the owner once (~70 lines: names, numbers, answers,
their way of talking, free speech), keeps the recordings on the laptop, and scores every candidate — local
Whisper turbo / large-v3, Parakeet, the 2B leaderboard models, an Indian-English Whisper fine-tune; Sarvam
Saaras v4 and Gemini 3.5 Transcribe in the cloud — on *key words right*, answers right, word error rate, time and
cost (✅ #112: the bench is built; ✅ #114: the owner's results — Sarvam 80 % key words, Whisper turbo 69 %, Gemini 3.5 Transcribe best at names but ~4 s; **local Whisper stays the default**, [D106](DECISIONS.md)). ✅ #115: a heard name is matched by sound to the apps and projects that exist (Whisper turbo 69 % → 76 % key words, D107). ✅ #118: "is yay installed?" answered (`packages.check`). ✅ #116: a workspace number heard as a word is the number ("in night" → 9, "in fire" → 5; turbo → 79 %, D108). Next: Gemini 3.5 Transcribe as a second opinion for an unknown name. Then: hearing set to the winner, choosing from lists by voice, installs with a typed password only. The
recordings become the test set for phase 7's model of the owner's voice.

- **Sarvam hears requests, Kokoro speaks** (needs the key fix from phase 1). Check that "Hey Sonu", talking over
  SONU and passwords stay local, and that only hearing is billed.
- **Answer questions by voice:** yes/no ("lock the screen" didn't ask out loud), and **pick from a list by
  voice** ("the second one", "the one in Downloads") for windows, files and folders shown in the HUD. Only an
  offered answer counts; high-risk steps still need the picker (T5).
- **Mishearings with local Whisper:** "kitty on" → "kittyon", "Zen on" → "Zenon", "claude" not caught ("I told it
  10–20 times"), "cowsay". Sarvam should fix most; keep better hints for the local fallback.
- **"Hey Sonu" on the owner's voice:** training gave a low result; decide in phase 7 whether to train a real
  wake-word model on their voice.
- **Installing needs the password:** "install cowsay" failed at authentication (`Not authorized`, exit 127) —
  the password-by-voice / on-screen flow doesn't reach pkexec. Fix, or say clearly what to do.

## Phase 3 — Every ability does what it says

The other ✖ from the test page, grouped. Each becomes a regression test ("found on real hardware") and a fix.

- **Windows:** "move this window to workspace 2" moved it but didn't follow it there; "what's open?" lists every
  workspace even when asked about the current one.
- **The rice:** "switch to the tokyo night theme" cycles themes instead of choosing that one; "change my
  wallpaper" does nothing (read the rice's real Quickshell IPC names first).
- **The laptop:** "why is my fan so loud?" names busy programs but not the temperature and fan speed (the
  "slow" answer already has them).
- **Files — search the whole PC,** not only home: mounted drives (`/mnt/storage`, Games, E_files…), asked first
  for folders outside home. "open claude code in sonu-test" opened the SONU project; another time it made a new
  "sonu test" folder instead of finding `sonu-test` → better folder matching (spaces, `-`, `_`, the nearest
  match, ask when unsure). "open my parking-notes" didn't work. "where is my invoice pdf?" found it but answered
  poorly and sometimes opened it in Chromium.
- **Documents:** summaries "good but not perfect", big PDFs don't work; **scanned PDFs** (a photo of a mark
  list) can't be read → read them with OCR or a vision model.
- **Research:** "find out how kestrels hunt and summarise it" sometimes opened a YouTube search instead
  (sometimes worked on the second try). "look up hyprland plugins on github" opened it but said it couldn't
  confirm.
- **Browser actions** (extension): explain the selected text, scroll ("already at the bottom" when it wasn't),
  click a link, type into a search box — none worked. Check the extension is installed and connected first.
- **Music:** "play my music" opened random YouTube (once it played the local songs); "next song" and "what's
  playing?" don't work.
- **Claude Code:** "copy the path … then paste it in kitty" copies but doesn't paste.
- **Memory bugs (now):** "what do you remember?" mixes facts up ("your name 21", "wifi on the bridge");
  "forget the wifi note" says forgotten but it's still there. "when did I last use btop?" answers with random
  things from the morning.
- **Small:** `sonu setting preset` (no "s") went to the planner as a request — suggest the right command.

## Phase 4 — The Control Center, redone

"The UI and UX is kind of bad — make it better, with the right options, all tested working."

- Research and design first (what to show, in what order, for whom), then rebuild.
- Every option tested end to end on the laptop: change → SONU restarts → the change really took effect (phase 1).
- Provider list cut down to the kept ones (Ollama Cloud, Anthropic, Gemini, Sarvam for hearing); key fields
  that fit long keys, show ✔ once saved, and survive restarts.
- Model choice that only offers models that can plan, shows what's installed, and unloads the old one.

## Phase 5 — Memory v2

"A better memory: fast, efficient, and remembers well." Research first: what kinds of memory SONU needs (facts
about the owner, preferences, aliases, notes, what happened), how to store and find them (structured facts vs.
embeddings, local only), how forgetting works (really gone, and provably), how to stop facts mixing, and how
to keep it fast on the 4B. Then build it, with the Control Center's memory page on top.

## Phase 6 — Personal connectors

"Gmail, Notion, notes, tasks — personal stuff, so I can use it as an assistant." Research first:

- **How to connect:** MCP servers (SONU already runs other MCP servers' tools as untrusted, D63) vs. each
  service's API with OAuth; tokens in the keyring.
- **What first:** reading (unread mail, today's calendar, my tasks, find a note) before anything that sends or
  changes; then actions, each asked first (sending mail is high risk).
- **Privacy:** mail and notes are private results (never sent to a cloud model unless allowed, never spoken by
  the cloud voice); a threat-model row per connector.
- Candidates: Gmail, Google Calendar, Notion, Google Tasks / Todoist, local notes (Markdown / Obsidian folder).

## Phase 7 — SONU's own models

The owner's idea, to research properly before deciding anything:

- **Hearing:** a local model as good as Sarvam with Indian English and slang (e.g. AI4Bharat's Indian-English
  models, Whisper fine-tuned on Indian English, NVIDIA Parakeet) — or fine-tune one on the owner's voice.
- **Planning:** fine-tune the 4B on SONU's own data (the eval cases, real requests from the audit log, corrected
  plans), e.g. a LoRA with Unsloth, to make it more accurate without making it bigger.
- **Both in one** (a speech-in model that plans directly) — likely the fastest, but the hardest; research only.
- **Wake word:** a small trained model on the owner's voice, if the transcript approach stays weak.

## Side track — a Claude Code mod that picks the model and effort

Not part of SONU: a mod for the owner's Claude Code that looks at each task and picks the model and effort to
match (a quick question → a small model and low effort; a big change → a bigger one), so tokens aren't wasted.
Research first: what Claude Code's mods and hooks can change (the model, the effort, or only suggest), and how
to classify a task cheaply. Built with Claude Code's own mod support, outside this repo.

## How to work through it

1. Start a phase with a research or design session; its result goes into this file (or a doc it links).
2. Build in small PRs; each ✖ above becomes a regression test and a fix; tick it here.
3. The owner tests the phase on the laptop (the test page gets a section for it); then the next phase.
