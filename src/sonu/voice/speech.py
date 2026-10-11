"""Text as SONU says it (Kokoro reads text through espeak-ng): paths, links, times and long lists said the
way a person would.

Measured on the laptop with ``espeak-ng -x`` (2026-10-12): "~/Documents" was "tilde slash documents",
"https://trails.example/kestrel" every letter and slash, "18:00" "eighteen zero zero", "09:05" "nine zero
five". Private results (a file list, tabs, history) are read out as they are, so these came up by voice.
"""

from __future__ import annotations

import re

_URL = re.compile(r"\bhttps?://(?:www\.)?([^/\s)\]]+)[^\s)\]]*")
_PATH = re.compile(r"(?<![\w.])~(?:/[^\s;,()“”\"']+)*")
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
KEEP = 3  # items of a long list said out loud; the rest is "and N more"


def _path(match: re.Match[str]) -> str:
    parts = [part.replace("_", " ") for part in match.group(0).split("/")[1:] if part]
    if not parts:
        return "your home folder"
    return f"{parts[-1]} in {parts[-2]}" if len(parts) >= 2 else parts[-1]


def _time(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    clock = hour % 12 or 12
    minutes = "" if minute == 0 else f" oh {minute}" if minute < 10 else f" {minute}"
    return f"{clock}{minutes} {'AM' if hour < 12 else 'PM'}"


def for_speech(text: str) -> str:
    text = _URL.sub(lambda m: m.group(1), text)
    text = _PATH.sub(_path, text)
    text = _TIME.sub(_time, text)
    return " ".join(text.split())


def shorten_list(line: str, keep: int = KEEP) -> str:
    """ "N files match …: a; b; c; d; e" → the first ``keep`` items and "and 2 more" (an existing "and N more"
    is added up)."""
    head, sep, rest = line.partition(": ")
    if not sep or "; " not in rest:
        return line
    items = rest.split("; ")
    more = 0
    if (last := re.fullmatch(r"and (\d+) more\.?", items[-1].strip())) is not None:
        more = int(last.group(1))
        items = items[:-1]
    if len(items) <= keep:
        return line
    more += len(items) - keep
    return f"{head}: {'; '.join(items[:keep])}; and {more} more"
