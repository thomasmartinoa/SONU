"""The `sonu` command's entry point: hand a request to the daemon if it's running (a few hundredths of a
second), otherwise load the full CLI (about half a second of imports) and run it here."""

from __future__ import annotations

import sys


def main() -> None:
    from sonu.daemon.client import try_run  # standard library only

    try:
        code = try_run(sys.argv[1:])
    except KeyboardInterrupt:  # the daemon cancels the request when the client goes away
        print("\n✖ Stopped (Ctrl+C). A step already sent to an app may still finish.", file=sys.stderr)
        raise SystemExit(130) from None
    if code is not None:
        raise SystemExit(code)
    from sonu.cli.app import main as full_cli

    full_cli()
