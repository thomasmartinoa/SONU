"""A project's own commands: "run the tests", "run the build" (D65).

SONU runs only commands the project itself lists — its ``package.json`` scripts, ``justfile``
recipes and ``Makefile`` targets — plus a few it can tell from the project's files (``uv run
pytest`` for a uv project with pytest set up, ``cargo test``, ``go test ./...``). Never a command
line from the model or from the user's words: the words only pick one of these by name.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sonu.desktop.apps.resolver import normalize

MAX_TASKS = 60
MAX_FILE = 256 * 1024  # a project file bigger than this isn't read
# The words people use for a command → the name projects give it.
SYNONYMS: dict[str, str] = {
    "tests": "test",
    "testing": "test",
    "test suite": "test",
    "unit tests": "test",
    "linter": "lint",
    "linting": "lint",
    "lints": "lint",
    "type check": "typecheck",
    "type checks": "typecheck",
    "type checker": "typecheck",
    "types": "typecheck",
    "compile": "build",
    "the build": "build",
    "dev server": "dev",
    "development server": "dev",
    "formatter": "format",
}
# Commands that send the project somewhere (or change something outside the laptop): asked as high risk.
OUTWARD = re.compile(r"(?:^|[^a-z])(?:deploy|publish|release|push|upload|ship)(?:$|[^a-z])")
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)", re.M)
_JUST_RECIPE = re.compile(r"^@?([A-Za-z0-9][A-Za-z0-9_-]*)(?:\s+[^:=\n]*)?:(?!=)", re.M)


@dataclass(frozen=True, slots=True)
class Task:
    name: str  # "test"
    argv: tuple[str, ...]  # ("pnpm", "run", "test")
    source: str  # "package.json"
    runs: str = ""  # what the project says it does ("vitest run"), shown when asking
    listed: bool = True  # the project lists it (False: SONU inferred it from the project's files)

    @property
    def command(self) -> str:
        return " ".join(self.argv)

    @property
    def outward(self) -> bool:
        return bool(OUTWARD.search(self.name.lower()))


def _text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            return None
        return path.read_text(errors="replace")
    except OSError:
        return None


def _package_manager(folder: Path) -> str:
    for lock, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
        ("bun.lockb", "bun"),
    ):
        if (folder / lock).exists():
            return manager
    return "npm"


def _npm_scripts(folder: Path) -> list[Task]:
    text = _text(folder / "package.json")
    if text is None:
        return []
    try:
        scripts = json.loads(text).get("scripts")
    except (ValueError, AttributeError):
        return []
    if not isinstance(scripts, dict):
        return []
    manager = _package_manager(folder)
    return [
        Task(name, (manager, "run", name), "package.json", str(body)[:200])
        for name, body in scripts.items()
        if isinstance(name, str) and re.fullmatch(r"[\w:.-]{1,64}", name)
    ]


def _recipes(folder: Path) -> list[Task]:
    path = next((folder / n for n in ("justfile", "Justfile", ".justfile") if (folder / n).is_file()), None)
    text = _text(path) if path else None
    if text is None:
        return []
    names = dict.fromkeys(m for m in _JUST_RECIPE.findall(text) if m not in ("set", "alias", "export"))
    return [Task(name, ("just", name), "justfile") for name in names]


def _make_targets(folder: Path) -> list[Task]:
    path = next((folder / n for n in ("GNUmakefile", "makefile", "Makefile") if (folder / n).is_file()), None)
    text = _text(path) if path else None
    if text is None:
        return []
    return [Task(name, ("make", name), "Makefile") for name in dict.fromkeys(_MAKE_TARGET.findall(text))]


def _inferred(folder: Path) -> list[Task]:
    """Commands a project of a known kind has without listing them."""
    found: list[Task] = []
    pyproject = _text(folder / "pyproject.toml")
    if pyproject is not None and (folder / "uv.lock").is_file():
        try:
            tools = tomllib.loads(pyproject).get("tool", {})
        except tomllib.TOMLDecodeError:
            tools = {}
        if "pytest" in tools or "pytest" in pyproject or (folder / "tests").is_dir():
            found.append(Task("test", ("uv", "run", "pytest"), "pyproject.toml", listed=False))
        if "ruff" in tools:
            found.append(Task("lint", ("uv", "run", "ruff", "check"), "pyproject.toml", listed=False))
        if "mypy" in tools:
            found.append(Task("typecheck", ("uv", "run", "mypy"), "pyproject.toml", listed=False))
    if (folder / "Cargo.toml").is_file():
        found += [
            Task(name, ("cargo", name), "Cargo.toml", listed=False) for name in ("test", "build", "check")
        ]
    if (folder / "go.mod").is_file():
        found += [Task(name, ("go", name, "./..."), "go.mod", listed=False) for name in ("test", "build")]
    return found


def project_tasks(folder: Path) -> list[Task]:
    """Every command SONU may run in ``folder``: what the project lists first, then what it infers."""
    tasks = [*_npm_scripts(folder), *_recipes(folder), *_make_targets(folder), *_inferred(folder)]
    return tasks[:MAX_TASKS]


def is_project(folder: Path) -> bool:
    markers = ("package.json", "pyproject.toml", "Cargo.toml", "go.mod", "Makefile", "justfile", ".git")
    return any((folder / marker).exists() for marker in markers)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def matching_tasks(tasks: list[Task], words: str, which: Callable[[str], str | None]) -> list[Task]:
    """The commands the user's words name: by name ("test", "build:prod"), or the usual word for it
    ("the tests" → test). Listed ones win over inferred ones; then a name containing the word."""
    said = " ".join(w for w in normalize(words).split() if w not in ("the", "my", "a", "all", "run"))
    wanted = {_key(said), _key(SYNONYMS.get(said, said))}
    if said.endswith("s"):
        wanted.add(_key(said[:-1]))  # "builds" → build
    wanted.discard("")
    exact = [task for task in tasks if _key(task.name) in wanted]
    if not exact:  # "unit tests" → test:unit; "lint" → lint:fix and lint:check
        parts = {w for w in wanted if len(w) >= 3}
        exact = [t for t in tasks if parts & {_key(p) for p in re.split(r"[^A-Za-z0-9]+", t.name)}]
    listed = [task for task in exact if task.listed]
    found = listed or exact
    usable = [task for task in found if which(task.argv[0])]
    return usable or found
