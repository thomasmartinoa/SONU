from __future__ import annotations

from collections import Counter
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from sonu.doctor.model import STATUS_ORDER, CheckResult

_ICONS = {
    "ok": "[green]✔[/]",
    "info": "[blue]ℹ[/]",
    "warn": "[yellow]▲[/]",
    "fail": "[red]✖[/]",
    "skip": "[dim]–[/]",
}


def summarize(results: list[CheckResult]) -> dict[str, int]:
    counts = Counter(result.status for result in results)
    return {status: counts.get(status, 0) for status in STATUS_ORDER}


def render_console(console: Console, results: list[CheckResult], *, sonu_version: str) -> None:
    table = Table(
        title=f"SONU doctor · v{sonu_version}", title_justify="left", show_lines=False, expand=False
    )
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Area", style="bold", no_wrap=True)
    table.add_column("Check", no_wrap=True)
    table.add_column("Result", overflow="fold")

    previous_group = None
    for result in results:
        group = result.group if result.group != previous_group else ""
        previous_group = result.group
        text = escape(result.summary)
        if result.hint:
            text += f"\n[dim]→ {escape(result.hint)}[/]"
        table.add_row(_ICONS[result.status], group, escape(result.title), text)
    console.print(table)

    counts = summarize(results)
    parts = [f"{counts[status]} {status}" for status in STATUS_ORDER if counts[status]]
    colour = "red" if counts["fail"] else "yellow" if counts["warn"] else "green"
    console.print(f"[{colour}]{' · '.join(parts)}[/]")


def as_json(results: list[CheckResult], *, sonu_version: str) -> dict[str, Any]:
    return {
        "sonu_version": sonu_version,
        "summary": summarize(results),
        "checks": [result.as_dict() for result in results],
    }
