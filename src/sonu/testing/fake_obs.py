"""A fake OBS Studio WebSocket server (obs-websocket 5) for tests and evals.

It speaks the real protocol — the RFC 6455 handshake, Hello with an authentication challenge,
Identify, Request / RequestResponse — so SONU's client is tested end to end, and it keeps the state
the real OBS keeps: recording (and paused), the program scene, streaming, the virtual camera, the
replay buffer and each input's mute.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from sonu.integrations.obs import auth_answer, encode_frame, read_frame

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass
class FakeObsState:
    scenes: list[str] = field(default_factory=lambda: ["Scene Main", "Scene only screen", "Scene 3"])
    scene: str = "Scene Main"
    inputs: dict[str, bool] = field(  # input name → muted
        default_factory=lambda: {"Desktop Audio": False, "Mic/Aux": False}
    )
    recording: bool = False
    paused: bool = False
    streaming: bool = False
    virtualcam: bool = False
    replay_buffer: bool = False
    recordings_dir: Path = field(default_factory=Path.cwd)  # tests point it at a temporary folder
    saved: list[str] = field(default_factory=list)  # files OBS wrote (recordings, replays)
    requests: list[str] = field(default_factory=list)
    stream_configured: bool = True  # False: no streaming service set up in OBS


class FakeObs:
    def __init__(self, state: FakeObsState | None = None, *, password: str | None = "secret") -> None:  # noqa: S107 — a fake server's test password
        self.state = state or FakeObsState()
        self.password = password
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self, port: int = 0) -> None:
        """Listen (on ``port``, e.g. to come up after SONU launched OBS; 0: any free port)."""
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # -------------------------------------------------------------- protocol
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._conversation(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _conversation(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        key = next(
            line.split(":", 1)[1].strip()
            for line in head.split("\r\n")
            if line.lower().startswith("sec-websocket-key")
        )
        accept = base64.b64encode(
            hashlib.sha1((key + _GUID).encode(), usedforsecurity=False).digest()
        ).decode()
        writer.write(
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n".encode()
        )
        hello: dict[str, Any] = {"obsWebSocketVersion": "5.6.2", "rpcVersion": 1}
        salt, challenge = "c2FsdA==", "Y2hhbGxlbmdl"
        if self.password:
            hello["authentication"] = {"salt": salt, "challenge": challenge}
        await self._send(writer, {"op": 0, "d": hello})
        identify = await self._receive(reader)
        answer = identify.get("d", {}).get("authentication")
        if self.password and answer != auth_answer(self.password, salt, challenge):
            writer.write(encode_frame(0x8, (4009).to_bytes(2, "big"), mask=False))
            await writer.drain()
            return
        await self._send(writer, {"op": 2, "d": {"negotiatedRpcVersion": 1}})
        while True:
            message = await self._receive(reader)
            if message.get("op") != 6:
                continue
            body = message["d"]
            ok, comment, data = self._handle(body["requestType"], body.get("requestData") or {})
            status: dict[str, Any] = {"result": ok, "code": 100 if ok else 600}
            if comment:
                status["comment"] = comment
            await self._send(
                writer,
                {
                    "op": 7,
                    "d": {
                        "requestType": body["requestType"],
                        "requestId": body["requestId"],
                        "requestStatus": status,
                        "responseData": data,
                    },
                },
            )

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write(encode_frame(0x1, json.dumps(message).encode(), mask=False))
        await writer.drain()

    @staticmethod
    async def _receive(reader: asyncio.StreamReader) -> dict[str, Any]:
        while True:
            _, opcode, payload = await read_frame(reader)
            if opcode == 0x8:
                raise ConnectionError("closed")
            if opcode == 0x1:
                message = json.loads(payload)
                return message if isinstance(message, dict) else {}

    # -------------------------------------------------------------- requests
    def _handle(self, kind: str, data: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
        s = self.state
        s.requests.append(kind)
        if kind == "GetRecordStatus":
            return (
                True,
                "",
                {"outputActive": s.recording, "outputPaused": s.paused, "outputTimecode": "00:00:05.000"},
            )
        if kind == "StartRecord":
            if s.recording:
                return False, "Recording is already active.", {}
            s.recording, s.paused = True, False
            return True, "", {}
        if kind == "StopRecord":
            if not s.recording:
                return False, "Recording is not active.", {}
            s.recording = s.paused = False
            path = s.recordings_dir / f"2026-09-27 12-00-{len(s.saved):02d}.mkv"
            path.write_bytes(b"\x1aE\xdf\xa3")
            s.saved.append(str(path))
            return True, "", {"outputPath": str(path)}
        if kind in ("PauseRecord", "ResumeRecord"):
            if not s.recording:
                return False, "Recording is not active.", {}
            s.paused = kind == "PauseRecord"
            return True, "", {}
        if kind == "GetSceneList":
            scenes = [{"sceneName": name, "sceneIndex": i} for i, name in enumerate(reversed(s.scenes))]
            return True, "", {"currentProgramSceneName": s.scene, "scenes": scenes}
        if kind == "SetCurrentProgramScene":
            if data.get("sceneName") not in s.scenes:
                return False, "No source was found by the name of the scene.", {}
            s.scene = data["sceneName"]
            return True, "", {}
        if kind == "GetStreamStatus":
            return True, "", {"outputActive": s.streaming, "outputTimecode": "00:00:00.000"}
        if kind in ("StartStream", "StopStream"):
            if kind == "StartStream" and not s.stream_configured:
                return False, "No stream service is configured.", {}
            s.streaming = kind == "StartStream"
            return True, "", {}
        if kind == "GetVirtualCamStatus":
            return True, "", {"outputActive": s.virtualcam}
        if kind in ("StartVirtualCam", "StopVirtualCam"):
            s.virtualcam = kind == "StartVirtualCam"
            return True, "", {}
        if kind == "GetReplayBufferStatus":
            return True, "", {"outputActive": s.replay_buffer}
        if kind in ("StartReplayBuffer", "StopReplayBuffer"):
            s.replay_buffer = kind == "StartReplayBuffer"
            return True, "", {}
        if kind == "SaveReplayBuffer":
            if not s.replay_buffer:
                return False, "Replay buffer is not active.", {}
            path = s.recordings_dir / f"Replay 2026-09-27 12-00-{len(s.saved):02d}.mkv"
            path.write_bytes(b"\x1aE\xdf\xa3")
            s.saved.append(str(path))
            return True, "", {}
        if kind == "GetLastReplayBufferReplay":
            replays = [p for p in s.saved if Path(p).name.startswith("Replay")]
            return (
                (True, "", {"savedReplayPath": replays[-1]}) if replays else (False, "No replay saved.", {})
            )
        if kind == "GetInputList":
            return (
                True,
                "",
                {"inputs": [{"inputName": name, "inputKind": "pulse_input_capture"} for name in s.inputs]},
            )
        if kind == "GetInputMute":
            if data.get("inputName") not in s.inputs:
                return False, "No source was found.", {}
            return True, "", {"inputMuted": s.inputs[data["inputName"]]}
        if kind == "SetInputMute":
            if data.get("inputName") not in s.inputs:
                return False, "No source was found.", {}
            s.inputs[data["inputName"]] = bool(data.get("inputMuted"))
            return True, "", {}
        return False, f"Your request type is not valid: {kind}", {}
