from __future__ import annotations

import json

import pytest

from sonu.config.settings import AutonomyMode
from sonu.policy import Policy
from sonu.tools.desktop_tools import CloseWindow, DescribeDesktop, LaunchApp
from sonu.tools.registry import MAX_STEPS, ToolRegistry

LOW, MEDIUM, READ = LaunchApp(), CloseWindow(), DescribeDesktop()


@pytest.mark.parametrize(
    ("autonomy", "tool", "tainted", "verdict"),
    [
        ("confirm-risky", READ, True, "allow"),
        ("confirm-risky", LOW, True, "allow"),
        ("confirm-risky", MEDIUM, False, "confirm"),
        ("confirm-each", LOW, False, "confirm"),
        ("confirm-each", READ, False, "allow"),
        ("suggest", LOW, False, "deny"),
        ("suggest", READ, False, "allow"),
        ("trusted", MEDIUM, False, "allow"),
        ("trusted", MEDIUM, True, "confirm"),  # untrusted text could have shaped the plan
        ("trusted", LOW, True, "allow"),
    ],
)
def test_policy_matrix(autonomy: AutonomyMode, tool: object, tainted: bool, verdict: str) -> None:
    decision = Policy(autonomy).decide(tool, tainted=tainted)  # type: ignore[arg-type]
    assert decision.verdict == verdict


def test_plan_schema_is_flat_and_pins_tool_names() -> None:
    registry = ToolRegistry()
    schema = registry.plan_schema()
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    steps = schema["properties"]["steps"]
    assert steps["maxItems"] == MAX_STEPS
    names = [variant["properties"]["tool"]["const"] for variant in steps["items"]["anyOf"]]
    assert names == [tool.name for tool in registry.tools]
    launch = steps["items"]["anyOf"][0]["properties"]["args"]
    assert launch["additionalProperties"] is False
    assert launch["required"] == ["app"]


def test_planner_description_lists_every_tool() -> None:
    text = ToolRegistry().describe_for_planner()
    assert "- apps.launch (app, workspace, new_window):" in text
    assert "- windows.close (window, all):" in text
    assert "- desktop.describe (no arguments):" in text
