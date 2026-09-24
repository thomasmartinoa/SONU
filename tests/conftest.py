from __future__ import annotations

import asyncio
import concurrent.futures
import shutil
import tempfile
import threading
from collections.abc import AsyncIterator, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest

from sonu.testing.fake_hyprland import FakeHyprland

REAL_SYSTEM_MARKERS = ("hyprland", "ollama")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Tests that need a real Hyprland/Ollama only run when selected, e.g. ``pytest -m hyprland``."""
    selected = config.option.markexpr or ""
    for item in items:
        for marker in REAL_SYSTEM_MARKERS:
            if item.get_closest_marker(marker) and marker not in selected:
                item.add_marker(pytest.mark.skip(reason=f"needs a real {marker}; run with -m {marker}"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short temp dir: Unix socket paths must stay under ~108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="sonu-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def xdg_env(tmp_path: Path) -> dict[str, str]:
    """Environment variables that point every SONU directory into a temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
    }


@pytest.fixture
async def fake_hypr(short_tmp: Path) -> AsyncIterator[FakeHyprland]:
    async with FakeHyprland(short_tmp, signature="fake_1") as fake:
        yield fake


class ThreadedFakeHyprland:
    """A FakeHyprland running on its own event loop in a background thread.

    Needed for CLI tests: the CLI calls ``asyncio.run`` itself, so the fake cannot share its loop.
    """

    def __init__(self, runtime_dir: Path) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.fake = FakeHyprland(runtime_dir, signature="cli")

    def __enter__(self) -> FakeHyprland:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self.fake.start(), self.loop).result(timeout=5)
        return self.fake

    def __exit__(self, *exc_info: object) -> None:
        asyncio.run_coroutine_threadsafe(self.fake.stop(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def submit(self, coroutine: Coroutine[Any, Any, None]) -> concurrent.futures.Future[None]:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)


@pytest.fixture
def threaded_hypr(short_tmp: Path) -> Iterator[tuple[ThreadedFakeHyprland, FakeHyprland]]:
    runner = ThreadedFakeHyprland(short_tmp)
    with runner as fake:
        yield runner, fake
