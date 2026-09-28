"""What SONU says back: a short, natural sentence or two after every request.

The model phrases it; SONU keeps it honest. It sees only the verified result lines (never a private
result) and must say what they say: if a step failed, wasn't confirmed or wasn't approved, the reply
has to say so; every number it uses must come from the results or the request. If the model's reply
breaks any of that, or the model is slow or down, SONU says the plain result instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from sonu.core.conversation import PRIVATE, Turn, results_of
from sonu.core.report import TaskReport
from sonu.llm.chat import ChatMessage, ChatModelError, TextModel

MAX_WORDS = 45
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)?")
# Words that admit something didn't work. A reply to a failed task must use one; a reply to a task
# that worked must not.
_NEGATIVE = re.compile(
    r"\b(not|no|couldn't|could not|didn't|did not|wasn't|isn't|can't|cannot|failed|unable|but|however|"
    r"skipped|declined|unsure|haven't|won't|nothing)\b|n't\b",
    re.IGNORECASE,
)
_BAD_STATUSES = {"failed", "partial", "unverified", "declined", "error"}
_UNITS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
)  # fmt: skip
_TENS = {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUMBER_WORDS = {word: str(value) for value, word in enumerate(_UNITS)} | {
    word: str(value) for word, value in (*_TENS.items(), ("hundred", 100))
}
# Padding small models add after every result (found on real hardware: "All checked out.", "…and
# confirmed. Everything looks good."): dropped, so the reply says the outcome and stops.
_FILLER_SENTENCE = re.compile(
    r"^(all (good|set|done|checked out)( there)?|everything (looks|is) (good|fine|great)|"
    r"all checked out)[.!]?$",
    re.IGNORECASE,
)
_FILLER_CLAUSE = re.compile(r",? and (confirmed|checked|verified|running smoothly)\b", re.IGNORECASE)


def tidy(reply: str) -> str:
    """Drop filler sentences and "… and confirmed" clauses (keeping at least one sentence)."""
    reply = _FILLER_CLAUSE.sub("", " ".join(reply.split()))
    sentences = re.split(r"(?<=[.!?])\s+", reply)
    kept = [s for s in sentences if not _FILLER_SENTENCE.match(s.strip())]
    return " ".join(kept or sentences[:1])


def _numbers(text: str) -> set[str]:
    words = {_NUMBER_WORDS[w] for w in re.findall(r"[a-z]+", text.lower()) if w in _NUMBER_WORDS}
    return set(_NUMBER.findall(text)) | words


PERSONA = """\
You are {name}, the user's assistant on their Linux desktop, speaking out loud. You are warm, calm \
and quick, like a capable friend — never robotic, never gushing. Reply in one or two short \
sentences (at most {max_words} words). No lists, no markdown, no emojis."""

RESULT_RULES = """\
Tell the user what happened, using only the RESULT lines below. ✔ means it worked; ▲ means it ran but \
couldn't be confirmed; ✖ means it failed; ⊘ means it wasn't approved. If anything is not ✔, say so \
plainly. Never add details, numbers or names that aren't in the lines. Speak like a person, not a \
log: say the outcome, not the process — don't mention checking or verifying unless something \
couldn't be confirmed. Words SONU typed into Claude Code are a task for it, not a result: never say \
what Claude did, answered or will do. {private}

For example:
RESULT: ✔ Opened kitty on workspace 2 → Kitty's up on workspace 2.
RESULT: ✔ Battery: 100% (not charging) → You're at 100 percent, and it's not charging.
RESULT: ✖ There's no folder called 'recipes' in your home → I couldn't find a recipes folder — want me \
to open another one?
RESULT: ✔ Sent it to Claude Code in shop-app → Sent — Claude Code has it.
RESULT: ▲ Opened 'Faded' on YouTube, but couldn't confirm it's playing → It's open on YouTube, but I \
couldn't tell if it's actually playing."""

# What SONU really can do — so small talk never promises more (found on real hardware: "I can … run
# commands", which SONU can't).
ABILITIES = """\
What you can do on this computer: open, move, arrange, fullscreen and close apps and windows; search \
the web and open sites; play songs on YouTube or Spotify, pause and skip; volume, brightness, Wi-Fi, \
Bluetooth and power mode; reminders and timers; screenshots, and recording with OBS; open folders \
and the newest download; the desktop's theme, wallpaper and night light; the clipboard; the laptop's \
battery and health; suspend or shut down (after asking). Coming soon: talking by voice, and working \
with Claude Code and GitHub for the user. You can't run arbitrary commands, type into apps, or \
install software yet — never claim otherwise."""


def _plain(report: TaskReport) -> str:
    """The honest fallback: the result as SONU would state it without the model."""
    if report.error:
        return f"Sorry — {report.error}"
    results = list(results_of(report))
    if report.plan is not None and report.plan.kind == "steps" and len(results) == 1:
        return results[0][2:]
    return report.headline() if report.plan is not None and report.plan.kind == "steps" else ""


def acceptable(reply: str, report: TaskReport, allowed_text: str) -> bool:
    """The model's reply keeps to the facts: length, tone matching the outcome, no made-up numbers."""
    if not reply or len(reply.split()) > MAX_WORDS or "\n" in reply.strip() or reply[0] in "{[<#*`":
        return False  # empty, too long, or not a spoken sentence (JSON, markdown)
    negative = bool(_NEGATIVE.search(reply))
    if report.status in _BAD_STATUSES and not negative:
        return False  # a failure must be admitted
    if report.status == "done" and re.search(r"\b(couldn't|failed|didn't|unable|wasn't)\b", reply, re.I):
        return False  # and success not turned into failure
    return _numbers(reply) <= _numbers(allowed_text) | {"1"}  # "one" in "one moment" is fine


class Responder:
    def __init__(self, model: TextModel, *, name: str = "Sonu", user_name: str | None = None) -> None:
        self.model, self.name, self.user_name = model, name, user_name

    def _system(self, extra: str) -> str:
        persona = PERSONA.format(name=self.name, max_words=MAX_WORDS)
        if self.user_name:
            persona += f" The user's name is {self.user_name}; use it now and then, not every time."
        return f"{persona}\n\n{extra}"

    @staticmethod
    def _history(history: Sequence[Turn]) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        for turn in history:
            messages += [ChatMessage("user", turn.request), ChatMessage("assistant", turn.reply)]
        return messages

    async def reply(self, report: TaskReport, history: Sequence[Turn] = ()) -> str:
        """The spoken-style reply to a finished request."""
        plan = report.plan
        if report.error or plan is None:
            return _plain(report)
        if plan.kind in ("ask", "cannot"):
            return plan.message  # a question or a "can't" — the planner's words are already short
        if plan.kind == "reply":
            return await self._chat(report.request, plan.message, history)
        if report.dry_run:
            return ""  # found on real hardware: a dry run's reply only repeated the "would" line
        if report.status == "needs-input":
            return _plain(report)
        results = results_of(report)
        if results and all(PRIVATE in line for line in results):
            # Found on real hardware: "what's playing?" → "I can't see the song name". The line on screen
            # is the answer; its content stays away from the model, so there's nothing to add.
            return ""
        private = (
            f'"{PRIVATE}" lines are on the user\'s screen: point to them ("that\'s on your screen") without '
            "guessing what they say, and never say you can't see them."
            if any(PRIVATE in line for line in results)
            else ""
        )
        messages = [
            ChatMessage("system", self._system(RESULT_RULES.format(private=private))),
            *self._history(history),
            ChatMessage("user", f"{report.request}\n\nRESULT:\n" + "\n".join(results)),
        ]
        try:
            reply = (await self.model.complete_text(messages, temperature=0.4, max_tokens=80)).content
        except ChatModelError:
            return _plain(report)
        reply = tidy(reply)
        return (
            reply if acceptable(reply, report, report.request + " " + " ".join(results)) else _plain(report)
        )

    async def _chat(self, request: str, planned: str, history: Sequence[Turn]) -> str:
        """Small talk and questions SONU can answer without acting ("hi Sonu, how's it going?")."""
        messages = [
            ChatMessage(
                "system",
                self._system(
                    "The user is chatting or asking something you can answer without doing anything on "
                    "the computer. Answer naturally. Don't claim to have done or checked anything."
                    f"\n\n{ABILITIES}"
                ),
            ),
            *self._history(history),
            ChatMessage("user", request),
        ]
        try:
            reply = " ".join(
                (await self.model.complete_text(messages, temperature=0.7, max_tokens=90)).content.split()
            )
        except ChatModelError:
            return planned
        return reply if reply and len(reply.split()) <= MAX_WORDS * 2 else planned
