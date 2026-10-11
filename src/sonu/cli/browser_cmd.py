"""`sonu browser`: set up SONU's browser extension (D70) and see which browsers are connected."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.markup import escape

from sonu.browser import host
from sonu.browser.install import install as do_install
from sonu.browser.install import plan
from sonu.browser.install import uninstall as do_uninstall
from sonu.cli.state import get_state
from sonu.integrations.browser import BrowserError, Browsers

app = typer.Typer(
    help="SONU's browser extension: list, switch and close tabs by voice (local only).",
    no_args_is_help=True,
)


@app.command("host", hidden=True)
def browser_host() -> None:
    """The native messaging host (the browser starts it)."""
    host.main()


@app.command("install")
def browser_install(ctx: typer.Context) -> None:
    """Write the native messaging host's manifests and get the extension ready to add."""
    state = get_state(ctx)
    setup = plan(Path.home(), state.paths.data_dir)
    if not setup.manifests:
        state.err.print("No Chromium, Zen or Firefox profile found in your home folder.")
        raise typer.Exit(code=1)
    do_install(setup)
    out = state.console
    out.print("[bold]SONU's browser helper is set up.[/] Written:")
    for path in (setup.launcher, *setup.manifests):
        out.print(f"  {escape(str(path))}")
    out.print("\n[bold]Now add the extension[/] (once per browser):")
    if any(".mozilla" not in path.parts for path in setup.manifests):
        out.print(
            "  Chromium: open chrome://extensions → turn on Developer mode → Load unpacked → "
            f"{escape(str(setup.extension))}"
        )
    if any(".mozilla" in path.parts for path in setup.manifests):
        out.print(
            "  Zen / Firefox: about:config → xpinstall.signatures.required = false (it isn't signed by "
            "Mozilla), then about:addons → ⚙ → Install Add-on From File → "
            f"{escape(str(setup.xpi))}\n"
            "    (or, until the browser restarts: about:debugging → This Zen → Load Temporary Add-on)"
        )
    out.print("\nThen `sonu browser status` shows the browsers SONU can reach.")


@app.command("status")
def browser_status(ctx: typer.Context) -> None:
    """Which browsers have the extension connected now, and how many tabs each has."""
    state = get_state(ctx)

    async def look() -> list[tuple[str, int | None]]:
        browsers = Browsers()
        rows: list[tuple[str, int | None]] = []
        for link in await browsers.links():
            try:
                rows.append((link.browser, len(await browsers.tabs([link]))))
            except BrowserError:
                rows.append((link.browser, None))
        return rows

    rows = asyncio.run(look())
    if not rows:
        state.console.print(
            "No browser is connected. `sonu browser install`, add the extension, and keep the browser open."
        )
        raise typer.Exit(code=1)
    for name, count in rows:
        tabs = "didn't answer" if count is None else f"{count} tab{'s' if count != 1 else ''}"
        state.console.print(f"{name}: connected · {tabs}")


@app.command("uninstall")
def browser_uninstall(ctx: typer.Context) -> None:
    """Remove the host's manifests and SONU's copy of the extension (remove the extension in the browser)."""
    state = get_state(ctx)
    removed = do_uninstall(plan(Path.home(), state.paths.data_dir))
    for path in removed:
        state.console.print(f"removed {escape(str(path))}")
    if not removed:
        state.console.print("Nothing to remove.")
