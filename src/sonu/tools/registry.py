"""The set of tools the planner may use, and the JSON schema that constrains its output.

The schema is handed to Ollama's structured-output ``format`` field, so the model *cannot*
produce an unknown tool name or arguments of the wrong shape — every step is one of the
``anyOf`` branches below, each pinned to a tool name with ``const``.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from sonu.tools.base import Tool
from sonu.tools.browser_tools import BROWSER_TOOLS
from sonu.tools.clipboard_tools import CLIPBOARD_TOOLS
from sonu.tools.desktop_tools import DEFAULT_TOOLS
from sonu.tools.dev_tools import DEV_TOOLS
from sonu.tools.document_tools import DOCUMENT_TOOLS
from sonu.tools.everyday_tools import EVERYDAY_TOOLS
from sonu.tools.file_change_tools import FILE_CHANGE_TOOLS
from sonu.tools.file_tools import FILE_TOOLS
from sonu.tools.github_tools import GITHUB_TOOLS
from sonu.tools.health_tools import HEALTH_TOOLS
from sonu.tools.history_tools import HISTORY_TOOLS
from sonu.tools.journal_tools import JOURNAL_TOOLS
from sonu.tools.layout_tools import LAYOUT_TOOLS
from sonu.tools.media_tools import MEDIA_TOOLS
from sonu.tools.memory_tools import MEMORY_TOOLS
from sonu.tools.message_tools import MESSAGE_TOOLS
from sonu.tools.music_tools import MUSIC_TOOLS
from sonu.tools.obs_tools import OBS_TOOLS
from sonu.tools.package_tools import PACKAGE_TOOLS
from sonu.tools.power_tools import POWER_TOOLS
from sonu.tools.project_tools import PROJECT_TOOLS
from sonu.tools.rice_tools import RICE_TOOLS
from sonu.tools.screen_tools import SCREEN_TOOLS
from sonu.tools.settings_tools import SETTINGS_TOOLS
from sonu.tools.voice_tools import VOICE_TOOLS
from sonu.tools.web_tools import WEB_TOOLS
from sonu.tools.window_tools import WINDOW_TOOLS

MAX_STEPS = 8
MAX_UNSUPPORTED = 4


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s and drop ``title`` noise; small models do better with flat schemas."""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                return walk(copy.deepcopy(defs[name]))
            return {key: walk(value) for key, value in node.items() if key not in ("$defs", "title")}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    result: dict[str, Any] = walk(schema)
    return result


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool] = (
            *DEFAULT_TOOLS,
            *WINDOW_TOOLS,
            *WEB_TOOLS,
            *BROWSER_TOOLS,
            *HISTORY_TOOLS,
            *FILE_CHANGE_TOOLS,
            *CLIPBOARD_TOOLS,
            *MUSIC_TOOLS,
            *HEALTH_TOOLS,
            *DOCUMENT_TOOLS,
            *MEDIA_TOOLS,
            *SETTINGS_TOOLS,
            *EVERYDAY_TOOLS,
            *FILE_TOOLS,
            *RICE_TOOLS,
            *POWER_TOOLS,
            *OBS_TOOLS,
            *MEMORY_TOOLS,
            *JOURNAL_TOOLS,
            *LAYOUT_TOOLS,
            *MESSAGE_TOOLS,
            *VOICE_TOOLS,
            *DEV_TOOLS,
            *PROJECT_TOOLS,
            *GITHUB_TOOLS,
            *PACKAGE_TOOLS,
            *SCREEN_TOOLS,
        ),
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def add(self, tools: Iterable[Tool]) -> list[str]:
        """Add tools (an MCP server's, D63); a name SONU already has is skipped. Returns the skipped names."""
        skipped = []
        for tool in tools:
            if tool.name in self._tools:
                skipped.append(tool.name)
            else:
                self._tools[tool.name] = tool
        return skipped

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def args_schema(self, tool: Tool) -> dict[str, Any]:
        return _inline_refs(tool.args_json_schema())

    def plan_schema(self) -> dict[str, Any]:
        step_variants = [
            {
                "type": "object",
                "properties": {"tool": {"const": tool.name}, "args": self.args_schema(tool)},
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
            for tool in self._tools.values()
        ]
        return {
            "type": "object",
            # "unsupported" comes first so the model decides what it can't do before planning steps.
            "properties": {
                "unsupported": {
                    "type": "array",
                    "maxItems": MAX_UNSUPPORTED,
                    "items": {"type": "string", "maxLength": 120},
                },
                "kind": {"type": "string", "enum": ["steps", "ask", "reply", "cannot"]},
                "message": {"type": "string"},
                "steps": {"type": "array", "maxItems": MAX_STEPS, "items": {"anyOf": step_variants}},
            },
            "required": ["unsupported", "kind", "message", "steps"],
            "additionalProperties": False,
        }

    def describe_for_planner(self) -> str:
        lines = []
        for tool in self._tools.values():
            properties = self.args_schema(tool).get("properties", {})
            fields = ", ".join(properties) or "no arguments"
            lines.append(f"- {tool.name} ({fields}): {tool.description}")
        return "\n".join(lines)
