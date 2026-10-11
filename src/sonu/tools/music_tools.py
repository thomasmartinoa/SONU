"""Local music (D77): "play my music", "shuffle my music", "play kestrel band from my music", "play the songs
in my downloads".

SONU finds the songs itself — the music folder (``XDG_MUSIC_DIR``) or a folder the user named, the songs whose
path has the user's words (artist and album folders count) — and plays them with its own mpv
(``integrations/mpv.py``), in folder order unless the user said shuffle. Done when mpv says a song is playing
and its position moves. Pausing, skipping and "what's playing?" reach the same mpv (``media.control``,
``system.status``) when it's the player in use.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.hyprland.errors import HyprlandError
from sonu.integrations.mpv import MpvError, Playing, SonuPlayer
from sonu.tools.base import Choice, Choices, Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.file_tools import _KINDS, file_words, files_named, find_folders, home_relative, user_dirs
from sonu.tools.grounding import ungrounded_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

MAX_SONGS = 5000
START_S = 8.0  # mpv takes a moment to start and open the first song
_SONGS = _KINDS["music"]
_SHUFFLE = re.compile(r"\b(?:shuffle|shuffled|random|randomly|mix)\b", re.I)
# Words that ask for music, not name a song ("play some music from my downloads").
_NOT_A_NAME = {
    "some",
    "music",
    "songs",
    "song",
    "all",
    "everything",
    "play",
    "my",
    "the",
    "shuffle",
    "random",
}


def songs_in(folder: Path, words: list[str]) -> list[Path]:
    """The songs under ``folder`` whose path has every word, in folder order (an album's track order)."""
    found = files_named(folder, words, _SONGS, limit=MAX_SONGS, nearest=False, in_name=False)
    return sorted(found, key=lambda p: [part.casefold() for part in p.relative_to(folder).parts])


async def player_in_use(ctx: ToolContext) -> Playing | None:
    """SONU's mpv when it's the player the user means: it's playing, or it's paused and nothing else plays."""
    if ctx.music is None:
        return None
    state = await ctx.music.state()
    if state is None or not state.paused:
        return state
    if ctx.system.available("playerctl") and await ctx.system.player_status() == "Playing":
        return None  # paused SONU music, a video playing in the browser: the user means the video
    return state


def describe(state: Playing) -> str:
    song = " — ".join(part for part in (state.title, state.artist) if part)
    where = f" (song {state.index + 1} of {state.count})" if state.count > 1 else ""
    return f"{'paused' if state.paused else 'playing'} — {song}{where}" if song else "playing"


class PlayLocalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: str | None = Field(
        default=None, max_length=120, description="the user's words for a song, artist or album; none: all"
    )
    place: str | None = Field(
        default=None, max_length=160, description="a folder the user named; none: ~/Music"
    )


class PlayLocal(Tool):
    name = "media.play_local"
    description = (
        "Play music files from the user's own folders with mpv: 'play my music', 'shuffle my music', "
        "'play <artist / album / song> from my music', 'play the songs in my downloads'."
    )
    risk = Risk.LOW
    effects = frozenset({Effect.MEDIA_CONTROL, Effect.APP_LAUNCH})
    Args = PlayLocalArgs
    grounded = ("words", "place")

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(PlayLocalArgs, args)
        for value in (a.words, a.place):
            if value and (missing := ungrounded_words(value, ctx.user_words or ctx.request_text)):
                return ToolOutcome(
                    StepStatus.FAILED, f"That must be your own words ({', '.join(missing)} isn't)"
                )
        if ctx.music is None:
            return ToolOutcome(StepStatus.FAILED, "SONU's music player isn't available here")
        if not ctx.system.available("mpv"):
            return ToolOutcome(StepStatus.FAILED, "mpv isn't installed (sudo pacman -S mpv)")
        home = ctx.system.home
        if choice:
            folder = Path(choice)
        elif a.place:
            folders = await asyncio.to_thread(find_folders, a.place, home, ctx.system.screenshot_dir())
            if len(folders) > 1:
                shown = folders[:6]
                return ToolOutcome(
                    StepStatus.NEEDS_INPUT,
                    f"several folders are called '{a.place}'",
                    question=f"Which '{a.place}' folder?",
                    choices=Choices(
                        f"Which '{a.place}' folder?",
                        tuple(Choice(home_relative(f, home), str(f)) for f in shown),
                    ),
                )
            if not folders:
                return ToolOutcome(StepStatus.FAILED, f"There's no folder called '{a.place}' in your home")
            folder = folders[0]
        else:
            folder = user_dirs(home)["XDG_MUSIC_DIR"]
        where = home_relative(folder, home)
        names, _ = file_words(a.words or "")
        names = [w for w in names if w not in _NOT_A_NAME]
        songs = await asyncio.to_thread(songs_in, folder, names) if folder.is_dir() else []
        if not songs:
            matching = f" matching “{a.words}”" if names else ""
            return ToolOutcome(StepStatus.FAILED, f"No songs{matching} in {where}")
        shuffle = bool(_SHUFFLE.search(ctx.request_text))
        count = f"{len(songs)} songs" if len(songs) != 1 else "1 song"
        matching = f" matching “{a.words}”" if names else ""
        how = ", shuffled" if shuffle else ""
        return Prepared(f"Play {count}{matching} from {where} with mpv{how}", (songs, shuffle, where))

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        songs, shuffle, where = cast("tuple[list[Path], bool, str]", prepared.payload)
        player = ctx.music
        if player is None:
            return ToolOutcome(StepStatus.FAILED, "SONU's music player isn't available here")
        await asyncio.to_thread(player.write_playlist, songs)
        try:
            if await player.running():
                await player.load(shuffle)
            else:
                await ctx.desktop.launch(player.argv(shuffle))
        except (MpvError, HyprlandError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't start the music: {exc}")
        state = await _moving(player, START_S)
        count = f"{len(songs)} songs" if len(songs) != 1 else "1 song"
        playing = Check("a song is playing", state is not None and not state.paused)
        if state is None or state.paused:
            return ToolOutcome(
                StepStatus.FAILED if state is None else StepStatus.UNVERIFIED,
                f"mpv didn't start playing {count} from {where}"
                if state is None
                else f"Sent {count} to mpv, but it's paused",
                (playing,),
            )
        # Song names are the user's: said privately ("playing — Song 1 — Kestrel Band").
        return ToolOutcome(
            StepStatus.DONE,
            f"Playing {count} from {where}{', shuffled' if shuffle else ''}: {describe(state)}",
            (playing, Check("the song is moving", True)),
            private=True,
        )


async def _moving(player: SonuPlayer, timeout_s: float) -> Playing | None:
    """The player's state once a song is playing and its position has moved (None: it never answered)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    first: Playing | None = None
    last: Playing | None = None
    while loop.time() < deadline:
        last = await player.state()
        if last is not None and not last.paused:
            if first is None:
                first = last
            elif last.position > first.position or last.index != first.index:
                return last
        await asyncio.sleep(0.25)
    return last


MUSIC_TOOLS: tuple[Tool, ...] = (PlayLocal(),)
