# Phase 2 research — voice first (2026-10-19)

The owner's goal: "drive everything by voice — don't make me click the HUD", and for hearing: **"first test all
the models — Sarvam, Whisper, Gemini — with my voice and different words, choose the one that fits, and train a
better model for my voice later."** This file is the research for phase 2 ([`PHASES.md`](../PHASES.md)) and the
plan for the PRs that follow.

## 1. What the testing showed (the audit log, not memory)

| What | Evidence | What it means |
|---|---|---|
| Words misheard | "install cowsay" (10-15) was heard as **kauze, C-O-W-S-A-Y, Coase, CowSay, Cow Set, Cozy** in eight tries; "kitty on" → "kittyon", "Zen on" → "Zenon", "claude" missed "10–20 times" | Local Whisper (large-v3-turbo) on the owner's Indian English. A hint list ("kitty", "Zen", …) only fixes the words someone already noticed |
| The spoken password failed | "the spoken password didn't work — install it again and type the password" | A password is the worst thing to say aloud: Whisper doesn't know it, and anyone nearby hears it |
| "Cozy" installed the wrong thing | `packages.install {"name": "Cozy"}` → "dunesh wasn't installed" | The search for a misheard name matched an unrelated package — a bug to fix whatever hears |
| Choices weren't answered | "1 packages could be 'Cow Set' — no answer given"; "2 packages could be 'cozy' — no answer given" | By voice, a choice only shows HUD buttons; nothing listens for "the first one" |
| "Lock the screen" didn't ask | `desktop.shell lock_screen` → allow, **low risk** | Not a bug: low risk never asks. A decision for the owner (§5) |
| Yes/no by voice works | `confirm_by_voice` (D42): medium risk asks aloud, high risk asks aloud *and* on screen | Keep it; extend the same listening to choices |

## 2. Hearing: measure on the owner's voice, then choose

**Agreed with the owner: no more lookalike-word patches as the main fix.** Hints help the words they list and
nothing else, and every app, package, song and file name is a new word. Published benchmarks don't answer the
question either: they are read English (LibriSpeech, etc.), mostly vendor-measured, and the one Indian-English set
(AI4Bharat's [Svarah](https://github.com/AI4Bharat/Svarah), 117 speakers) is not *this* speaker, this mic, or
commands full of app names. So the first PR is a benchmark on the owner's own recordings.

### The candidates

| Model | Where | Why it's a candidate | Cost / size | Notes |
|---|---|---|---|---|
| **Whisper large-v3-turbo** | local (now) | What SONU uses | ~1.6 GB GPU, ~0.3 s | The baseline |
| **Whisper large-v3** | local | Best published Svarah result (~7.1–7.2% WER; [Svarah paper](https://arxiv.org/pdf/2305.15760)) | ~3 GB, slower | Doesn't fit next to the 4B on 8 GB — CPU or swap |
| **Parakeet TDT 0.6B v3** (NVIDIA) | local, ONNX ([onnx-asr](https://pypi.org/project/onnx-asr/)) | Very fast, small, near the top for English | ~1.3 GB GPU | Fits beside the 4B; no prompt biasing in the ONNX path |
| **Granite Speech 4.1 2B** (IBM), **Cohere Transcribe 2B**, **Qwen3-ASR 1.7B** | local (transformers) | Top of the [Open ASR leaderboard](https://www.marktechpost.com/2026/07/23/best-open-speech-recognition-asr-models-in-2026-wer-languages-latency-and-license-compared/) in 2026 (≈5.3–5.8% mean WER) | ~4 GB each | Too big to keep loaded with the 4B; benchmarked one at a time to see whether they're worth it |
| **Whisper large-v3 Indian fine-tune** (e.g. [Oriserve Hindi2Hinglish](https://oriserve.com/blog/oriserve-open-sources-india-focused-ai-speech-model-fine-tuned-on-whisper)) | local | Trained on Indian-accented speech | ~3 GB | Mostly Hindi/Hinglish data: may help, may not — measure |
| **Sarvam Saaras v4** | cloud (already wired, D96) | Built for Indian English; key terms (names SONU expects); modes *transcribe / verbatim / codemix* ([Sarvam](https://www.sarvam.ai/blogs/introducing-saaras-v4)) | ≈ ₹30 an hour of audio (vendor; [MarkTechPost](https://www.marktechpost.com/2026/09/26/sarvam-ai-releases-saaras-v4-a-speech-to-text-model-for-all-22-indian-languages-and-global-english/)) — a 4 s request ≈ ₹0.03 | The owner: "perfect, understands my Indian English better than Whisper". Claims are Sarvam's own |
| **Gemini 3.5 Transcribe** | cloud (new) | Google's speech-to-text model (Aug 2026, preview), a custom vocabulary of up to ~100 terms ([docs](https://ai.google.dev/gemini-api/docs/transcribe)) | ≈ $0.005 a minute ([Artificial Analysis](https://artificialanalysis.ai/de/speech-to-text/models/gemini)) | A different API (Interactions) from SONU's current Gemini hearing, which asks a general model (`gemini-flash-latest`) to listen — both measured |

Groq (hosted Whisper) is left out: the owner removed it (D102), and it's the same Whisper as local.

### The benchmark: `sonu voice bench`

`sonu voice check` (10 sentences, audio thrown away, every word counts the same) becomes a real benchmark:

1. **What the owner says (~70 lines, ~15–20 minutes, once).** Grouped so the results say *where* a model fails:
   - **Names SONU must get exactly:** apps (kitty, Zen, Claude Code, nvim, btop, OBS, Spotify, VS Code,
     Telegram, Thunar), packages (cowsay, wlrctl, fastfetch), files and folders (parking-notes, resume pdf,
     dev_works, sonu-test), songs and artists.
   - **Numbers:** "workspace 5", "in 4", "volume 30 percent", "remind me in 20 minutes".
   - **Answers:** yes, no, yeah, nope, "the second one", "number three", "the one in Downloads", "none of them".
   - **The owner's way of talking:** "can you open me github in workspace 3", "do one thing — put kitty in 5
     only", "okay then bye", long compound requests from the eval suite, filler ("hey okay so um").
   - **"Hey Sonu"** alone and before requests.
   - **Free speech:** a few requests said naturally, not read — the owner types what they meant after. Reading
     aloud is cleaner than real use; this keeps the benchmark honest.
2. **Kept on the laptop, with the owner's OK** (`~/.local/share/sonu/voice/bench/`, 0700, never synced): one
   WAV per line plus what it should say. Any new model can then be scored **without recording again** — and these
   recordings are the first training and test data for phase 7. Deleting the folder deletes them.
3. **Every model hears every recording**, one model on the GPU at a time (the planner is let go during the run).
   Cloud models get only these test recordings, audited first, and only with "hearing" allowed.
4. **Scored on what matters to SONU**, not only word error rate:
   - **Key words right** (%) — the app, number, package, file or song in each line, exact (after spelling
     normalisation: "2" = "two", "VS Code" = "vscode"). This is the main score.
   - **Answers right** (%) — yes/no and choices, the words that decide what SONU does next.
   - **Word error rate** — the usual measure, for comparison with published numbers.
   - **Time** from the end of speech to text (cloud includes the network), and **cost** per request.
   - Optional: does SONU's planner make the same plan from the transcript as from the true text?
5. **Results** go to the test page and `docs/MODELS.md`, per group, so "Sarvam is best at names, Parakeet at
   answers" is visible.

### Then choosing (a proposed rule — the owner decides with the numbers)

- **Requests:** the model with the best key-word score, if it answers in ≲ 1.5 s. If a cloud model wins clearly,
  it hears requests and the best local model is the fallback (offline, no key, a limit).
- **"Hey Sonu", talking over SONU, passwords:** always local (D96) — so the best *local* model matters even if a
  cloud model wins.
- **What fits:** a local model kept loaded must fit beside the 4B (about 2.5 GB free today).

### Later: a model for the owner's voice (phase 7)

Research shows adapting Whisper to accents with LoRA lowers errors (e.g. [Mixture of LoRA Experts for
multi-accent ASR, 2025](https://arxiv.org/abs/2505.20006): better than plain LoRA and full fine-tuning on
L2-ARCTIC), but there's no reliable number for "a few hours of one person". So: the benchmark recordings are the **held-out
test set** (never trained on); training data comes from real use, opt-in — each request's audio with the text the
owner confirms or corrects. When there's ~1–2 hours, fine-tune (LoRA on Whisper turbo or Parakeet) and score it on
the same benchmark. It ships only if it beats the best model on the key-word score.

## 3. Answering by voice

- **Yes / no** stays as it is (medium risk: said aloud and answered aloud; high risk: on screen too, T5). The
  benchmark's "answers" group checks it's heard right.
- **Choosing from a list by voice (new).** When a spoken request needs a choice (which file, which folder, which
  package), SONU shows the numbered list on the HUD, **says how many there are and the first few** ("Eight files
  match. First, Resume.pdf in Downloads; second, …; or say none"), and listens:
  - "one", "the second one", "number three", "the last one"; "the one in Downloads" (a word only that option's
    label has); "none" / "cancel".
  - **Only an offered option counts** (as with buttons); unclear → asked once more, then left to the HUD buttons,
    which work at the same time (the first answer wins).
  - The labels are given to the hearing model as key terms, so names in the list are heard right.
  - High-risk steps still need the on-screen picker (T5) — a voice choice never approves a high-risk action.
- **Long lists:** more than ~5 → SONU says the count and the first three, and the HUD shows them all.

## 4. Installing: no spoken passwords

- **The owner keeps "say the password"** as an option (D105) — for when they're alone; typing stays the default.
  Proposed earlier: drop it. By voice, SONU says
  "type your password in the window", and the prompt is SONU's own kitty window (`polkit_tty.py`) — or the
  owner's polkit-gnome dialog. A misheard password fails and is heard by anyone in the room.
- **Hearing the package name right** is the real problem (§1): names go to the hearing model as key terms
  (Sarvam, Gemini 3.5), and when the name isn't an exact package, SONU offers the closest packages **by voice**
  (§3) instead of "no answer given".
- **Bug:** "Cozy" chose `dunesh` — the package search must not pick a name that isn't like what was said.

## 5. Decisions for the owner — answered 2026-10-19 ([D105](../DECISIONS.md)): keep the recordings; yes, send
the test recordings to Sarvam and Gemini; English only (no code-mixing); lock asks first; the spoken password
stays an option ("say it when I'm alone, type it when someone is near")

1. **Recordings:** keep the benchmark recordings on the laptop (needed to re-test and, later, to train)?
2. **Cloud in the benchmark:** send the test recordings (only those) to Sarvam and Gemini? About 70 clips, a few
   rupees in total.
3. **Do you mix in Malayalam / Hindi words when you talk to SONU?** Then Sarvam's *codemix* mode and some
   Hinglish/Manglish lines join the benchmark.
4. **"Lock the screen":** keep it unasked (it's undone by your password), or ask first (yes/no aloud)?
5. **Passwords by voice:** remove the option, as recommended?

## 6. The PRs (in order)

1. **`sonu voice bench`** — record (kept with consent), run every candidate, score by group, results table.
   Adds Gemini 3.5 Transcribe and Sarvam's modes as engines, and Parakeet (ONNX) as a local engine.
2. **The owner records** (~15–20 min) and runs it; results posted; the hearing choice made.
3. **Hearing set to the winner** (and the local fallback), the hint/key terms per request (app names, the
   choices on screen).
4. **Choosing by voice** (§3).
5. **Installs:** typed password only, the package name heard or chosen by voice, the "Cozy → dunesh" bug.
6. The owner tests phase 2 (a test-page section).
