"""`sonu voice bench …`: record the owner once, then score every speech model on those recordings (phase 2,
D105). See :mod:`sonu.voice.bench` for what is kept and how it's scored."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import typer

from sonu.cli.state import get_state
from sonu.voice import bench
from sonu.voice.engines import VoiceUnavailable, voice_dir, voice_installed

app = typer.Typer(
    help="Which speech model hears you best: record once, score every model.", no_args_is_help=True
)

# Local: Whisper turbo (SONU's now), Whisper large-v3, Parakeet v3. Cloud: Sarvam Saaras v4 with SONU's key
# terms and without, Gemini listening (a chat model), Gemini 3.5 Transcribe.
MODELS = ["turbo", "large-v3", "parakeet", "sarvam", "sarvam-plain", "gemini", "gemini-transcribe"]
MODELS_HELP = f"Models to score (repeatable): {', '.join(MODELS)}, or whisper:<size>."
WHISPER = {"turbo": "large-v3-turbo", "large-v3": "large-v3", "small": "small.en"}
CLOUD_MODEL = {
    "sarvam": ("sarvam", "saaras:v4", True),
    "sarvam-plain": ("sarvam", "saaras:v4", False),
    "gemini": ("gemini", "gemini-flash-latest", True),
    "gemini-transcribe": ("gemini", "gemini-3.5-transcribe", True),
}


def _folder(ctx: typer.Context, take: str) -> Path:
    return bench.bench_dir(voice_dir(get_state(ctx).paths.data_dir), take)


@app.command("record")
def bench_record(
    ctx: typer.Context,
    take: str = typer.Option(
        "quiet", "--take", help="A name for this recording session (e.g. quiet, music)."
    ),
    redo: list[str] = typer.Option([], "--redo", help="Record these lines again (their ids)."),  # noqa: B008
    group: str | None = typer.Option(None, "--group", help=f"Only this group: {', '.join(bench.GROUPS)}."),
) -> None:
    """Read the lines aloud once (~15–20 min; stop any time — it goes on from there). Kept here only."""
    from sonu.voice.audio import Endpointing, record
    from sonu.voice.engines import silero_detector

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
    folder = _folder(ctx, take)
    done = {line.id for line in bench.recorded(folder)}
    unknown = [line_id for line_id in redo if line_id not in bench.BY_ID]
    if unknown:
        typer.echo(f"✖ No such line: {', '.join(unknown)}", err=True)
        raise typer.Exit(code=1)
    todo = [
        line
        for line in bench.LINES
        if (line.id in redo if redo else line.id not in done) and (group is None or line.group == group)
    ]
    if not todo:
        typer.echo(f"Every line is recorded ({len(done)}). Next: sonu voice bench run")
        return
    typer.echo(
        f"{len(todo)} lines to read. Say each one the way you talk to SONU; recording stops when you stop.\n"
        f"Kept on this laptop only: {folder}\n"
        "(Ctrl+C stops — what's recorded is kept, and next time it goes on.)\n"
    )
    detector = silero_detector()
    group_now = ""
    for number, line in enumerate(todo, 1):
        if line.group != group_now:
            group_now = line.group
            typer.echo(f"── {bench.GROUPS[line.group]}")
        while True:
            say = f"🎙 {line.prompt} — say it your way" if line.group == "free" else f"🎙 Say: “{line.text}”"
            typer.echo(f"{number:>3}/{len(todo)}  {say}")
            utterance = asyncio.run(record(Endpointing(start_timeout_s=8.0, max_s=25.0), detector=detector))
            if not utterance.spoke:
                answer = typer.prompt(
                    "      nothing heard — Enter to try again, s to skip", default="", show_default=False
                )
                if answer.strip().lower() == "s":
                    break
                continue
            if line.group == "free":
                typed = typer.prompt(
                    "      Type exactly what you said", default="", show_default=False
                ).strip()
                if not typed:
                    typer.echo("      (nothing typed — said again)")
                    continue
                expected = typed
            else:
                expected = line.text
            again = typer.prompt(
                "      ✔ Enter for the next, r to say it again", default="", show_default=False
            )
            if again.strip().lower() == "r":
                continue
            bench.save_recording(folder, line.id, utterance.audio())
            bench.remember_said(folder, line.id, expected)
            break
    typer.echo(f"\nRecorded {len(bench.recorded(folder))} of {len(bench.LINES)}. Next: sonu voice bench run")


def _make_ears(name: str, ctx: typer.Context) -> tuple[Any, bool]:
    """A model by its bench name → (ears, whether it's given SONU's hint)."""
    from sonu.audit.log import AuditLog
    from sonu.llm.providers import lookup_key
    from sonu.runtime import provider_for
    from sonu.voice.cloud_ears import CloudEars
    from sonu.voice.engines import ParakeetEars, WhisperEars

    state = get_state(ctx)
    ears: Any
    if name in CLOUD_MODEL:
        provider_name, model, hinted = CLOUD_MODEL[name]
        provider = provider_for(state.settings, provider_name)
        key = lookup_key(provider, os.environ)
        if not key:
            raise VoiceUnavailable(f"no {provider.label} key — add it in the Control Center")
        ears = CloudEars(provider, key, model, audit=AuditLog(state.paths.audit_file))
        return ears, hinted
    if name == "parakeet":
        ears = ParakeetEars()
    else:
        ears = WhisperEars(WHISPER.get(name, name.removeprefix("whisper:")), state.settings.voice.device)
    ears.load()
    return ears, True


def _sonud_running() -> bool:
    if shutil.which("systemctl") is None:
        return False
    done = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "sonud"], capture_output=True, check=False
    )
    return done.returncode == 0


@contextlib.contextmanager
def _gpu_freed(ctx: typer.Context, ask: bool) -> Any:
    """Pause sonud (and let go of the planner it holds in the GPU) for the run; start it again after."""
    state = get_state(ctx)
    if not _sonud_running() or not (
        ask and typer.confirm("Pause SONU during the run, so the GPU is free?", default=True)
    ):
        yield
        return
    subprocess.run(["systemctl", "--user", "stop", "sonud"], check=False)
    held = ""
    with contextlib.suppress(OSError):
        held = (state.paths.state_dir / "held-model").read_text(encoding="utf-8").strip()
    if held:
        from sonu.llm.ollama import OllamaClient, OllamaError

        async def let_go() -> None:
            async with OllamaClient(state.settings.ollama.host) as client:
                with contextlib.suppress(OllamaError):
                    await client.load(held, keep_alive="0")

        asyncio.run(let_go())
    typer.echo("   SONU is paused; it starts again when the run ends.")
    try:
        yield
    finally:
        subprocess.run(["systemctl", "--user", "start", "sonud"], check=False)


@app.command("run")
def bench_run(
    ctx: typer.Context,
    model: list[str] = typer.Option(  # noqa: B008
        MODELS,
        "--model",
        "-m",
        help=MODELS_HELP,
    ),
    take: str = typer.Option("quiet", "--take"),
    again: bool = typer.Option(
        False, "--again", help="Hear the recordings again (not what was heard before)."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Don't ask before sending to the cloud or pausing SONU."
    ),
) -> None:
    """Every model hears the recordings; then the scores (`sonu voice bench report` shows them again)."""
    from sonu.cli.voice_cmd import gpu_full, on_the_cpu
    from sonu.daemon.server import speech_hint
    from sonu.desktop.apps.catalog import AppCatalog
    from sonu.voice.cloud_ears import CloudHearingError

    folder = _folder(ctx, take)
    lines = bench.recorded(folder)
    if not lines:
        typer.echo("✖ Nothing recorded yet — first: sonu voice bench record", err=True)
        raise typer.Exit(code=1)
    cloud = [name for name in model if name in CLOUD_MODEL]
    if cloud and not yes:
        typer.echo(
            f"{len(lines)} recordings (only these) go to: {', '.join(cloud)} — audited, as agreed (D105)."
        )
        if not typer.confirm("Send them?", default=True):
            model = [name for name in model if name not in CLOUD_MODEL]
    hint = speech_hint(AppCatalog.load(os.environ))
    local = [name for name in model if name not in CLOUD_MODEL]
    with _gpu_freed(ctx, ask=bool(local) and not yes) if local else contextlib.nullcontext():
        for name in model:
            heard = {} if again else bench.load_heard(folder, name)
            todo = [line for line in lines if line.id not in heard]
            if not todo:
                typer.echo(f"✔ {name}: already heard all {len(lines)} (--again to redo)")
                continue
            typer.echo(f"… {name}: hearing {len(todo)} recordings")
            try:
                ears, hinted = _make_ears(name, ctx)
            except VoiceUnavailable as exc:
                typer.echo(f"✖ {name}: {exc}", err=True)
                continue
            except RuntimeError as exc:
                if not gpu_full(exc):
                    raise
                ears, hinted = on_the_cpu(WHISPER.get(name, name)), True
            failed, used_up = 0, ""
            for line in todo:
                pcm = bench.load_recording(folder, line.id)
                text = None
                for attempt in range(4):  # a rate limit is waited out; any other error is tried once more
                    started = time.perf_counter()
                    try:
                        text = ears.transcribe(pcm, bench.hint_for(line, hint) if hinted else "")
                        break
                    except CloudHearingError as exc:
                        if bench.used_up(str(exc)):
                            used_up = str(exc)
                            break
                        wait = bench.wait_for_limit(str(exc))
                        if wait is None and attempt > 0:
                            break
                        typer.echo(f"   {name}: {str(exc)[:160]} — trying again", err=True)
                        time.sleep(wait or 2.0)
                    except RuntimeError as exc:
                        if not gpu_full(exc) or getattr(ears, "device", "") != "cuda":
                            raise
                        del ears
                        gc.collect()
                        ears = on_the_cpu(WHISPER.get(name, name))
                if used_up:
                    typer.echo(f"✖ {name}: {used_up[:200]}", err=True)
                    typer.echo(
                        f"   Its limit is used up for now; the {len(heard)} heard are kept — run again later "
                        f"(-m {name}) to hear the other {len(lines) - len(heard)}.",
                        err=True,
                    )
                    break
                if text is None:
                    failed += 1  # not saved: the next run tries this line again
                    continue
                heard[line.id] = (text, time.perf_counter() - started)
                bench.save_heard(folder, name, heard)  # each line, so Ctrl+C keeps what was heard
            if failed:
                typer.echo(f"✖ {name}: {failed} recordings not heard — run again to try just those", err=True)
            del ears
            gc.collect()  # one model on the GPU at a time
    _report(folder, model, misses=False)


@app.command("report")
def bench_report(
    ctx: typer.Context,
    take: str = typer.Option("quiet", "--take"),
    misses: bool = typer.Option(False, "--misses", help="Every key word each model got wrong."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """The scores of every model that heard the recordings."""
    folder = _folder(ctx, take)
    names = bench.models_heard(folder)
    if not names:
        typer.echo("✖ No model has heard the recordings yet — sonu voice bench run", err=True)
        raise typer.Exit(code=1)
    if as_json:
        import json

        typer.echo(json.dumps(bench.as_json(_scores(folder, names)), indent=1))
        return
    _report(folder, names, misses=misses)


def _scores(folder: Path, names: list[str]) -> list[bench.Score]:
    lines, expected = bench.recorded(folder), bench.said(folder)
    durations = {line.id: len(bench.load_recording(folder, line.id)) / 2 / bench.RATE for line in lines}
    return [bench.score(name, lines, expected, bench.load_heard(folder, name), durations) for name in names]


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _report(folder: Path, names: list[str], *, misses: bool) -> None:
    from rich.console import Console
    from rich.table import Table

    scores = [s for s in _scores(folder, names) if s.all.lines]
    scores.sort(key=lambda s: -(s.all.key_score or 0))
    total = len(bench.recorded(folder))
    table = Table(title=f"How well each model hears you ({total} recordings)")
    table.add_column("model", no_wrap=True)
    table.add_column("heard", justify="right")
    table.add_column("KEY WORDS", justify="right", style="bold")
    for group in ("names", "numbers", "answers", "talk", "wake"):
        table.add_column(group, justify="right")
    table.add_column("WER", justify="right")
    table.add_column("s", justify="right")
    table.add_column("per 1000", justify="right", no_wrap=True)
    for s in scores:
        mean_audio = s.audio_seconds / s.all.lines if s.all.lines else 0.0
        table.add_row(
            s.model,
            f"{s.all.lines}/{total}",
            _pct(s.all.key_score),
            *(
                _pct(s.groups[g].key_score) if g in s.groups else "–"
                for g in ("names", "numbers", "answers", "phrasing", "wake")
            ),
            _pct(s.all.wer),
            f"{s.all.mean_seconds:.2f}",
            bench.cost_per_1000(s.model, mean_audio),
        )
    console = Console(width=max(Console().width, 130))  # the table needs the room; kitty has it
    console.print(table)
    console.print(
        "KEY WORDS: the app / number / package / file / song in each line, exact — the main score. "
        "talk: your way of talking · WER: word errors · s: seconds per request · per 1000: requests' cost"
    )
    if misses:
        for s in scores:
            console.print(f"\n[bold]{s.model}[/bold] — {len(s.misses)} lines with a key word wrong")
            for line_id, heard, missed in s.misses:
                line = bench.BY_ID[line_id]
                console.print(
                    f"  “{line.text or line_id}” → “{heard}”  (missed: {', '.join(missed)})", markup=False
                )
