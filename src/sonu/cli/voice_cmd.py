"""`sonu voice …`: set up and try SONU's voice (M3). Talking to it is `sonu listen` (the SONU key)."""

from __future__ import annotations

import asyncio

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
    from typing import Any

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
        score = Score(name)
        for sentence, audio in recordings:
            started = time.perf_counter()
            try:
                heard = ears.transcribe(audio, hint)
            except CloudHearingError as exc:
                typer.echo(f"✖ {exc}", err=True)
                break
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
