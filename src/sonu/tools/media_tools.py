"""Playing a particular song, artist or video — from YouTube, in the user's browser.

1. The user's words (checked like any web search: nothing the model or the screen made up) go to
   YouTube through ``yt-dlp``, which only looks up the top result. Nothing is downloaded.
2. That video's page opens in the browser.
3. SONU finds the browser in the system media controls (MPRIS, the interface media keys use),
   presses play if the browser held the video back (autoplay rules), and checks it's playing.

The lookup happens in ``execute``, so a dry run sends nothing to YouTube.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.desktop.apps.entry import AppEntry
from sonu.system.commands import CommandError, ProgramMissing
from sonu.system.control import Video
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import ungrounded_words
from sonu.tools.web_tools import Target, WebPlan, compact, open_in_browser, prepare_browser
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

PLAYER_APPEAR_S = 12.0  # the page has to load before the browser registers its media session


async def _settle[T](read: Callable[[], Awaitable[T]], ok: Callable[[T], bool], timeout_s: float) -> T:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        value = await read()
        if ok(value) or loop.time() >= deadline:
            return value
        await asyncio.sleep(0.3)


def same_title(player_title: str, video_title: str) -> bool:
    """The browser's media title is the video's title (sometimes shortened or prefixed)."""
    a, b = compact(player_title), compact(video_title)
    if a and b:
        head = b[:16]
        return head in a or a[:16] in b
    return bool(player_title) and player_title.casefold().strip() == video_title.casefold().strip()


class PlayArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        min_length=1, max_length=120, description="what to play, in the user's words: a song, artist or video"
    )
    browser: str | None = Field(
        default=None, max_length=80, description="only if the user named a browser, e.g. 'chrome'"
    )


@dataclass(frozen=True, slots=True)
class _PlayPlan:
    entry: AppEntry
    argv: tuple[str, ...]  # the browser's argv; the video's URL is added once it's known
    query: str


class PlayMedia(Tool):
    name = "media.play"
    description = "Play a particular song, artist or video: finds it on YouTube and plays it in the browser."
    risk = Risk.LOW
    effects = frozenset({Effect.WEB_OPEN, Effect.MEDIA_CONTROL, Effect.APP_LAUNCH})
    Args = PlayArgs

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(PlayArgs, args)
        if not ctx.system.available("yt-dlp"):
            return ToolOutcome(
                StepStatus.FAILED, f"{ProgramMissing('yt-dlp')} — SONU uses it to find the video to play"
            )
        query = " ".join(a.query.split())
        missing = ungrounded_words(query, ctx.request_text)
        if missing:
            return ToolOutcome(
                StepStatus.FAILED,
                f"Won't look up '{query}': {', '.join(missing)} isn't in your request "
                "(SONU only sends your own words to websites)",
            )
        browser = prepare_browser(Target("", "", ""), a.browser, ctx, choice)
        if isinstance(browser, ToolOutcome):
            return browser
        argv = browser.argv[:-1]  # prepare_browser appended the (still empty) URL
        return Prepared(
            f"Play '{query}' from YouTube in {browser.entry.name}", _PlayPlan(browser.entry, argv, query)
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        plan = cast(_PlayPlan, prepared.payload)
        try:
            video = await ctx.system.find_video(plan.query)
        except CommandError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't look it up on YouTube: {exc}")
        if video is None:
            return ToolOutcome(StepStatus.FAILED, f"YouTube found nothing for '{plan.query}'")
        where = f"'{video.title}' on YouTube in {plan.entry.name}"
        opened = await open_in_browser(
            WebPlan(
                plan.entry,
                (*plan.argv, video.url),
                Target(video.url, f"'{video.title}' on YouTube", "youtube"),
            ),
            ctx,
        )
        try:
            return await self._make_it_play(video, where, opened, ctx)
        except CommandError as exc:
            return ToolOutcome(
                StepStatus.UNVERIFIED, f"Opened {where}, but couldn't check it's playing ({exc})"
            )

    @staticmethod
    async def _make_it_play(video: Video, where: str, opened: ToolOutcome, ctx: ToolContext) -> ToolOutcome:
        if not ctx.system.available("playerctl"):
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Opened {where}, but can't check it's playing ({ProgramMissing('playerctl')})",
            )

        async def find() -> tuple[str, str | None] | None:
            for name in await ctx.system.players():
                status, title = await ctx.system.player_state(name)
                if same_title(title, video.title):
                    return name, status
            return None

        found = await _settle(
            find, lambda f: f is not None, min(PLAYER_APPEAR_S, ctx.settings.web.load_timeout_s)
        )
        if found is None:
            if opened.status is StepStatus.FAILED:
                return opened
            return ToolOutcome(
                StepStatus.UNVERIFIED,
                f"Opened {where}, but couldn't confirm it's playing "
                "(the browser didn't show up in the system's media controls)",
            )
        name, status = found
        pressed = status != "Playing"
        if pressed:  # the browser held it back (autoplay rules): press play, like the media key would
            await ctx.system.play_player(name)
            status = await _settle(
                lambda: _status(ctx, name), lambda s: s == "Playing", ctx.settings.verify.timeout_s
            )
        check = Check("the video is playing", status == "Playing", {"player": name, "status": status})
        if status != "Playing":
            return ToolOutcome(
                StepStatus.FAILED,
                f"Opened {where}, but it isn't playing (the player says: {status})",
                (check,),
            )
        return ToolOutcome(StepStatus.DONE, f"Playing {where}", (check,))


async def _status(ctx: ToolContext, name: str) -> str | None:
    status, _ = await ctx.system.player_state(name)
    return status


MEDIA_TOOLS: tuple[Tool, ...] = (PlayMedia(),)
