"""Research (D84): "look up how kestrels hunt and summarise it", "find out when the next Arch release is".

The query is the user's own words (the only thing SONU sends a search engine); SONU fetches the top pages
itself (``integrations/research.py``: public addresses only, no cookies or scripts, size and time limits)
and the planner model **on this laptop** answers from their text, naming the sites it read. Web text is
other people's: data for the local model, never instructions — and the answer is a private result, never fed
back into planning.
"""

from __future__ import annotations

import asyncio
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from sonu.core.page_reader import PageReaderError
from sonu.integrations.research import ResearchError, fetch_text, search
from sonu.tools.base import Effect, Prepared, Risk, StepStatus, Tool, ToolOutcome
from sonu.tools.context import ToolContext
from sonu.tools.grounding import not_own_words
from sonu.verify.checks import Check
from sonu.world.snapshot import WorldSnapshot

PAGES = 3  # read this many pages (of the top results that can be read)
TRIED = 6


class ResearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=200, description="what to look up, in the user's own words")
    question: str | None = Field(
        default=None, max_length=300, description="the user's question, if they asked one"
    )


class Research(Tool):
    name = "web.research"
    description = (
        "Look something up on the web and answer from the top pages with the local model: 'look up how "
        "kestrels hunt', 'find out X and summarise it'. web.search only opens the results in the browser."
    )
    risk = Risk.LOW  # the user's own words go to a search engine, as with web.search
    effects = frozenset({Effect.WEB_FETCH})
    Args = ResearchArgs
    grounded = ("query",)

    async def prepare(
        self, args: BaseModel, ctx: ToolContext, snapshot: WorldSnapshot, *, choice: str | None = None
    ) -> Prepared | ToolOutcome:
        a = cast(ResearchArgs, args)
        if problem := not_own_words(ctx, a.query):
            return problem
        if ctx.web is None or ctx.pages is None:
            return ToolOutcome(
                StepStatus.FAILED, "SONU can't look things up here (no web access or local model)"
            )
        return Prepared(
            f"Search the web for “{a.query}” and read the top pages with the local model",
            (a.query, a.question or f"What does this say about {a.query}? Summarise it."),
        )

    async def execute(self, prepared: Prepared, ctx: ToolContext) -> ToolOutcome:
        query, question = cast("tuple[str, str]", prepared.payload)
        if ctx.web is None or ctx.pages is None:
            return ToolOutcome(StepStatus.FAILED, "SONU can't look things up here")
        try:
            ctx.pages.check_where()
            results = await search(query, ctx.web, TRIED)
        except (ResearchError, PageReaderError) as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't look it up: {exc}")
        if not results:
            return ToolOutcome(StepStatus.FAILED, f"The search found nothing for “{query}”")
        pages = await asyncio.gather(*(fetch_text(r.url, ctx.web) for r in results))
        read = [
            (r, text) for r, page in zip(results, pages, strict=True) if page and (text := page[1].strip())
        ]
        read = read[:PAGES]
        if not read:
            return ToolOutcome(StepStatus.FAILED, f"None of the top pages for “{query}” could be read")
        share = max(1000, ctx.pages.max_text // len(read))
        text = "\n\n".join(f"Source {n} ({r.host}):\n{body[:share]}" for n, (r, body) in enumerate(read, 1))
        sites = ", ".join(dict.fromkeys(r.host for r, _ in read))
        try:
            answer = await ctx.pages.read(text, question, what=f"the top web pages for “{query}” ({sites})")
        except PageReaderError as exc:
            return ToolOutcome(StepStatus.FAILED, f"Couldn't read them: {exc}")
        checks = (Check(f"{len(read)} page{'s' if len(read) != 1 else ''} read by the local model", True),)
        # Other people's words, through the local model: shown and said to the user, never planned from.
        return ToolOutcome(StepStatus.DONE, f"{answer} (from {sites})", checks, private=True)


RESEARCH_TOOLS: tuple[Tool, ...] = (Research(),)
