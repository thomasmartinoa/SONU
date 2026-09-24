"""``sonu ask`` (and bare ``sonu <request>``), plus ``sonu prompt`` for keybinds and menus."""

from __future__ import annotations

import asyncio
import json
import sys
from enum import StrEnum

import typer
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm
from rich.status import Status

from sonu.cli.state import AppState, env_snapshot, get_state
from sonu.core.engine import Confirmer, ConfirmRequest, Observer
from sonu.core.planning import Plan
from sonu.core.report import ICONS, StepRecord, TaskReport
from sonu.runtime import SetupError, build_runtime
from sonu.tools.base import Risk
from sonu.ui.desktop_ui import Notifier, Picker

SUCCESS_STATUSES = {"done", "planned", "reply", "ask"}


class ConfirmMode(StrEnum):
    AUTO = "auto"
    TERMINAL = "terminal"
    GUI = "gui"
    NEVER = "never"


# ---------------------------------------------------------------------------- confirmation
def terminal_confirmer(console: Console) -> Confirmer:
    async def confirm(request: ConfirmRequest) -> bool:
        colour = "red" if request.risk in (Risk.HIGH, Risk.FORBIDDEN) else "yellow"
        console.print(
            f"[{colour}]⚠ SONU wants to:[/] {escape(request.description)}\n"
            f"  [dim]risk: {request.risk.value} — {escape(request.reason)}[/]"
        )
        return await asyncio.to_thread(Confirm.ask, "  Allow?", default=False, console=console)

    return confirm


def gui_confirmer(picker: Picker) -> Confirmer:
    async def confirm(request: ConfirmRequest) -> bool:
        return await picker.confirm(
            f"SONU wants to: {request.description} ({request.risk.value} risk). Allow?"
        )

    return confirm


async def _never(request: ConfirmRequest) -> bool:
    return False


def pick_confirmer(mode: ConfirmMode, console: Console) -> tuple[Confirmer, str]:
    if mode is ConfirmMode.AUTO:
        mode = ConfirmMode.TERMINAL if sys.stdin.isatty() else ConfirmMode.GUI
    if mode is ConfirmMode.TERMINAL:
        return terminal_confirmer(console), "terminal"
    if mode is ConfirmMode.GUI:
        picker = Picker.detect()
        if picker is not None:
            return gui_confirmer(picker), picker.program
    # Nothing can ask the user: risky steps are refused (fail closed).
    return _never, "none (risky steps will be skipped)"


# ---------------------------------------------------------------------------- live output
class TerminalObserver(Observer):
    def __init__(self, console: Console, *, enabled: bool) -> None:
        self.console = console
        self.enabled = enabled
        self.status: Status | None = None

    def start(self, model: str) -> None:
        if self.enabled:
            self.status = self.console.status(f"Planning with {model}…")
            self.status.start()

    def stop(self) -> None:
        if self.status is not None:
            self.status.stop()
            self.status = None

    def on_plan(self, plan: Plan) -> None:
        self.stop()
        if self.enabled and plan.kind == "steps":
            steps = len(plan.steps)
            plural = "s" if steps != 1 else ""
            self.console.print(
                f"[dim]plan by {escape(plan.model)} in {plan.duration_s:.1f}s · {steps} step{plural}[/]"
            )
            if plan.message:
                self.console.print(f"[bold]{escape(plan.message)}[/]")

    def on_step_start(self, index: int, description: str) -> None:
        if self.enabled:
            self.status = self.console.status(f"{escape(description)}…")
            self.status.start()

    def on_step(self, record: StepRecord) -> None:
        self.stop()
        if self.enabled:
            summary = record.outcome.summary
            if record.outcome.status.value == "skipped":
                summary = f"would: {record.description}"
            self.console.print(f"{ICONS[record.outcome.status]} {escape(summary)}")


def print_final(console: Console, report: TaskReport, *, streamed: bool) -> None:
    status = report.status
    if report.error:
        console.print(f"[red]✖ {escape(report.error)}[/]")
        return
    if not streamed or report.plan is None or report.plan.kind != "steps" or not report.steps:
        for line in report.lines():
            console.print(escape(line))
    else:
        for question in report.questions:
            console.print(f"[cyan]? {escape(question)}[/]")
    colour = {"done": "green", "planned": "blue", "partial": "yellow", "needs-input": "cyan"}.get(
        status, "red"
    )
    if report.plan is not None and report.plan.kind == "steps":
        console.print(f"[{colour}]{escape(report.headline())}[/]")


# ---------------------------------------------------------------------------- running a request
def run_request(
    state: AppState,
    text: str,
    *,
    dry_run: bool,
    deep: bool,
    model: str | None,
    as_json: bool,
    notify: bool,
    confirm_mode: ConfirmMode,
) -> None:
    state.require_valid_config()
    console = state.console
    confirmer, how = pick_confirmer(confirm_mode, state.err)
    observer = TerminalObserver(console, enabled=not as_json)

    async def go() -> TaskReport:
        runtime = await build_runtime(state.settings, state.paths, env_snapshot(), deep=deep, model=model)
        try:
            observer.start(runtime.planner_model)
            return await runtime.engine.run(text, confirm=confirmer, dry_run=dry_run, observer=observer)
        finally:
            observer.stop()
            await runtime.aclose()

    try:
        report = asyncio.run(go())
    except SetupError as exc:
        observer.stop()
        state.err.print(f"[red]✖ {escape(str(exc))}[/]")
        if notify:
            asyncio.run(Notifier().send("SONU can't start", str(exc).split("\n")[0], urgent=True))
        raise typer.Exit(code=1) from exc

    if as_json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print_final(console, report, streamed=True)
        if state.verbosity:
            state.err.print(f"[dim]confirmations via: {how}[/]")
    if notify:
        body = "\n".join(report.lines())
        asyncio.run(Notifier().send(report.headline(), body, urgent=report.status in ("failed", "error")))
    if report.status not in SUCCESS_STATUSES:
        raise typer.Exit(code=1)


_REQUEST = typer.Argument(..., help="What you want, in plain words.")
_DRY_RUN = typer.Option(False, "--dry-run", help="Show what SONU would do; change nothing.")
_DEEP = typer.Option(False, "--deep", help="Use the bigger planner model (slower, smarter).")
_MODEL = typer.Option(None, "--model", help="Use this Ollama model for planning.")
_JSON = typer.Option(False, "--json", help="Print the report as JSON.")
_NOTIFY = typer.Option(False, "--notify", help="Also show the result as a desktop notification.")
_CONFIRM = typer.Option(
    ConfirmMode.AUTO,
    "--confirm",
    help="How to ask before risky steps: auto (terminal if interactive, else a rofi/fuzzel/wofi "
    "dialog), terminal, gui, or never (risky steps are skipped).",
)


def ask(
    ctx: typer.Context,
    request: list[str] = _REQUEST,
    dry_run: bool = _DRY_RUN,
    deep: bool = _DEEP,
    model: str | None = _MODEL,
    as_json: bool = _JSON,
    notify: bool = _NOTIFY,
    confirm: ConfirmMode = _CONFIRM,
) -> None:
    """Ask SONU to do something, e.g. `sonu ask open kitty on workspace 3`."""
    text = " ".join(request).strip()
    run_request(
        get_state(ctx),
        text,
        dry_run=dry_run,
        deep=deep,
        model=model,
        as_json=as_json,
        notify=notify,
        confirm_mode=confirm,
    )


def prompt(
    ctx: typer.Context,
    dry_run: bool = _DRY_RUN,
    deep: bool = _DEEP,
    model: str | None = _MODEL,
) -> None:
    """Pop up a text box (rofi/fuzzel/wofi), then run the request and show the result as a notification.

    Made for keybinds, e.g. in Hyprland: hl.bind("XF86Presentation", hl.dsp.exec_cmd("sonu prompt"))
    """
    state = get_state(ctx)
    picker = Picker.detect()
    if picker is None:
        state.err.print("[red]✖ `sonu prompt` needs rofi, fuzzel or wofi[/]")
        raise typer.Exit(code=1)
    text = asyncio.run(picker.ask_text("SONU"))
    if not text:
        raise typer.Exit(code=0)
    run_request(
        state,
        text,
        dry_run=dry_run,
        deep=deep,
        model=model,
        as_json=False,
        notify=True,
        confirm_mode=ConfirmMode.GUI,
    )
