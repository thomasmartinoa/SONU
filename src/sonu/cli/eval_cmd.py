"""``sonu eval``: score planner models on the eval suite (fake desktop, real model)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
from pathlib import Path

import typer
from rich.markup import escape
from rich.table import Table

from sonu.cli.state import get_state
from sonu.evals.runner import EvalSummary, load_cases, run_eval
from sonu.llm.chat import OllamaChatModel
from sonu.llm.ollama import OllamaClient, OllamaError

_MODELS = typer.Option(
    None, "--model", "-m", help="Model(s) to evaluate; repeat for several. Default: your planner."
)
_CASES = typer.Option(None, "--case", "-c", help="Only run these case ids (repeatable).")
_JSON = typer.Option(False, "--json", help="Print results as JSON.")
_DEEP = typer.Option(False, "--deep", help="Use the deep-planner role settings (thinking on).")


def eval_cmd(
    ctx: typer.Context,
    models: list[str] | None = _MODELS,
    cases: list[str] | None = _CASES,
    as_json: bool = _JSON,
    deep: bool = _DEEP,
) -> None:
    """Measure how well planner models drive SONU (runs on a fake desktop; your real desktop is untouched)."""
    state = get_state(ctx)
    state.require_valid_config()
    settings = state.settings
    role = settings.models.planner_deep if deep else settings.models.planner
    names = models or [role.model]
    selected = load_cases()
    if cases:
        unknown = set(cases) - {case.id for case in selected}
        if unknown:
            state.err.print(f"[red]unknown case id(s): {', '.join(sorted(unknown))}[/]")
            raise typer.Exit(code=2)
        selected = [case for case in selected if case.id in cases]

    async def go() -> list[EvalSummary]:
        summaries = []
        async with OllamaClient(settings.ollama.host, timeout_s=settings.ollama.timeout_s) as client:
            for name in names:
                try:
                    await client.model_info(name)
                except OllamaError as exc:
                    state.err.print(f"[red]✖ {escape(str(exc))}[/]")
                    continue
                model = OllamaChatModel(client, role.model_copy(update={"model": name}), settings.ollama)
                if not as_json:
                    state.err.print(f"[dim]evaluating {escape(name)} on {len(selected)} cases…[/]")
                with tempfile.TemporaryDirectory(prefix="sonu-eval-") as workdir:
                    summaries.append(await run_eval(selected, model, Path(workdir)))
        return summaries

    summaries = asyncio.run(go())
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "model": summary.model,
                        "passed": summary.passed,
                        "total": len(summary.results),
                        "score": round(summary.score, 3),
                        "mean_planning_s": round(summary.mean_planning_s, 3),
                        "results": [dataclasses.asdict(result) for result in summary.results],
                    }
                    for summary in summaries
                ],
                indent=2,
            )
        )
        return
    for summary in summaries:
        table = Table(title=f"{summary.model}", title_justify="left")
        table.add_column("case")
        table.add_column("", width=2)
        table.add_column("status")
        table.add_column("plan s", justify="right")
        table.add_column("why it failed", overflow="fold")
        for result in summary.results:
            table.add_row(
                result.case_id,
                "[green]✔[/]" if result.passed else "[red]✖[/]",
                result.status,
                f"{result.planning_s:.1f}",
                escape("; ".join(result.failures)),
            )
        state.console.print(table)
    for summary in summaries:
        state.console.print(
            f"[bold]{escape(summary.model)}[/]: {summary.passed}/{len(summary.results)} passed "
            f"({summary.score:.0%}) · mean planning time {summary.mean_planning_s:.1f}s"
        )
    if not summaries:
        raise typer.Exit(code=1)
