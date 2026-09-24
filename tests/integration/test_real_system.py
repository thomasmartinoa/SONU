"""Checks against the real desktop. Opt-in, read-only, and meant to be run on a Hyprland machine:

uv run pytest -m hyprland      # needs a running Hyprland session
uv run pytest -m ollama        # needs a running Ollama server
"""

from __future__ import annotations

import pytest

from sonu.desktop.hyprland import HyprlandClient, discover_instance
from sonu.llm import OllamaClient


@pytest.mark.hyprland
@pytest.mark.anyio
async def test_real_hyprland_state_parses() -> None:
    client = HyprlandClient(discover_instance())
    version = await client.version()
    assert version.parsed is not None
    assert await client.config_provider() in ("lua", "hyprlang")
    # Parsing every live window/workspace/monitor proves the models match this Hyprland build.
    await client.clients()
    await client.workspaces()
    assert await client.monitors()


@pytest.mark.ollama
@pytest.mark.anyio
async def test_real_ollama_inventory_parses() -> None:
    async with OllamaClient() as client:
        assert await client.version()
        for model in await client.installed_models():
            await client.model_info(model.name)
