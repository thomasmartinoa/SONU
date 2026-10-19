"""What SONU says back: a short, natural sentence or two after every request.

The model phrases it; SONU keeps it honest. It sees only the verified result lines (never a private
result) and must say what they say: if a step failed, wasn't confirmed or wasn't approved, the reply
has to say so; every number it uses must come from the results or the request. If the model's reply
breaks any of that, or the model is slow or down, SONU says the plain result instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime

from sonu.core.conversation import PRIVATE, Turn, results_of
from sonu.core.report import TaskReport
from sonu.llm.chat import ChatMessage, ChatModelError, TextModel

MAX_WORDS = 45
CHAT_WORDS = 40  # small talk: a person's answer, not a speech (found on the laptop: 25-word offers of help)
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


# Offers of more help at the end of small talk (found on the laptop, nearly every time: "Ready to tackle
# your day?", "Need another quick win today?", "Just let me know what you want to focus on first."). A
# person doesn't say that after every sentence; the user asks when they want something.
_OFFER = re.compile(
    r"^(is there anything|anything (else|specific)|need (anything|another|a hand|something)|"
    r"just (let me know|say)|let me know|ready (to|when)|shall we|"
    r"what('s| is) next|what would you like|how can i help|what can i do|"
    r"i'?m (right )?here (to help|whenever|if|for you)|what are we (up to|doing|working)|want me to help)\b",
    re.IGNORECASE,
)
# How the user sounds — decided by SONU from their words, and given to the model as one instruction.
_ANNOYED = re.compile(
    r"\b(ugh|argh|come on|already|seriously|again\?|damn|dammit|stupid|useless|"
    r"why (isn't|won't|can't|doesn't)|"
    r"still (not|isn't|doesn't)|for god'?s sake|wtf)\b",
    re.IGNORECASE,
)
_THANKS = re.compile(r"^\W*(thanks|thank you|cheers|ta|nice one|perfect|great|awesome)\b", re.IGNORECASE)
_GREETING_WORDS = re.compile(r"^\W*(hi|hey|hello|yo|good (morning|afternoon|evening))\b", re.IGNORECASE)


def tone(request: str) -> str:
    """One instruction for how to answer, from how the user spoke (found on the laptop: "kitty 2" and "ugh
    just open kitty on 2 already" both got "Martin, Kitty is now up on workspace 2.")."""
    if _ANNOYED.search(request):
        return "They sound annoyed: be brief and plain — no cheerfulness, no exclamation marks."
    if _THANKS.match(request):
        return "They're thanking you: answer in a few words."
    if len(request.split()) <= 3 and not _GREETING_WORDS.match(request):
        return "They were brief: answer in at most six words."
    return ""


def part_of_day(now: datetime) -> str:
    return (
        "morning"
        if 5 <= now.hour < 12
        else "afternoon"
        if now.hour < 17
        else "evening"
        if now.hour < 22
        else "night"
    )


def without_offers(reply: str) -> str:
    """Small talk without the offer of more help at its end (keeping at least one sentence)."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(reply.split()))
    kept = [s for s in sentences if not _OFFER.match(s.strip())]
    return " ".join(kept or sentences[:1])


def fit(reply: str, max_words: int) -> str:
    """The first whole sentences within ``max_words`` (never cut mid-sentence; at least the first)."""
    sentences = re.split(r"(?<=[.!?])\s+", reply)
    kept: list[str] = []
    for sentence in sentences:
        if kept and len(" ".join([*kept, sentence]).split()) > max_words:
            break
        kept.append(sentence)
    return " ".join(kept)


def without_name(reply: str, name: str) -> str:
    """The reply without the user's name: "Martin, Kitty's up" → "Kitty's up"; "Thanks, Martin!" →
    "Thanks!"."""
    pattern = re.escape(name)
    reply = re.sub(rf"^\W*{pattern}\W*[,!.]\s*", "", reply, flags=re.I)
    reply = re.sub(rf"[,\s]+{pattern}(?=[.!?]|$)", "", reply, flags=re.I)
    reply = re.sub(rf"\s*\b{pattern}\b", "", reply, flags=re.I)
    return reply[:1].upper() + reply[1:] if reply else reply


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
and quick, like a capable friend — never robotic, never gushing, with a light touch of humour when \
the moment allows. Match the user: a few words for a short command, a little more for a chat. \
Reply in one or two short sentences (at most {max_words} words). No lists, no markdown, no emojis."""

RESULT_RULES = """\
Tell the user what happened, using only the RESULT lines below. ✔ means it worked; ▲ means it ran but \
couldn't be confirmed; ✖ means it failed; ⊘ means it wasn't approved. If anything is not ✔, say so \
plainly. Never add details, numbers or names that aren't in the lines. Speak like a person, not a \
log: say the outcome, not the process — don't mention checking or verifying unless something \
couldn't be confirmed. "↻ Tried another way" means the step before it failed and SONU then did what \
follows instead: say briefly what didn't work and what you did. Words SONU typed into Claude Code are a \
task for it, not a result: never say what Claude did, answered or will do. {private}

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
What you can do on this computer — apps and windows: open, move, arrange side by side, fullscreen and \
close them, switch workspaces, save and restore window layouts. Web: search and open sites, look things \
up on the web and summarise the top pages, play songs and videos on YouTube or Spotify, pause and skip; \
in the user's browser (with SONU's extension) list, switch and close tabs, summarise or answer questions \
about a page, click links and buttons, type into fields and scroll; search the browser history and \
bookmarks. Files: open folders and files by name, find files, recent files, summarise or answer \
questions about the user's documents (text, PDF, Word), move, rename, delete to the trash and restore, \
make folders; copy things to the clipboard and read or clear it. Music: play the user's own music folder \
(shuffle, pause, skip). Settings: volume, brightness, Wi-Fi, Bluetooth, power mode, night light, \
do-not-disturb, the theme and wallpaper; the laptop's battery and health, which programs use the CPU, \
memory or GPU and why the fan is loud, whether anything crashed or failed, and stopping a program (after \
asking). Screenshots; recording and streaming with OBS. Reminders and timers. A briefing of the day \
("good morning", "brief me"). Opening your own Control Center (SONU's settings; the user changes them \
there). When the user turns it on in the config, a heads-up by yourself about a low battery, a nearly \
full disk or a CPU that stays very hot (never during a call). Read the screen, press buttons in apps, \
and type the user's words into apps (after asking). Unread message counts, recent notifications (if \
turned on), what the user was doing earlier (the activity journal), what you and the user talked about \
earlier (days back, too), and remembering things they tell you. Developer work: open projects in VS \
Code, run a project's tests or build (after asking), git changes and commits, a terminal in a folder, \
Claude Code (open it, type into it, a prompt you wrote, switch its model), GitHub pull requests (list, \
merge after asking). Install and remove software (after asking — the system asks for the password), \
and say whether something is installed. \
Suspend or shut down (after asking). Talk by voice ("Hey Sonu"). You can't run arbitrary commands, send \
messages for the user, delete anything for good, join new Wi-Fi networks or pair new devices — never \
claim otherwise."""


def _plain(report: TaskReport) -> str:
    """The honest fallback: the result as SONU would state it without the model."""
    if report.error:
        return f"Sorry — {report.error}"
    results = list(results_of(report))
    if report.plan is not None and report.plan.kind == "steps" and len(results) == 1:
        return results[0][2:]
    return report.headline() if report.plan is not None and report.plan.kind == "steps" else ""


# Actions that matter too much to claim unless SONU's own results say so. Found on real hardware: "merge
# the pull request in SONU" was planned as only listing it, and the reply said "I merged PR #31".
_CLAIMS = re.compile(
    r"\b(merged|deleted|removed|installed|uninstalled|sent|closed|pressed|clicked|typed|shut (?:it )?down|"
    r"restarted|rebooted|"
    r"locked|suspended|logged out|bought|paid|published|pushed|went live)\b",
    re.I,
)
_NEGATED = re.compile(r"(?:\bnot|n't|\bnever|\bno)\W+(?:\w+\W+){0,2}$", re.I)


# After a failure, a reply that promises to act ("I'll get it onto your screen") promises what SONU
# won't do on its own — it only acts on the user's next request.
_PROMISE = re.compile(r"\b(I'll|I will|let me|I'm going to|I am going to)\b(?! need)", re.I)


def claims_unbacked(reply: str, results: str) -> bool:
    """The reply says SONU did something (merged, deleted, sent, …) that no result line shows."""
    for match in _CLAIMS.finditer(reply):
        if _NEGATED.search(reply[: match.start()]):
            continue  # "I haven't merged it"
        if match.group(1).lower()[:5] not in results.lower():
            return True
    return False


def acceptable(reply: str, report: TaskReport, allowed_text: str) -> bool:
    """The model's reply keeps to the facts: length, tone matching the outcome, no made-up numbers."""
    if not reply or len(reply.split()) > MAX_WORDS or "\n" in reply.strip() or reply[0] in "{[<#*`":
        return False  # empty, too long, or not a spoken sentence (JSON, markdown)
    negative = bool(_NEGATIVE.search(reply))
    if report.status in _BAD_STATUSES and not negative:
        return False  # a failure must be admitted
    recovered = report.recovered_from is not None  # D92: "zen wasn't open, so I opened it on 4" is honest
    if (
        report.status == "done"
        and not recovered
        and re.search(r"\b(couldn't|failed|didn't|unable|wasn't)\b", reply, re.I)
    ):
        return False  # and success not turned into failure
    if claims_unbacked(reply, "\n".join(results_of(report))):
        return False
    if report.status in _BAD_STATUSES and _PROMISE.search(reply):
        return False  # found on the laptop: "Zen isn't showing yet; I'll get it onto your screen" — it didn't
    return _numbers(reply) <= _numbers(allowed_text) | {"1"}  # "one" in "one moment" is fine


class Responder:
    def __init__(
        self,
        model: TextModel,
        *,
        name: str = "Sonu",
        user_name: str | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.model, self.name, self.user_name, self.clock = model, name, user_name, clock

    def _system(self, extra: str, *, request: str = "", named: bool = False) -> str:
        persona = PERSONA.format(name=self.name, max_words=MAX_WORDS)
        if named and self.user_name:
            persona += f" The user's name is {self.user_name}."
        if how := tone(request):
            persona += f" {how}"
        return f"{persona}\n\n{extra}"

    def _may_name(self, history: Sequence[Turn]) -> bool:
        """SONU decides when the user's name is said: in small talk, once a conversation (found on the
        laptop: "use it now and then" put "Martin, …" in front of nearly every reply)."""
        if not self.user_name:
            return False
        return not any(self.user_name.casefold() in turn.reply.casefold() for turn in history)

    def _unnamed(self, reply: str, named: bool) -> str:
        return reply if named or not self.user_name else without_name(reply, self.user_name)

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
            ChatMessage("system", self._system(RESULT_RULES.format(private=private), request=report.request)),
            *self._history(history),
            ChatMessage("user", f"{report.request}\n\nRESULT:\n" + "\n".join(results)),
        ]
        try:
            reply = (await self.model.complete_text(messages, temperature=0.4, max_tokens=80)).content
        except ChatModelError:
            return _plain(report)
        # A result isn't small talk: no name, and no "Ready to switch over?" after it (found on the laptop).
        reply = self._unnamed(without_offers(tidy(reply)), named=False)
        return (
            reply if acceptable(reply, report, report.request + " " + " ".join(results)) else _plain(report)
        )

    async def _chat(self, request: str, planned: str, history: Sequence[Turn]) -> str:
        """Small talk and questions SONU can answer without acting ("hi Sonu, how's it going?")."""
        now, named = self.clock(), self._may_name(history)
        messages = [
            ChatMessage(
                "system",
                self._system(
                    "The user is chatting or asking something you can answer without doing anything on "
                    "the computer. Answer naturally, like a friend would — short, and without offering more "
                    "help or asking what's next (they'll ask). Don't claim to have done or checked anything. "
                    f"(It's {now:%A}, {now:%H:%M} — {part_of_day(now)}. Mention it only if their greeting "
                    f"doesn't fit the time or they ask.)\n\n{ABILITIES}",
                    request=request,
                    named=named,
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
        if not reply or reply[0] in "{[<#*`":
            return planned
        return fit(self._unnamed(without_offers(reply), named), CHAT_WORDS)
