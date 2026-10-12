"""sonud's side of the HUD (D80): what SONU is doing, written to a state file its own Quickshell HUD
(``src/sonu/hud/shell.qml``) shows — listening, thinking, each step as it happens, the result — and the
questions it asks there, answered by a click (the answer file).

Both files live in SONU's 0700 runtime folder (``$XDG_RUNTIME_DIR/sonu/hud``); the state is replaced
atomically, the answer is read once and deleted. The HUD is a separate ``qs`` instance sonud starts: the
owner's own shell and dotfiles are never touched. Everything it shows is on the user's screen only — the
same lines the terminal prints; nothing goes to a model or the audit log from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ANSWER_TIMEOUT_S = 120.0  # nobody answered: the step isn't done (like closing the picker)
POLL_S = 0.1
MAX_STEPS_SHOWN = 8


def hud_folder(env: dict[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    base = environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "sonu" / "hud"


def hud_qml() -> Path:
    """SONU's own HUD (QML), as installed with the package."""
    return Path(str(resources.files("sonu.hud") / "shell.qml"))


def _icon_free(line: str) -> str:
    """ "✔ Opened kitty" → "Opened kitty" (the HUD draws its own icons)."""
    first, _, rest = line.partition(" ")
    return rest if rest and len(first) <= 2 and not first.isalnum() else line


class Hud:
    def __init__(self, folder: Path, hide_after_s: float = 6.0) -> None:
        self.folder, self.hide_after_s = folder, hide_after_s
        self.state: dict[str, Any] = {"state": "idle"}
        self.seq = 0
        self._ids = itertools.count(1)
        self._process: asyncio.subprocess.Process | None = None
        self._after_question: str | None = None

    @property
    def state_file(self) -> Path:
        return self.folder / "state.json"

    @property
    def answer_file(self) -> Path:
        return self.folder / "answer.json"

    # ------------------------------------------------------------------ the HUD process
    async def start(self, qs: str | None = None, env: dict[str, str] | None = None) -> bool:
        """Start SONU's HUD (``qs -n -p <shell.qml>``) in the session ``env``; False without Quickshell."""
        program = qs or shutil.which("qs", path=(env or os.environ).get("PATH"))
        if program is None:
            return False
        self._write()
        env = {**os.environ, **(env or {}), "SONU_HUD_DIR": str(self.folder)}
        self._process = await asyncio.create_subprocess_exec(
            program,
            "-n",
            "-p",
            str(hud_qml()),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return True

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.terminate()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), 3.0)

    # ------------------------------------------------------------------ what it shows
    def _write(self) -> None:
        self.seq += 1
        self.state["seq"] = self.seq
        self.state["hide_after_s"] = self.hide_after_s
        try:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.folder.chmod(0o700)
            temporary = self.folder / ".state.json.tmp"
            temporary.write_text(json.dumps(self.state, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(self.state_file)
        except OSError as exc:
            logger.debug("the HUD's state wasn't written: %s", exc)

    def show(self, **fields: Any) -> None:
        # Found by a flaky test (#74): progress sonud had queued before a question ("step started") arrived
        # while it was open and switched the HUD to "working" — the buttons went, nobody could answer. A
        # question stays until it's answered (ask() closes it itself).
        if self._asking() and "question" not in fields:
            if "state" in fields:
                self._after_question = str(fields["state"])  # where the HUD goes once it's answered
            fields = {k: v for k, v in fields.items() if k != "state"}
        self.state.update(fields)
        self._write()

    def _asking(self) -> bool:
        question = self.state.get("question")
        return (
            self.state.get("state") == "asking"
            and isinstance(question, dict)
            and bool(question.get("options"))
        )

    def begin(self, request: str = "", state: str = "thinking") -> None:
        self.state = {"state": state, "request": request, "steps": []}
        self._write()

    def idle(self) -> None:
        self.state = {"state": "idle"}
        self._write()

    def _steps(self) -> list[dict[str, str]]:
        steps: list[dict[str, str]] = self.state.setdefault("steps", [])
        return steps

    def feed(self, message: dict[str, Any]) -> None:
        """A progress message sonud sends its client — the HUD shows the same thing."""
        kind = message.get("type")
        if kind == "listening":
            self.begin("", "listening")
        elif kind == "heard":
            self.begin(str(message.get("text") or ""), "thinking")
        elif kind == "plan":
            self.show(state="acting" if message.get("steps") else "thinking")
        elif kind == "step_start":
            self._steps().append({"text": str(message.get("description") or ""), "status": "running"})
            del self._steps()[:-MAX_STEPS_SHOWN]
            self.show(state="acting")
        elif kind == "step":
            line = _icon_free(str(message.get("line") or ""))
            status = str(message.get("status") or "done")
            steps = self._steps()
            if steps and steps[-1]["status"] == "running":
                steps[-1] = {"text": line, "status": status}
            else:
                steps.append({"text": line, "status": status})
            self.show(question=None)
        elif kind == "show":
            self.show(details=str(message.get("text") or ""))
        elif kind in ("confirm", "choose"):  # asked in the terminal: shown here, answered there
            text = message.get("description") or message.get("title") or ""
            self.show(
                state="asking", question={"id": 0, "text": f"{text}?", "note": "Answer in the terminal"}
            )
        elif kind == "error":
            self.show(state="done", result=f"✖ {message.get('message', '')}", question=None)
        elif kind == "final":
            self._final(message)

    def _final(self, message: dict[str, Any]) -> None:
        report = message.get("report") or {}
        lines = [str(line) for line in message.get("before_headline") or []]
        result = (report.get("reply") if isinstance(report, dict) else None) or message.get("headline") or ""
        if not result and lines:
            result = _icon_free(lines[-1].removeprefix("» "))
        if not result and not self._steps():
            self.idle()  # a quiet end ("thanks"): nothing to show
            return
        self.show(state="done", result=str(result), question=None, details="")

    # ------------------------------------------------------------------ questions
    async def ask(
        self,
        text: str,
        options: list[tuple[str, Any]],
        *,
        risk: str = "",
        note: str = "",
        details: str = "",
        timeout_s: float = ANSWER_TIMEOUT_S,
    ) -> Any:
        """Show a question with buttons; the value of the one the user clicked, or None (no answer)."""
        question_id = next(self._ids)
        with contextlib.suppress(FileNotFoundError):
            self.answer_file.unlink()
        before = self.state.get("state", "acting")
        self._after_question = None
        self.show(
            state="asking",
            details=details,
            question={
                "id": question_id,
                "text": text,
                "risk": risk,
                "note": note,
                "options": [{"label": label, "value": value} for label, value in options],
            },
        )
        offered = [value for _, value in options]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        answer: Any = None
        try:
            while loop.time() < deadline:
                got = self._read_answer(question_id)
                if got is not _NO_ANSWER and got in offered:  # only a value SONU itself offered
                    answer = got
                    break
                await asyncio.sleep(POLL_S)
        finally:
            with contextlib.suppress(FileNotFoundError):
                self.answer_file.unlink()
            after = self._after_question or (before if before != "asking" else "acting")
            self.show(state=after, question=None, details="")
        return answer

    def _read_answer(self, question_id: int) -> Any:
        try:
            data = json.loads(self.answer_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _NO_ANSWER
        if not isinstance(data, dict) or data.get("id") != question_id:
            return _NO_ANSWER
        return data.get("value")


_NO_ANSWER = object()


def hud_confirmer(hud: Hud) -> Any:
    """Yes / No on the HUD for a step that needs the user's OK (what the picker does without it)."""

    async def confirm(request: Any) -> bool:
        risk = getattr(request.risk, "value", str(request.risk))
        note = "This one can't be undone — read it first" if risk == "high" else ""
        answer = await hud.ask(
            f"{request.description}?",
            [("Yes", True), ("No", False)],
            risk=risk,
            note=note,
            details=request.details or "",
        )
        return answer is True

    return confirm


def hud_chooser(hud: Hud) -> Any:
    """The user picks one of SONU's choices on the HUD."""

    async def choose(choices: Any) -> str | None:
        options = [(option.label, option.value) for option in choices.options]
        answer = await hud.ask(choices.prompt, [*options, ("None of these", None)])
        return answer if isinstance(answer, str) else None

    return choose
