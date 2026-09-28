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
            "  uv tool install --reinstall --from ~/dev_works/SONU 'sonu[voice]'",
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
