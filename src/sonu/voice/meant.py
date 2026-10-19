"""The names the user meant when the recogniser wrote them another way — matched by sound against what
exists: installed apps, the user's projects, SONU's own names (D107) — and a workspace number heard as
another word ("in night" is 9, D108).

Measured on the owner's recordings (`sonu voice bench`): every recogniser writes the same names wrong in
the same way — Claude as "Cloud", btop as "VTOP", nvim as "NWIM" / "EnVim", dev_works as "Dubworks" — and
then the plan fails ("'cloud-code' is not installed"). A list of look-alike words only fixes the words
someone noticed; this compares how words *sound* with the names that are really on this laptop, so a new
app or project is covered the day it appears.

Strict on purpose — a wrong name is worse than a misheard one, which at least fails visibly:
- only the same sound key, and only when exactly one name has it;
- a span never starts or ends with an everyday word ("the", "open", "them"), never crosses punctuation;
- one word is matched only if the recogniser wrote it as a name (a capital letter) and it's long enough;
- ordinary folder names ("Videos", "theme") aren't names to match: recognisers get those right.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sonu.desktop.apps.resolver import BUILTIN_ALIASES, AppResolver

# Words a name never starts or ends with.
_EVERYDAY = frozenset(
    """a an the to in on at of for from with into onto and or then than so but my me i i'm im you your it its
    is are was be been do does did what whats now them this that these those there here can could would
    will please hey okay ok yes no not just also one some all open close move put focus go play install
    remove show start launch run make set turn copy find get tell give switch take bring side by up down
    off""".split()  # noqa: SIM905
)
# SONU's own words, alone never a name: "the cloud" is its cloud settings, not the Claude app.
_OWN_WORDS = frozenset({"cloud", "local", "balanced"})
# How a letter is said, for names that start with one ("nvim" is "en vim", "btop" is "bee top").
_LETTERS = {
    "b": "bee", "c": "see", "d": "dee", "f": "ef", "g": "jee", "h": "aitch", "j": "jay", "k": "kay",
    "l": "el", "m": "em", "n": "en", "p": "pee", "q": "queue", "r": "ar", "s": "es", "t": "tee",
    "v": "vee", "w": "double u", "x": "ex", "z": "zed",
}  # fmt: skip
_VOWELS = "aeiouy"
_ONE_WORD_KEY = 4  # "VTOP" → btop; shorter keys ("zen", "kit") match too many words
_MANY_WORDS_KEY = 6  # "Cloud Code" → Claude Code
_MAX_WORDS = 3
_PLAIN = re.compile(r"^[A-Za-z]+$")
_INSIDE = re.compile("[^\\w'\N{RIGHT SINGLE QUOTATION MARK}-]")  # punctuation that ends a phrase
# Numbers are a closed set, unlike names, so what the recognisers wrote for each one on the owner's voice
# (`sonu voice bench`) is listed — and only used where a workspace number goes, at the end of a phrase:
# "workspace for." is 4, "in fire only" is 5, "in night" is 9.
_HEARD_FOR = {
    "tree": 3, "forum": 4, "file": 5, "fire": 5, "night": 9, "nite": 9, "nand": 9,
}  # fmt: skip
# Also everyday words, so only right after "workspace"
_WORDS_FOR = {"won": 1, "to": 2, "too": 2, "for": 4, "fore": 4, "ate": 8}
_PHRASE_ENDS = frozenset({"", "only", "and", "too", "please", "now", "then", "also"})


def sound_key(text: str) -> str:
    """How words sound, roughly: letters that sound alike merged (b/v/w, d/t, g/k, z/s, c), every run of
    vowels one vowel, a silent final vowel dropped — word by word, then joined. "Claude Code" = "Cloud
    Code", "VTOP" = "btop", "Dubworks" = "dev_works"."""
    keys = "".join(_word_key(word) for word in re.split(r"[^a-z]+", text.lower()) if word)
    return re.sub(r"(.)\1+", r"\1", keys)


def _word_key(t: str) -> str:
    for pair, sound in (("ph", "f"), ("ck", "k"), ("sh", "s"), ("ch", "k"), ("th", "t"), ("wh", "w"),
                        ("qu", "kw"), ("x", "ks"), ("gh", "")):  # fmt: skip
        t = t.replace(pair, sound)
    t = re.sub(r"c(?=[eiy])", "s", t)
    t = t.translate(str.maketrans("cqzvbdgj", "ksswwtkj"))
    t = re.sub(r"(?<=[^aeiouy])h|h$", "", t)
    t = re.sub(f"[{_VOWELS}]+", "a", t)
    t = re.sub(r"(.)\1+", r"\1", t)
    return t.removesuffix("a") if len(t) > 2 else t


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _said_as(name: str) -> set[str]:
    """The ways a name is said: as written; spelled out when it has no vowel ("vlc" is "vee el see");
    its first letter spelled when it starts with consonants no word starts with ("nvim" is "en vim")."""
    forms = {name}
    word = name.lower()
    if re.fullmatch(r"[a-z]{2,5}", word) and not re.search(f"[{_VOWELS}]", word):
        forms.add(" ".join(_LETTERS.get(letter, letter) for letter in word))
    elif (
        len(word) > 2
        and word[0] in _LETTERS
        and word[1] not in _VOWELS
        and not re.match(r"[bcdfgkpst][lr]|s[cfkmnptw]|[cgpstw]h", word)  # "Blender", "Spotify", "Thunar"
    ):
        forms.add(f"{_LETTERS[word[0]]} {name[1:]}")
    return forms


@dataclass(slots=True)
class Names:
    """What exists, by how it sounds."""

    by_key: dict[str, str] = field(default_factory=dict)
    exact: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, names: Iterable[str]) -> Names:
        found: dict[str, set[str]] = {}
        spelled: dict[str, str] = {}
        exact: set[str] = set()
        for name in names:
            name = name.strip()
            if not name or not re.search(r"[A-Za-z]", name):
                continue
            exact.add(_compact(name))
            # The plainest spelling of the same name: "btop", not "btop++"
            spelled[_compact(name)] = min(spelled.get(_compact(name), name), name, key=_fanciness)
            for form in _said_as(name):
                key = sound_key(form)
                if len(key) >= _ONE_WORD_KEY:
                    found.setdefault(key, set()).add(_compact(name))
        # One name per sound only — two names that sound alike ("kitty", "code"?) are never guessed between.
        by_key = {key: spelled[next(iter(same))] for key, same in found.items() if len(same) == 1}
        return cls(by_key, exact)

    def _joined(self, word: str) -> str | None:
        """A name run into the next word ("Zenon" is "Zen on", "kittyon" is "kitty on")."""
        core = word.rstrip(".,!?;:")
        said = re.fullmatch(r"([A-Za-z]{3,}?)(on|in|to)", core, re.I)
        if said is None or _compact(said.group(1)) not in self.exact or _compact(core) in self.exact:
            return None
        return f"{said.group(1)} {said.group(2).lower()}{word[len(core) :]}"

    def meant(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """``text`` with the names it meant, and what changed: [("Cloud Code", "Claude Code"), …]."""
        words = text.split(" ")
        out: list[str] = []
        changes: list[tuple[str, str]] = []
        i = 0
        while i < len(words):
            for size in range(min(_MAX_WORDS, len(words) - i), 0, -1):
                span = words[i : i + size]
                name = self._name_for(span)
                if name is None:
                    continue
                said = " ".join(span)
                ending = said[len(said.rstrip(".,!?;:")) :]
                core = said[: len(said) - len(ending)]
                out.append(name + ending)
                changes.append((core, name))
                i += size
                break
            else:
                joined = self._joined(words[i])
                if joined is not None:
                    changes.append((words[i].rstrip(".,!?;:"), joined.rstrip(".,!?;:")))
                out.append(joined or words[i])
                i += 1
        return " ".join(_numbers(out, changes)), changes

    def _name_for(self, span: list[str]) -> str | None:
        """The one existing name these words sound like, if they aren't already it."""
        if any(_INSIDE.search(word) for word in span[:-1]):
            return None  # punctuation inside: two different things
        core = [word.strip(".,!?;:") for word in span]
        if not all(_PLAIN.match(word) for word in core):
            return None  # numbers, paths, links
        if core[0].lower() in _EVERYDAY or core[-1].lower() in _EVERYDAY:
            return None
        if _compact("".join(core)) in self.exact:
            return None  # already a name
        if len(core) == 1 and (not re.search(r"[A-Z]", core[0]) or core[0].lower() in _OWN_WORDS):
            return None  # one everyday word ("send", "file"): only a word written as a name
        key = sound_key("".join(core))
        if len(key) < (_ONE_WORD_KEY if len(core) == 1 else _MANY_WORDS_KEY):
            return None
        return self.by_key.get(key)


def _numbers(words: list[str], changes: list[tuple[str, str]]) -> list[str]:
    """A workspace number heard as another word: after "workspace" ("workspace for." → 4, "workspace Forum
    and" → 4), or after "in" when it isn't a word there ("in file." → 5, "in night" → 9) — only at the end of
    a phrase, so "workspace to the left" and "in file manager" stay. Number words ("four") are left to the
    planner, which reads them."""
    out = list(words)
    for i in range(1, len(out)):
        word = out[i].rstrip(".,!?;:")
        before = out[i - 1].lower()
        after = "" if word != out[i] or i + 1 == len(out) else out[i + 1].lower().rstrip(".,!?;:")
        if after not in _PHRASE_ENDS or before.rstrip(".,!?;:") != before:
            continue
        said = word.lower()
        if before == "workspace":
            number = _HEARD_FOR.get(said) or _WORDS_FOR.get(said)
        elif before == "in":
            number = _HEARD_FOR.get(said)
        else:
            number = None
        if number is not None:
            out[i] = f"{number}{out[i][len(word) :]}"
            changes.append((word, str(number)))
    return out


def _fanciness(name: str) -> tuple[int, int]:
    return len(re.sub(r"[A-Za-z0-9 ]", "", name)), len(name)


def _made_up(name: str) -> bool:
    """A folder name no recogniser knows: dev_works, sonu-test, sonu2 (not "Videos" or "theme")."""
    return bool(re.search(r"[_\-0-9]", name)) or (name.isupper() and len(name) > 2)


def project_names(home: Path) -> list[str]:
    """The user's own folders worth matching: one or two levels below home, with a made-up name or a git
    repository (SONU)."""
    names: list[str] = []
    for pattern in ("*", "*/*"):
        try:
            folders = [p for p in home.glob(pattern) if p.is_dir()]
        except OSError:
            continue
        for folder in folders:
            parts = folder.relative_to(home).parts
            if any(part.startswith(".") for part in parts):
                continue
            if _made_up(folder.name) or (folder / ".git").exists():
                names.append(folder.name)
    return names


def known_names(resolver: AppResolver, home: Path) -> Names:
    """Everything SONU can be asked for by name, right now: the installed apps (names, programs, the
    names SONU and the user gave them), Claude Code, and the user's projects."""
    said: list[str] = ["Claude Code"]
    for entry in resolver.catalog.entries:
        if not entry.no_display:
            said += [value for value in (entry.name, entry.executable) if value]
    said += [*BUILTIN_ALIASES, *resolver.aliases]
    return Names.of([*said, *project_names(home)])
