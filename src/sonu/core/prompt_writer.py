"""The prompt writer (M4 part 2, D52): turn the user's spoken idea into a clear prompt for Claude Code.

The local model restates and structures the idea — goal, what the user asked for, a way to check the
result — and never adds facts the user didn't give (names, prices, colours, technologies). What it
writes leaves the laptop only after the user has seen all of it and said yes (``dev.prompt``).
"""

from __future__ import annotations

import re

from sonu.llm.chat import ChatMessage, ChatModelError, TextModel

MAX_CHARS = 2500

SYSTEM = """\
You write prompts for Claude Code, a coding agent working in the user's project folder. Turn the \
user's idea into one clear prompt that Claude Code can act on — the user's idea, better organised, \
not a bigger one.

Write it as the user talking to Claude Code ("Build …", "I want …"). Plain text: short lines, "-" \
for lists, no markdown, no numbering of the sections. Exactly these parts:
- One or two sentences: what to build or change.
- "Requirements:" — only the things the user said, each as a list item, in their words. Add nothing \
else: no extra features, prices, checkout, backups, formats, designs, frameworks or libraries.
- "Before you start:" — look at what's already in the folder, and ask me about anything important I \
didn't say (list those questions if you can see them).
- "Done when:" — it works for what I asked, and you tell me how to run or see it.

Keep it under 150 words. Reply with the prompt only."""


class PromptWriterError(Exception):
    """No usable prompt came back."""


def tidy(text: str) -> str:
    text = re.sub(r"^```[a-z]*\n|\n```$", "", text.strip())
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)  # markdown headings → plain lines
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


class PromptWriter:
    def __init__(self, model: TextModel) -> None:
        self.model = model

    async def write(self, idea: str, folder: str) -> str:
        messages = [
            ChatMessage("system", SYSTEM),
            ChatMessage("user", f"Project folder: {folder}\nMy idea: {idea}"),
        ]
        try:
            reply = await self.model.complete_text(messages, temperature=0.3, max_tokens=400)
        except ChatModelError as exc:
            raise PromptWriterError(f"the model couldn't write it: {exc}") from exc
        prompt = tidy(reply.content)
        if len(prompt) < 20 or prompt.lstrip().startswith(("{", "[")):
            raise PromptWriterError("the model didn't write a usable prompt")
        return prompt[:MAX_CHARS]
