"""`sonu voice …`: set up and try SONU's voice (M3). Talking to it is `sonu listen` (the SONU key)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import typer

from sonu.cli.state import get_state
from sonu.voice.engines import (
    ESPEAK_DATA,
    KOKORO_FILES,
    KokoroVoice,
    VoiceUnavailable,
    download,
    missing_files,
    voice_dir,
    voice_installed,
)

app = typer.Typer(help="SONU's voice: set it up, hear it, check it.", no_args_is_help=True)


def _bench() -> typer.Typer:
    from sonu.cli.voice_bench_cmd import app as bench_app

    return bench_app


app.add_typer(_bench(), name="bench")

KEYBIND = 'hl.bind("XF86Launch1", hl.dsp.exec_cmd("sonu listen"))   -- the SONU key: talk to SONU'


@app.command("setup")
def voice_setup(ctx: typer.Context) -> None:
    """Download the voice (~350 MB, checksum-checked) and the speech-recognition model."""
    state = get_state(ctx)
    if not voice_installed():
        typer.echo(
            "✖ The voice extra isn't installed. Install SONU with it:\n"
            '  uv tool install --reinstall "sonu[voice] @ $HOME/dev_works/SONU"',
            err=True,
        )
        raise typer.Exit(code=1)
    if not ESPEAK_DATA.is_dir():
        typer.echo("✖ espeak-ng is missing (install: sudo pacman -S espeak-ng)", err=True)
        raise typer.Exit(code=1)
    folder = voice_dir(state.paths.data_dir)
    for file in missing_files(folder):
        typer.echo(f"… downloading {file.name} ({file.size // 1_000_000} MB)")
        try:
            download(file, folder)
        except (VoiceUnavailable, OSError) as exc:
            typer.echo(f"✖ {exc}", err=True)
            raise typer.Exit(code=1) from exc
    typer.echo(f"✔ Voice files in {folder} (checksums match)")
    model = state.settings.voice.stt_model
    typer.echo(f"… getting the speech-recognition model {model} (once)")
    from faster_whisper import download_model

    download_model(model)
    typer.echo(f"✔ Speech recognition: {model}")
    typer.echo(
        "\nNext: restart the daemon (`systemctl --user restart sonud`), then add this line to your Hyprland "
        f"binds (~/.config/hypr/modules/binds.lua):\n  {KEYBIND}"
    )


@app.command("test")
def voice_test(
    ctx: typer.Context,
    text: str = typer.Argument("Hi, I'm Sonu. Press the key and tell me what you need."),
    voice: str | None = typer.Option(None, "--voice", help="Try another voice, e.g. am_michael, bm_george."),
) -> None:
    """Say a sentence in SONU's voice (to hear it, or to pick a voice)."""
    from sonu.voice.audio import Speaker

    state = get_state(ctx)
    settings = state.settings.voice
    speaker = KokoroVoice(voice_dir(state.paths.data_dir), voice or settings.voice, settings.speed)
    try:
        samples = speaker.speak(text)
    except VoiceUnavailable as exc:
        typer.echo(f"✖ {exc}", err=True)
        raise typer.Exit(code=1) from exc
    asyncio.run(Speaker(speaker.rate).play(samples))


@app.command("status")
def voice_status(ctx: typer.Context) -> None:
    """Is everything for voice in place?"""
    state = get_state(ctx)
    missing = missing_files(voice_dir(state.paths.data_dir))
    rows = [
        ("voice extra", voice_installed()),
        ("espeak-ng", ESPEAK_DATA.is_dir()),
        (f"voice files ({len(KOKORO_FILES)})", not missing),
    ]
    for name, ok in rows:
        typer.echo(f"{'✔' if ok else '✖'} {name}")
    wake_on = (voice_dir(state.paths.data_dir) / "wake-word").exists()
    typer.echo(
        "· 'Hey Sonu': on — say “stop listening for Hey Sonu” to turn it off"
        if wake_on
        else "· 'Hey Sonu': off — say or type “listen for Hey Sonu” to turn it on"
    )
    if not all(ok for _, ok in rows):
        typer.echo("→ sonu voice setup")
        raise typer.Exit(code=1)


# What to say for `sonu voice train-wake` (D100): SONU's name, alone and before requests, then look-alikes and
# everyday phrases that must NOT wake it.
WAKE_SAID = [
    "Hey Sonu",
    "Hey Sonu",
    "Hey Sonu",
    "Hey Sonu",
    "Sonu",
    "Hey Sonu",
    "Hey Sonu, open kitty",
    "Hey Sonu, what's my battery?",
    "Hey Sonu, play some music",
    "Hey Sonu, open my browser",
    "Hey Sonu, what time is it?",
    "Hey Sonu, turn the volume down",
    "Hey Sonu, take a screenshot",
    "Hey Sonu, open Spotify",
    "Hey Sonu, brief me",
    "Hey Sonu, close this window",
    "Hey Sonu",
    "Hey Sonu",
]
WAKE_LOOKALIKES = [
    "Hey Sunny, how are you?",
    "I told Sonny about it",
    "Hey son, come here",
    "What song is this?",
    "Hey, so new",
    "The sun is out today",
    "Sony headphones are good",
    "Hey, soon I'll be done",
    "Open the window",
    "It's time for lunch",
    "Hey, what's up",
    "Some water, please",
]


@app.command("train-wake")
def voice_train_wake(ctx: typer.Context) -> None:
    """Teach SONU how your "Hey Sonu" sounds: say it ~18 times and a dozen look-alikes; nothing is kept but
    the words learned (D100)."""
    from sonu.voice.audio import Endpointing, record
    from sonu.voice.engines import WhisperEars, silero_detector
    from sonu.voice.wake import LEARNED_FILE, WAKE_HINT, train, use, wake_head, wakes

    state = get_state(ctx)
    if not voice_installed():
        typer.echo("✖ The voice extra isn't installed — see `sonu voice setup`", err=True)
        raise typer.Exit(code=1)
    folder = voice_dir(state.paths.data_dir)
    if (folder / "wake-word").exists():
        typer.echo(
            "✖ 'Hey Sonu' is on, so SONU would act on what you say. Say “stop listening for Hey Sonu” first "
            "(and turn it on again after).",
            err=True,
        )
        raise typer.Exit(code=1)
    ears = WhisperEars(state.settings.voice.stt_model, state.settings.voice.device)
    detector = silero_detector()
    typer.echo("Say each line the way you'd call SONU. Recording stops when you stop talking.\n")

    def hear(lines: list[str], what: str) -> list[tuple[str, str]]:
        heard: list[tuple[str, str]] = []
        for number, line in enumerate(lines, 1):
            typer.echo(f"{what} {number:>2}/{len(lines)}  🎙 Say: “{line}”")
            utterance = asyncio.run(record(Endpointing(start_timeout_s=8.0, max_s=6.0), detector=detector))
            if not utterance.spoke:
                typer.echo("          (nothing heard — skipped)")
                continue
            head = wake_head(utterance.audio(), detector)  # what the listener checks
            heard.append(
                (ears.transcribe(head, WAKE_HINT), ears.transcribe(head, ""))
            )  # and the second listen
        return heard

    said = hear(WAKE_SAID, "name")
    typer.echo("\nNow phrases that must NOT wake SONU:\n")
    lookalikes = hear(WAKE_LOOKALIKES, "other")
    if not said:
        typer.echo("✖ Nothing was heard — is the right microphone selected?", err=True)
        raise typer.Exit(code=1)

    found = train(said, lookalikes)
    use(found.names, found.greetings, found.alone, found.second_listen)
    typer.echo("")
    for sample in said:
        typer.echo(
            f"   {'✔' if wakes(sample, found.second_listen) else '✖'} {sample[0]}   (2nd: {sample[1]})"
        )
    for sample in lookalikes:
        woke = wakes(sample, found.second_listen)
        typer.echo(f"   {'✖ woke on' if woke else '✔ ignored'} {sample[0]}   (2nd: {sample[1]})")
    use(set(), set(), set())
    typer.echo(
        f"\nYour “Hey Sonu”: {found.before[0]}/{len(said)} heard before, {found.now[0]}/{len(said)} now. "
        f"Look-alikes that woke it: {found.before[1]}/{len(lookalikes)} before, "
        f"{found.now[1]}/{len(lookalikes)} now."
    )
    if found.second_listen:
        typer.echo("Listening twice: the name must still be there without the hint (it kept all of yours).")
    learned = sorted(found.names | found.alone)
    if learned:
        typer.echo(f"Learned how your “Sonu” is heard: {', '.join(learned)}")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LEARNED_FILE).write_text(json.dumps(found.saved()) + "\n")
    typer.echo("✔ Saved. `systemctl --user restart sonud` to use it (no recordings were kept).")


def gpu_full(exc: BaseException) -> bool:
    """CUDA (or cuBLAS) ran out of GPU memory."""
    text = str(exc)
    return "out of memory" in text.lower() or "ALLOC_FAILED" in text or "CUDA failed" in text


def on_the_cpu(name: str) -> Any:
    from sonu.voice.engines import WhisperEars

    typer.echo(f"   the GPU is full (SONU's own models use it) — {name} goes on with the CPU, slower")
    ears = WhisperEars(name, "cpu")
    ears.load()
    return ears


@app.command("check")
def voice_check(
    ctx: typer.Context,
    model: list[str] = typer.Option(  # noqa: B008
        ["large-v3-turbo", "large-v3"],
        "--model",
        "-m",
        help="Speech models to compare (repeatable): Whisper sizes, or sarvam / groq / gemini (their key).",
    ),
) -> None:
    """Read ten short requests aloud; SONU shows how well each speech model hears you. Nothing is kept."""
    import gc
    import os
    import time

    from sonu.audit.log import AuditLog
    from sonu.daemon.server import speech_hint
    from sonu.desktop.apps.catalog import AppCatalog
    from sonu.llm.providers import lookup_key
    from sonu.runtime import provider_for
    from sonu.voice.accuracy import SENTENCES, Score, word_errors
    from sonu.voice.audio import Endpointing, record
    from sonu.voice.cloud_ears import HEARING, CloudEars, CloudHearingError
    from sonu.voice.engines import WhisperEars, silero_detector

    state = get_state(ctx)
    if not voice_installed():
        typer.echo("✖ The voice extra isn't installed — see `sonu voice setup`", err=True)
        raise typer.Exit(code=1)
    if (voice_dir(state.paths.data_dir) / "wake-word").exists():
        typer.echo(
            "✖ 'Hey Sonu' is on, so SONU would act on what you read. Say “stop listening for Hey Sonu” "
            "first (and turn it on again after).",
            err=True,
        )
        raise typer.Exit(code=1)
    hint = speech_hint(AppCatalog.load(os.environ))
    detector = silero_detector()
    typer.echo("Read each line aloud, the way you talk to SONU. Recording stops when you stop talking.\n")
    recordings: list[tuple[str, bytes]] = []
    for number, sentence in enumerate(SENTENCES, 1):
        typer.echo(f"{number:>2}/{len(SENTENCES)}  🎙 Say: “{sentence}”")
        utterance = asyncio.run(record(Endpointing(start_timeout_s=8.0), detector=detector))
        if utterance.spoke:
            recordings.append((sentence, utterance.audio()))
        else:
            typer.echo("        (nothing heard — skipped)")
    if not recordings:
        typer.echo("✖ Nothing was heard — is the right microphone selected?", err=True)
        raise typer.Exit(code=1)

    scores: list[Score] = []
    for name in model:
        ears: Any
        if name in HEARING:  # a cloud service (D96): only these test sentences go, nothing else
            provider = provider_for(state.settings, name)
            ears = CloudEars(
                provider, lookup_key(provider, os.environ), audit=AuditLog(state.paths.audit_file)
            )
            typer.echo(f"\n… listening back with {ears.name} (the test sentences go to it)")
        else:
            typer.echo(f"\n… listening back with {name} (the first time, it downloads the model)")
            ears = WhisperEars(name, state.settings.voice.device)
            try:
                ears.load()
            except VoiceUnavailable as exc:
                typer.echo(f"✖ {exc}", err=True)
                continue
            except RuntimeError as exc:
                if not gpu_full(exc):
                    raise
                ears = on_the_cpu(name)
        score = Score(name)
        for sentence, audio in recordings:
            started = time.perf_counter()
            try:
                heard = ears.transcribe(audio, hint)
            except CloudHearingError as exc:
                typer.echo(f"✖ {exc}", err=True)
                break
            except RuntimeError as exc:
                # Found on real hardware: large-v3 after turbo crashed with CUBLAS_STATUS_ALLOC_FAILED —
                # sonud's own Whisper, Kokoro and planner share the 8 GB. Go on with this model on the CPU.
                if not gpu_full(exc) or getattr(ears, "device", "") != "cuda":
                    raise
                del ears
                gc.collect()
                ears = on_the_cpu(name)
                started = time.perf_counter()
                heard = ears.transcribe(audio, hint)
            score.add(sentence, heard, time.perf_counter() - started)
        if len(score.heard) < len(recordings):
            continue
        scores.append(score)
        del ears
        gc.collect()  # one model on the GPU at a time

    typer.echo("")
    for index, (sentence, _) in enumerate(recordings):
        typer.echo(f"“{sentence}”")
        for score in scores:
            heard = score.heard[index]
            mark = "✔" if word_errors(sentence, heard) == 0 else "✖"
            typer.echo(f"   {mark} {score.model:<16} {heard}")
    typer.echo("")
    for score in scores:
        typer.echo(
            f"{score.model:<16} {score.accuracy:.0%} of words right ({score.errors} wrong of {score.total}), "
            f"{score.mean_seconds:.2f} s per request"
        )
    best = max(scores, key=lambda s: (s.accuracy, -s.mean_seconds), default=None)
    current = state.settings.voice.stt_model
    if (
        best is not None
        and best.model != current
        and best.accuracy > next((s.accuracy for s in scores if s.model == current), 0.0)
    ):
        typer.echo(
            f"\n→ {best.model} hears you best. To use it, put this in {state.paths.config_file}:\n"
            f'    [voice]\n    stt_model = "{best.model}"\n'
            "  then: systemctl --user restart sonud"
        )
    elif best is not None:
        typer.echo(f"\n→ {current} (in use now) hears you as well as any of these.")
    typer.echo("The recordings were only in memory and are gone now.")
