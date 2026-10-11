"""A fake mpv for tests and evals (D77): started by the fake desktop's exec like the real one, it answers
SONU's JSON IPC socket like mpv does — songs from the playlist SONU wrote, a position that moves while it
plays — and, like mpv, leaves its socket file behind when it stops."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FakeMpv:
    folder: Path  # SonuPlayer.folder: where its socket is
    songs: list[Path] = field(default_factory=list)
    index: int = 0
    paused: bool = False
    starts: list[list[str]] = field(default_factory=list)  # the argv it was started with
    _offset: float = 0.0
    _since: float = 0.0
    _server: asyncio.Server | None = None

    @property
    def socket(self) -> Path:
        return self.folder / "mpv.sock"

    @property
    def running(self) -> bool:
        return self._server is not None

    def launched(self, argv: list[str]) -> None:
        """The fake desktop's exec hook: SONU started ``mpv …``."""
        if Path(argv[0]).name != "mpv":
            return
        self.starts.append(argv)
        playlist = next((a.split("=", 1)[1] for a in argv if a.startswith("--playlist=")), None)
        asyncio.get_running_loop().create_task(
            self.start(Path(playlist) if playlist else None, "--shuffle" in argv)
        )

    async def start(self, playlist: Path | None, shuffle: bool = False) -> None:
        await asyncio.sleep(0.05)  # a program takes a moment to start
        self._load(playlist, shuffle)
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.socket.unlink(missing_ok=True)
        # Like mpv, leave the socket file when it stops (Python 3.13+ removes it unless told not to).
        keep: dict[str, Any] = {"cleanup_socket": False} if sys.version_info >= (3, 13) else {}
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.socket), **keep)

    async def aclose(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

    def _load(self, playlist: Path | None, shuffle: bool) -> None:
        lines = playlist.read_text().splitlines() if playlist else []
        self.songs = [Path(line) for line in lines if line.strip()]
        if shuffle:
            self.songs.reverse()  # a shuffle tests can predict
        self.index, self.paused = 0, False
        self._restart()

    def _restart(self) -> None:
        self._offset, self._since = 0.0, asyncio.get_running_loop().time()

    def _position(self) -> float:
        if self.paused:
            return self._offset
        return self._offset + asyncio.get_running_loop().time() - self._since

    def _get(self, name: str) -> Any:
        song = self.songs[self.index] if self.songs else None
        values = {
            "pid": 4242,
            "pause": self.paused,
            "path": str(song) if song else None,
            "media-title": song.stem if song else None,
            "metadata": {"title": song.stem, "artist": song.parent.parent.name} if song else {},
            "time-pos": self._position(),
            "playlist-pos": self.index,
            "playlist-count": len(self.songs),
        }
        if name not in values:
            raise KeyError(name)
        return values[name]

    async def _answer(self, command: list[Any]) -> Any:
        name, args = command[0], command[1:]
        if name == "get_property":
            return self._get(str(args[0]))
        if name == "set_property" and args[0] == "pause":
            if bool(args[1]) != self.paused:
                self._offset, self.paused = self._position(), bool(args[1])
                self._since = asyncio.get_running_loop().time()
            return None
        if name in ("playlist-next", "playlist-prev"):
            step = 1 if name == "playlist-next" else -1
            if not 0 <= self.index + step < len(self.songs):
                raise ValueError("error running command")
            self.index += step
            self._restart()
            return None
        if name == "loadlist":
            self._load(Path(str(args[0])), False)
            return None
        if name == "playlist-shuffle":
            self.songs.reverse()
            return None
        if name == "playlist-play-index":
            self.index = int(args[0])
            self._restart()
            return None
        if name in ("stop", "quit"):
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.aclose()))
            return None
        raise ValueError("invalid parameter")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                try:
                    reply = {"data": await self._answer(request["command"]), "error": "success"}
                except KeyError:
                    reply = {"error": "property unavailable"}
                except ValueError as exc:
                    reply = {"error": str(exc)}
                reply["request_id"] = request.get("request_id", 0)
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
        except (ConnectionError, ValueError):
            pass
        finally:
            writer.close()
