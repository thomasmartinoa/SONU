"""SONU's own music player: mpv with a JSON IPC socket (D77) — "play my music", then pause, next, "what's
playing?" through the same socket.

mpv has no media controls (MPRIS) without the ``mpv-mpris`` plugin, which isn't on the owner's laptop, so SONU
starts its mpv with ``--input-ipc-server`` in its own 0700 runtime folder and talks to it there. Measured on
the laptop (mpv with silent test files): ``time-pos`` advances while it plays, ``metadata`` has the tags,
``playlist-next`` / ``pause`` work, and ``stop`` makes it exit (its socket file stays behind: a socket nobody
answers is "not running").
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TIMEOUT_S = 2.0
MAX_LINE = 1024 * 1024


class MpvError(Exception):
    """SONU's mpv isn't running, or refused."""


def music_folder(env: dict[str, str] | None = None) -> Path:
    """SONU's runtime folder for its player (socket, playlist)."""
    environ = os.environ if env is None else env
    base = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "sonu" / "music"


@dataclass(frozen=True, slots=True)
class Playing:
    paused: bool
    title: str
    artist: str
    position: float  # seconds into the song
    index: int  # which song in the list (0 = first)
    count: int  # how many songs


class SonuPlayer:
    def __init__(self, folder: Path | None = None) -> None:
        self.folder = folder or music_folder()
        self._ids = itertools.count(1)

    @property
    def socket(self) -> Path:
        return self.folder / "mpv.sock"

    @property
    def playlist(self) -> Path:
        return self.folder / "playlist.m3u"

    def argv(self, shuffle: bool = False) -> list[str]:
        """How SONU starts its mpv: no window, no terminal, its own socket, the playlist SONU wrote."""
        return [
            "mpv",
            "--no-video",
            "--force-window=no",
            "--no-terminal",
            f"--input-ipc-server={self.socket}",
            f"--playlist={self.playlist}",
            *(["--shuffle"] if shuffle else []),
        ]

    def write_playlist(self, songs: list[Path]) -> Path:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        self.playlist.write_text("".join(f"{song}\n" for song in songs), encoding="utf-8")
        self.playlist.chmod(0o600)
        return self.playlist

    async def command(self, *args: Any) -> Any:
        """One mpv command; its ``data`` (mpv's events on the way are skipped)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket), limit=MAX_LINE), TIMEOUT_S
            )
        except (OSError, TimeoutError) as exc:
            raise MpvError("SONU's music player isn't running") from exc
        request_id = next(self._ids)
        try:
            writer.write((json.dumps({"command": list(args), "request_id": request_id}) + "\n").encode())
            await writer.drain()
            while True:
                line = await asyncio.wait_for(reader.readline(), TIMEOUT_S)
                if not line:
                    raise MpvError("the music player closed")
                reply = json.loads(line)
                if isinstance(reply, dict) and reply.get("request_id") == request_id:
                    break
        except (OSError, TimeoutError, ValueError) as exc:
            raise MpvError("the music player didn't answer") from exc
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        if reply.get("error") not in ("success", None):
            raise MpvError(f"the music player refused: {reply.get('error')}")
        return reply.get("data")

    async def running(self) -> bool:
        try:
            await self.command("get_property", "pid")
        except MpvError:
            return False
        return True

    async def state(self) -> Playing | None:
        """What it's playing, or None when it isn't running."""
        try:
            paused = await self.command("get_property", "pause")
            meta = await self.command("get_property", "metadata") or {}
            title = await self.command("get_property", "media-title") or ""
            position = await self.command("get_property", "time-pos") or 0.0
            index = await self.command("get_property", "playlist-pos") or 0
            count = await self.command("get_property", "playlist-count") or 0
        except MpvError:
            return None
        tags = {str(k).lower(): str(v) for k, v in meta.items()} if isinstance(meta, dict) else {}
        return Playing(
            bool(paused),
            tags.get("title") or str(title),
            tags.get("artist", ""),
            float(position),
            int(index),
            int(count),
        )

    async def load(self, shuffle: bool) -> None:
        """A running mpv plays the new playlist instead."""
        await self.command("loadlist", str(self.playlist), "replace")
        if shuffle:
            await self.command("playlist-shuffle")
            await self.command("playlist-play-index", 0)
        await self.command("set_property", "pause", False)
