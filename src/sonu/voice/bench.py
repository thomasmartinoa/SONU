"""`sonu voice bench`: which speech model hears *this* user best (phase 2, D105).

The owner reads ~70 lines once — names SONU must get exactly, numbers, answers, their own way of talking,
"Hey Sonu", and a few requests said freely — and the recordings are **kept on the laptop** (SONU's data
folder, 0700), so every model, today's and later ones, hears the very same audio. They are also the test set
for a model trained on the owner's voice later (phase 7): never trained on.

A model is scored on what decides what SONU does, not only on words:

* **key words** — the app, number, package, file or song in each line, exact after spelling is evened out
  ("2" = "two", "VS Code" = "vscode", "dev_works" = "dev works");
* **answers** — yes / no / "the second one" / "none", the words that answer SONU's questions;
* **word error rate** — the usual measure, for comparing with published numbers;
* **time** from the recording to the text, and what it costs.

Where the files are (``<data>/voice/bench/<take>/``): ``<line id>.wav`` and ``said.json`` (what each line
should say — for free speech, what the owner typed they said), and ``results/<model>.json`` (what each model
heard). Deleting the folder deletes all of it.
"""

from __future__ import annotations

import json
import re
import wave
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sonu.voice.wake import fix_name

RATE = 16_000


@dataclass(frozen=True)
class Line:
    id: str
    group: str
    text: str  # what to say ("" for free speech: the owner types what they said)
    keys: tuple[str, ...] = ()  # key words, each "one|or another"
    prompt: str = ""  # free speech: what to talk about
    options: tuple[str, ...] = ()  # answers: the choices SONU would be offering (go to the model as a hint)


# What each answer to a list means: the option's place (0 = the first), or None for "none".
CHOSEN: dict[str, int | None] = {
    "ans-first": 0, "ans-second": 1, "ans-three": 2, "ans-last": 2, "ans-downloads": 0, "ans-none": None,
}  # fmt: skip

GROUPS = {
    "names": "Names SONU must get exactly (apps, packages, files, songs)",
    "numbers": "Numbers",
    "answers": "Answers to SONU's questions",
    "settings": "Settings and questions",
    "phrasing": "Your way of talking",
    "wake": "“Hey Sonu”",
    "free": "Said freely (not read)",
}

_FILES = ("Resume.pdf in Downloads", "resume.pdf in Portfolio", "resume.pdf in dist")


def _l(id: str, group: str, text: str, *keys: str, options: tuple[str, ...] = ()) -> Line:
    return Line(id, group, text, keys, options=options)


LINES: tuple[Line, ...] = (
    # ---- names: apps
    _l("app-kitty", "names", "Open kitty on workspace two.", "kitty", "2"),
    _l("app-zen", "names", "Open Zen on workspace four.", "zen", "4"),
    _l("app-claude", "names", "Open Claude Code in dev works.", "claude", "dev works"),
    _l("app-nvim", "names", "Open nvim in five.", "nvim|neovim|envim|n vim|en vim", "5"),
    _l("app-btop", "names", "Open btop.", "btop|b top|bee top"),
    _l("app-obs", "names", "Start OBS and start recording.", "obs", "recording"),
    _l(
        "app-vscode", "names", "Open VS Code in the SONU folder.", "vs code|vscode|visual studio code", "sonu"
    ),
    _l("app-telegram", "names", "Open Telegram in nine.", "telegram", "9"),
    _l("app-thunar", "names", "Open Thunar in Downloads.", "thunar", "downloads"),
    _l("app-chromium", "names", "Open Chromium and go to github dot com.", "chromium", "github"),
    _l("app-focus-zen", "names", "Focus on Zen.", "zen"),
    # ---- names: packages
    _l("pkg-cowsay", "names", "Install cowsay.", "install", "cowsay|cow say"),
    _l("pkg-fastfetch", "names", "Install fastfetch.", "install", "fastfetch|fast fetch"),
    _l("pkg-htop", "names", "Remove htop.", "remove", "htop|h top"),
    _l("pkg-yay", "names", "Is yay installed?", "yay"),
    # ---- names: files and folders
    _l("file-resume", "names", "Copy the path of my resume PDF.", "resume", "pdf"),
    _l("file-parking", "names", "Open my parking notes.", "parking notes"),
    _l("file-sonu-test", "names", "Open Claude Code in sonu test.", "claude", "sonu test"),
    _l("file-shop-app", "names", "Make a folder called shop app in dev works.", "shop app", "dev works"),
    _l("file-invoice", "names", "Where is my invoice PDF?", "invoice", "pdf"),
    _l("file-screenshot", "names", "Show me my last screenshot.", "screenshot"),
    # ---- names: songs and artists
    _l("song-alone", "names", "Play Alone by Marshmello.", "alone", "marshmello|marshmallow"),
    _l(
        "song-believer",
        "names",
        "Play Believer by Imagine Dragons on Spotify.",
        "believer",
        "imagine dragons",
        "spotify",
    ),
    _l("song-lofi", "names", "Play some lofi beats on YouTube.", "lofi|lo fi|low fi", "youtube"),
    _l("song-kesariya", "names", "Play Kesariya by Arijit Singh.", "kesariya|kesaria", "arijit singh|arijit"),
    # ---- numbers
    _l("num-volume", "numbers", "Set the volume to thirty percent.", "volume", "30"),
    _l("num-brightness", "numbers", "Set the brightness to seventy.", "brightness", "70"),
    _l("num-remind", "numbers", "Remind me in twenty minutes to stretch.", "20", "stretch"),
    _l("num-move", "numbers", "Move this window to workspace six.", "6"),
    _l("num-down", "numbers", "Turn the volume down a little.", "volume", "down"),
    _l("num-go", "numbers", "Go to workspace three.", "3"),
    # ---- answers (to "Should I …? Say yes or no", and to a list of choices)
    _l("ans-yes", "answers", "Yes.", "yes|yeah|yep|yup"),
    _l("ans-no", "answers", "No.", "no|nope|nah"),
    _l("ans-yeah", "answers", "Yeah, do it.", "yes|yeah|yep|yup"),
    _l("ans-dont", "answers", "No, don't.", "no|nope|nah"),
    _l("ans-second", "answers", "The second one.", "second|2nd", options=_FILES),
    _l("ans-three", "answers", "Number three.", "3", options=_FILES),
    _l("ans-first", "answers", "The first one.", "first|1st", options=_FILES),
    _l("ans-last", "answers", "The last one.", "last", options=_FILES),
    _l("ans-downloads", "answers", "The one in Downloads.", "downloads", options=_FILES),
    _l("ans-none", "answers", "None of them.", "none", options=_FILES),
    _l("ans-cancel", "answers", "Cancel.", "cancel"),
    # ---- settings and questions
    _l("set-night", "settings", "Turn on the night light.", "night light"),
    _l("set-bt", "settings", "Turn off Bluetooth.", "off", "bluetooth"),
    _l("set-dnd", "settings", "Turn on do not disturb.", "do not disturb"),
    _l("set-power", "settings", "Switch to power saver.", "power saver"),
    _l("set-lock", "settings", "Lock the screen.", "lock"),
    _l("set-shot", "settings", "Take a screenshot of this window.", "screenshot", "window"),
    _l("q-battery", "settings", "What's my battery?", "battery"),
    _l("q-slow", "settings", "Why is my laptop so slow?", "slow"),
    _l("q-playing", "settings", "What's playing?", "playing"),
    _l("q-remember", "settings", "What do you remember about me?", "remember"),
    # ---- the owner's way of talking
    _l("say-github", "phrasing", "Can you open me GitHub in workspace three.", "github", "3"),
    _l("say-one-thing", "phrasing", "Do one thing, put kitty in five only.", "kitty", "5"),
    _l("say-bye", "phrasing", "Okay then bye.", "bye"),
    _l("say-filler", "phrasing", "Hey okay so um can you open Telegram in nine.", "telegram", "9"),
    _l(
        "say-long-apps",
        "phrasing",
        "Open browser in four and nvim in five and Claude Code in five and VS Code in six.",
        "browser",
        "4",
        "nvim|neovim|envim|n vim|en vim",
        "claude",
        "vs code|vscode|visual studio code",
        "6",
    ),
    _l(
        "say-long-gmail",
        "phrasing",
        "Play Shape of You in Spotify and open my GitHub in workspace nine and Gmail in workspace eight.",
        "shape of you",
        "spotify",
        "github",
        "9",
        "gmail",
        "8",
    ),
    _l(
        "say-long-copy",
        "phrasing",
        "Copy the path of resume PDF and open Claude Code in dev works.",
        "resume",
        "pdf",
        "claude",
        "dev works",
    ),
    _l(
        "say-long-move",
        "phrasing",
        "Move Zen to three, move Telegram to three too, put them side by side and go to three.",
        "zen",
        "telegram",
        "side by side",
        "3",
    ),
    _l(
        "say-long-settings",
        "phrasing",
        "Mute the mic, set the volume to fifty and turn on the night light.",
        "mic",
        "50",
        "night light",
    ),
    _l("say-thanks", "phrasing", "Thanks Sonu, that's all.", "thanks|thank you", "sonu"),
    # ---- "Hey Sonu"
    _l("wake-alone", "wake", "Hey Sonu.", "sonu"),
    _l("wake-kitty", "wake", "Hey Sonu, open kitty.", "sonu", "kitty"),
    _l("wake-time", "wake", "Hey Sonu, what's the time?", "sonu", "time"),
    _l("wake-stop", "wake", "Sonu, stop.", "sonu", "stop"),
    # ---- said freely: what the owner really says, typed by them after
    Line("free-apps", "free", "", prompt="Ask SONU to open two apps on two workspaces — your own way"),
    Line("free-song", "free", "", prompt="Ask SONU to play a song you like"),
    Line("free-laptop", "free", "", prompt="Ask SONU something about your laptop"),
    Line("free-any", "free", "", prompt="Ask SONU anything, the way you'd really say it"),
)
BY_ID = {line.id: line for line in LINES}


# ---------------------------------------------------------------------------------------------- text
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}  # fmt: skip


def normalize(text: str) -> list[str]:
    """Lowercase words, punctuation gone, numbers as digits ("thirty five" = "35" = "35%" minus the sign),
    the name evened out ("Sonu"), words a model wrote together split ("OpenKitty")."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", fix_name(text))
    text = text.lower().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'").replace("%", " percent ")
    found = re.findall(r"[a-z0-9']+", text)
    out: list[str] = []
    for word in found:
        word = word.strip("'")
        if not word:
            continue
        if word in _UNITS:
            if out and out[-1].isdigit() and int(out[-1]) in _TENS.values() and _UNITS[word] < 10:
                out[-1] = str(int(out[-1]) + _UNITS[word])  # "twenty five"
            else:
                out.append(str(_UNITS[word]))
        elif word in _TENS:
            out.append(str(_TENS[word]))
        else:
            out.append(word)
    return out


def _joined(words: list[str], longest: int = 4) -> set[str]:
    """Every run of up to ``longest`` neighbouring words, written together: "dev works" → "devworks"."""
    return {"".join(words[i : i + n]) for n in range(1, longest + 1) for i in range(len(words) - n + 1)}


def key_found(key: str, heard: str) -> bool:
    """One key word ("vs code|vscode|visual studio code") is in what was heard."""
    runs = _joined(normalize(heard))
    return any("".join(normalize(option)) in runs for option in key.split("|"))


def word_errors(expected: str, heard: str) -> tuple[int, int]:
    """(words substituted, missed or added; words expected) — word-level edit distance."""
    want, got = normalize(expected), normalize(heard)
    row = list(range(len(got) + 1))
    for i, w in enumerate(want, 1):
        previous, row[0] = row[0], i
        for j, g in enumerate(got, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (w != g))
    return row[-1], len(want)


# ---------------------------------------------------------------------------------------------- files
def bench_dir(voice_folder: Path, take: str = "quiet") -> Path:
    return voice_folder / "bench" / take


def _private_folder(folder: Path) -> Path:
    for part in (folder.parent.parent, folder.parent, folder):  # voice/bench/<take>: all 0700
        part.mkdir(mode=0o700, parents=True, exist_ok=True)
        part.chmod(0o700)
    return folder


def save_recording(folder: Path, line_id: str, pcm16: bytes) -> Path:
    _private_folder(folder)
    path = folder / f"{line_id}.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(pcm16)
    path.chmod(0o600)
    return path


def load_recording(folder: Path, line_id: str) -> bytes:
    with wave.open(str(folder / f"{line_id}.wav"), "rb") as file:
        return file.readframes(file.getnframes())


def said(folder: Path) -> dict[str, str]:
    """What each recorded line should say: the line's text, or what the owner typed for free speech."""
    try:
        data = json.loads((folder / "said.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def remember_said(folder: Path, line_id: str, text: str) -> None:
    known = said(_private_folder(folder))
    known[line_id] = text
    path = folder / "said.json"
    path.write_text(json.dumps(known, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def recorded(folder: Path) -> list[Line]:
    """The lines recorded so far, in the bench's order (free speech only once its words were typed)."""
    known = said(folder)
    return [line for line in LINES if (folder / f"{line.id}.wav").exists() and line.id in known]


def results_file(folder: Path, model: str) -> Path:
    return folder / "results" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', model)}.json"


def save_heard(folder: Path, model: str, heard: Mapping[str, tuple[str, float]]) -> None:
    path = results_file(folder, model)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = {"model": model, "lines": {k: {"heard": t, "seconds": round(s, 3)} for k, (t, s) in heard.items()}}
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def load_heard(folder: Path, model: str) -> dict[str, tuple[str, float]]:
    try:
        data = json.loads(results_file(folder, model).read_text(encoding="utf-8"))
        return {k: (str(v["heard"]), float(v["seconds"])) for k, v in data["lines"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def models_heard(folder: Path) -> list[str]:
    out = []
    for path in sorted((folder / "results").glob("*.json")):
        try:
            out.append(str(json.loads(path.read_text(encoding="utf-8"))["model"]))
        except (OSError, ValueError, KeyError):
            continue
    return out


# ---------------------------------------------------------------------------------------------- scores
@dataclass
class Tally:
    keys: int = 0
    keys_right: int = 0
    errors: int = 0
    words: int = 0
    lines: int = 0
    seconds: float = 0.0

    def add(self, line: Line, expected: str, heard: str, seconds: float) -> list[str]:
        """Count one line; the key words it missed."""
        missed = [key for key in line.keys if not key_found(key, heard)]
        self.keys += len(line.keys)
        self.keys_right += len(line.keys) - len(missed)
        errors, words = word_errors(expected, heard)
        self.errors, self.words = self.errors + errors, self.words + words
        self.lines += 1
        self.seconds += seconds
        return missed

    @property
    def key_score(self) -> float | None:
        return self.keys_right / self.keys if self.keys else None

    @property
    def wer(self) -> float | None:
        return self.errors / self.words if self.words else None

    @property
    def mean_seconds(self) -> float:
        return self.seconds / self.lines if self.lines else 0.0


@dataclass
class Score:
    model: str
    all: Tally = field(default_factory=Tally)
    groups: dict[str, Tally] = field(default_factory=dict)
    misses: list[tuple[str, str, list[str]]] = field(default_factory=list)  # (line id, heard, missed keys)
    audio_seconds: float = 0.0
    with_sonu: Tally = field(default_factory=Tally)  # after SONU matched names by sound (D107)
    chosen: int = 0  # answers to a list that SONU turned into the right choice (D109) ...
    chosen_of: int = 0  # ... out of the answers recorded
    wrong_choices: list[tuple[str, str]] = field(default_factory=list)  # (line id, heard)


def score(
    model: str, lines: Iterable[Line], expected: Mapping[str, str], heard: Mapping[str, tuple[str, float]],
    durations: Mapping[str, float] | None = None, meant: Callable[[str], str] | None = None,
) -> Score:  # fmt: skip
    """How well ``model`` heard the lines; ``meant``: SONU's matching of names by sound, scored as well."""
    result = Score(model)
    for line in lines:
        if line.id not in heard or line.id not in expected:
            continue
        text, seconds = heard[line.id]
        missed = result.all.add(line, expected[line.id], text, seconds)
        result.groups.setdefault(line.group, Tally()).add(line, expected[line.id], text, seconds)
        result.audio_seconds += (durations or {}).get(line.id, 0.0)
        if meant is not None:
            result.with_sonu.add(line, expected[line.id], meant(text), seconds)
        if missed:
            result.misses.append((line.id, text, missed))
        if line.id in CHOSEN:
            result.chosen_of += 1
            if _chose(line, text) == CHOSEN[line.id]:
                result.chosen += 1
            else:
                result.wrong_choices.append((line.id, text))
    return result


def _chose(line: Line, heard: str) -> int | str | None:
    """The place SONU would pick for ``heard`` (None: "none"; "?": it would ask again). A wrong pick is
    worse than a missed word: it is counted the same, but listed on its own."""
    from sonu.tools.base import Choice
    from sonu.voice.choosing import pick

    picked = pick(heard, [Choice(label, str(i)) for i, label in enumerate(line.options)])
    if picked.kind == "pick":
        return int(picked.value or 0)
    return None if picked.kind == "none" else "?"


# What a request costs (vendor prices, 2026-10): per second of audio, and the currency.
PRICES: dict[str, tuple[float, str]] = {
    "sarvam": (30 / 3600, "₹"),  # Saaras v4: ₹30 an hour
    "gemini-transcribe": (0.005 / 60, "$"),  # Gemini 3.5 Transcribe: $0.005 a minute
}


def cost_per_1000(model: str, mean_audio_s: float) -> str:
    """What 1000 requests of this length cost ("free" for local; "?" when it's priced by tokens)."""
    base = model.split(":", 1)[0].removesuffix("-plain")
    if base in PRICES:
        rate, currency = PRICES[base]
        return f"{currency}{rate * mean_audio_s * 1000:.2f}"
    return "?" if base in CLOUD else "free"


CLOUD = {"sarvam", "sarvam-plain", "gemini", "gemini-transcribe"}


def wait_for_limit(error: str) -> float | None:
    """A cloud model's rate limit (429): how long to wait before trying again; None for any other error.
    Found running the bench: Gemini 3.5 Transcribe allows 3 requests a minute on the free tier."""
    if " 429" not in error and "rate limit" not in error.lower():
        return None
    said = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", error, re.I)
    return min(float(said.group(1)) + 1, 90.0) if said else 30.0


def used_up(error: str) -> bool:
    """A limit that waiting a minute won't fix (a daily quota, a retry hours away): that model stops for now
    and keeps what it heard. Found running the bench: Gemini's free tier allows 20 requests a day per model,
    and the run waited on it line after line."""
    if "PerDay" in error:
        return True
    said = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", error, re.I)
    return said is not None and float(said.group(1)) > 600


def hint_for(line: Line, everyday_hint: str) -> str:
    """What the model is told to expect: SONU's everyday hint (installed apps, yes/no) — never the line's
    own words — plus, for an answer to a list, the choices on screen (as SONU will do, phase 2)."""
    return everyday_hint + (". " + ", ".join(line.options) if line.options else "")


def as_json(scores: Iterable[Score]) -> list[dict[str, Any]]:
    out = []
    for s in scores:
        out.append(
            {
                "model": s.model,
                "key_words": s.all.key_score,
                "key_words_with_sonu": s.with_sonu.key_score,
                "answers": s.groups["answers"].key_score if "answers" in s.groups else None,
                "choices_understood": s.chosen / s.chosen_of if s.chosen_of else None,
                "wer": s.all.wer,
                "seconds": s.all.mean_seconds,
                "lines": s.all.lines,
                "groups": {g: {"key_words": t.key_score, "wer": t.wer} for g, t in s.groups.items()},
            }
        )
    return out
