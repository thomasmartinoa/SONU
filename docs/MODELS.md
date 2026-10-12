# Which model for which job

Research done for the owner on 2026-10-12 (D94). SONU's measurements are from the owner's laptop (RTX 3070 Ti
Laptop, 8 GB); everything else is cited and marked when it comes from a vendor. Re-measure before switching:
`sonu eval -m <model>` (planning) and `sonu voice check` (hearing).

## The short answer

- **Commands don't need a bigger model.** SONU's own checks (grounding, plan repair, typed tools) make the
  small model reliable: `qwen3.5:4b` passes 169 / 169 of the eval suite at 1.4 s a plan. A model that doesn't
  fit the GPU, or one SONU's rules weren't tuned on, does worse (`qwen3.5:9b`: 157 / 166, 3.7 s).
- **Open-ended jobs gain the most** from a bigger or cloud model: summaries of pages and documents, research,
  prompts for Claude Code, reading the screen, long multi-step requests.
- **Hearing depends on the accent** more than on size: Whisper loses accuracy on Indian English; India-trained
  cloud recognisers (Sarvam) are built for it — vendor numbers, so test on your voice.
- **So every job is switchable** between local and a cloud provider, with fully local as the default and the
  local model as the fallback when the cloud is unreachable.

## SONU's jobs

| Job | Does a bigger / cloud model help? | Setting |
|---|---|---|
| Planning (words → tool steps) | Little for commands; more for long, unusual requests | `[models.planner]`, `[cloud] use_for` "planning" |
| Replies (the spoken sentence) | A little (wording); SONU already sets tone, length, honesty | `[cloud] use_for` "replies" |
| Reading (pages, documents, research) | **A lot** — and more text fits than the 4B's 16k context | `[cloud] use_for` "reading" |
| Prompts for Claude Code | **A lot** | `[cloud] use_for` "prompts" |
| Seeing the screen | **A lot** — small local vision models misread screens | `[cloud] use_for` "vision", `vision_provider` |
| Hearing | Depends on your accent | `[voice] stt_model` (local), `[voice] hearing` (sarvam, groq, gemini) |
| Speaking | Style only (Indian English voices) | `[voice] voice` (Kokoro), `[voice] speaking` (sarvam, gemini) |

## Measured on the laptop (planning, SONU's eval suite)

| Planner | Passed | Time a plan | Note |
|---|---|---|---|
| `qwen3.5:4b` | 169 / 169 | 1.4 s | fits the GPU; SONU's rules tuned on it |
| `qwen3.5:9b` | 157 / 166 | 3.7 s | 25 % on the CPU next to the 4B |
| 4B + 9B for hard requests | 11 / 12 hard | 15.5 s | swapping models costs time |
| `qwen3:8b` (thinking) | 113 / 123 | 10.9 s | thinking is slow |
| `gpt-oss:120b-cloud` | 115 / 120 | 1.4 s | no window titles; 3 of 5 misses were SONU's (fixed) |

Not yet measured: the owner's other installed models (gemma4:e4b, gpt-oss:20b, glm-4.7-flash,
qwen3.6:35b-a3b, …) and the direct cloud APIs — to do when the GPU is free.

## Recommendations

**Planning and replies.** Local: `qwen3.5:4b` on 8 GB; worth measuring `gemma4:e4b` / `e2b` (function calling
trained in); `qwen3.5:9b` on 12 GB+. Cloud: what matters for a voice assistant is the time to the first token —
Groq-hosted open models and Gemini Flash are the fastest (~100–450 ms), Claude Haiku is close; Claude Sonnet
or `gpt-oss:120b-cloud` for the hardest requests.

**Reading, research, prompts.** The cloud's biggest win: a big model (Claude Sonnet, Gemini Pro, gpt-oss-120b).

**Seeing.** Cloud strongly preferred (`gemma4:31b-cloud`, Gemini, Claude) — anything on screen is sent.

**Hearing.**

| Model | Where | Note |
|---|---|---|
| Whisper large-v3-turbo | local | today; 96 % of words on the owner's `sonu voice check` |
| Whisper large-v3 | local | ~1–2 points more accurate, slower |
| Parakeet TDT 0.6B v3 | local | fewer English errors than Whisper large-v3, ~13× faster; English + 25 European languages |
| Canary-Qwen 2.5B | local | top of the open English leaderboard (~5.6 % WER); heavier |
| Sarvam Saaras v3 | cloud | built for Indian-accented English and Hinglish; ~₹30 / hour |
| Azure / Google Chirp / Deepgram Nova-3 | cloud | 11.9 / 14.1 / 13.4 % WER on Indian-accent English vs Whisper large-v3 12.8 % (vendor-reported) |

**Speaking.** Kokoro (local, today); Sarvam Bulbul v3 (Indian English voices), Cartesia (fastest first audio),
ElevenLabs (most expressive).

## Free and cheap ways to try the cloud

- **OpenRouter** has free models (`…:free`) behind one key.
- **OmniRoute** is a gateway you run yourself (`omniroute` provider, `http://127.0.0.1:20128/v1`) that routes
  across many providers' free tiers. Some of its free pools may break the terms of the services behind them,
  and what you send passes through whoever runs each one — SONU supports it; the choice is yours.
- **Ollama Cloud** (`ollama signin`) works today.

## Presets

| Job | Fully local (default) | Balanced | Full cloud |
|---|---|---|---|
| Planning | 4B | 4B; cloud for hard requests | cloud |
| Replies | 4B | 4B | cloud |
| Reading, research | 4B | cloud | cloud |
| Prompts | 4B | cloud | cloud |
| Seeing | local | cloud | cloud |
| Hearing | Whisper turbo | Whisper turbo | Sarvam / Google / Deepgram |
| Speaking | Kokoro | Kokoro | Sarvam Bulbul / Cartesia |
| What leaves the laptop | nothing | your words for hard requests; text you asked to read | your voice, your words, screenshots you ask about |

## Sources

- [Best Local LLMs for 8GB VRAM: Real Hardware Benchmarks](https://localllm.in/blog/best-local-llms-8gb-vram-2025)
- [Best Local AI Models for 8GB VRAM in August 2026](https://runaihome.com/blog/best-local-ai-models-8gb-vram-august-2026/)
- [Ollama Tool-Calling Models (2026)](https://localaimaster.com/blog/best-ollama-models-tool-calling)
- [Best open source speech-to-text model in 2026](https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks)
- [Parakeet vs Whisper (2026)](https://www.yaps.ai/blog/parakeet-vs-whisper)
- [Canary-1B-v2 & Parakeet-TDT-0.6B-v3](https://arxiv.org/pdf/2509.14128)
- [Svarah: English ASR on Indian accents](https://arxiv.org/pdf/2305.15760)
- [Voice of India benchmark](https://arxiv.org/pdf/2604.19151)
- [Voice AI for India vs global platforms (vendor)](https://www.caller.digital/blog/voice-ai-india-vs-global-platforms)
- [Sarvam Saaras V3](https://www.sarvam.ai/blogs/asr) · [Sarvam pricing](https://www.sarvam.ai/api-pricing)
- [LLM API latency 2026](https://www.edenai.co/post/llm-api-latency-benchmarks-speed-comparison-across-providers)
- [Fastest LLM inference APIs in 2026](https://inworld.ai/resources/fastest-llm-inference-api)
- [Best TTS models in 2026](https://openvoxai.com/blog/best-tts-models-local-cloud-2026)
- [Sarvam Bulbul](https://docs.sarvam.ai/api-reference-docs/getting-started/models/bulbul)
- [OmniRoute review](https://pinggy.io/blog/omniroute_ai_gateway_security/)
