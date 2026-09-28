"""SONU's accessibility helper — run with the *system* Python, which has the AT-SPI bindings (gi).

Standalone on purpose (no SONU imports): SONU's own environment has no PyGObject. Two commands, JSON
on stdout:

    controls <pid>                       the named, actionable controls of the app with that pid
    press <pid> <path> <name> <action>   press one (checked by name, so a moved control isn't pressed)

A control's ``path`` is its child indices from the app ("0/3/1"). Nothing else is done: no typing,
no mouse, no reading of text fields.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

MAX_CONTROLS = 400
MAX_DEPTH = 30


def _state(node: Any, atspi: Any) -> dict[str, bool]:
    states = node.get_state_set()
    names = {"showing": "SHOWING", "enabled": "ENABLED", "checked": "CHECKED", "pressed": "PRESSED",
             "selected": "SELECTED", "expanded": "EXPANDED"}  # fmt: skip
    return {key: bool(states.contains(getattr(atspi.StateType, value))) for key, value in names.items()}


def _actions(node: Any) -> list[str]:
    iface = node.get_action_iface()
    if iface is None:
        return []
    return [iface.get_action_name(i) for i in range(iface.get_n_actions())]


def _app(pid: int, atspi: Any) -> Any:
    desktop = atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        if app is not None and app.get_process_id() == pid:
            return app
    return None


def controls(pid: int, atspi: Any) -> dict[str, Any]:
    app = _app(pid, atspi)
    if app is None:
        return {"error": "not-exposed"}
    found: list[dict[str, Any]] = []

    def walk(node: Any, path: list[int]) -> None:
        if len(found) >= MAX_CONTROLS or len(path) > MAX_DEPTH:
            return
        try:
            name = (node.get_name() or "").strip()
            actions = _actions(node)
            if name and actions:
                found.append(
                    {
                        "path": "/".join(map(str, path)),
                        "name": name[:120],
                        "role": node.get_role_name(),
                        "actions": actions,
                        **_state(node, atspi),
                    }
                )
            for index in range(node.get_child_count()):
                child = node.get_child_at_index(index)
                if child is not None:
                    walk(child, [*path, index])
        except Exception:  # a window closing while it's read: skip that part
            return

    walk(app, [])
    return {"app": app.get_name(), "controls": found}


def press(pid: int, path: str, name: str, action: str, atspi: Any) -> dict[str, Any]:
    app = _app(pid, atspi)
    if app is None:
        return {"error": "not-exposed"}
    node = app
    for index in [int(p) for p in path.split("/") if p]:
        node = node.get_child_at_index(index) if node is not None else None
    if node is None or (node.get_name() or "").strip() != name:
        return {"error": "moved"}
    actions = _actions(node)
    if action not in actions:
        return {"error": "no-action"}
    before = {"name": name, **_state(node, atspi)}
    pressed = bool(node.get_action_iface().do_action(actions.index(action)))
    time.sleep(0.4)
    try:
        after = {"name": (node.get_name() or "").strip(), **_state(node, atspi)}
    except Exception:
        after = {"gone": True}
    return {"pressed": pressed, "before": before, "after": after}


def main(argv: list[str]) -> int:
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError):
        print(json.dumps({"error": "no-atspi"}))
        return 0
    if len(argv) >= 2 and argv[0] == "controls":
        result = controls(int(argv[1]), Atspi)
    elif len(argv) >= 5 and argv[0] == "press":
        result = press(int(argv[1]), argv[2], argv[3], argv[4], Atspi)
    else:
        result = {"error": "usage"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    import warnings

    warnings.simplefilter("ignore")  # Atspi's deprecation notes would mix into the output
    sys.exit(main(sys.argv[1:]))
