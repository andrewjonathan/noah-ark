"""Control socket: lets `noah restart|stop|start` talk to a running supervisor.

Protocol: the client sends one JSON line {"cmd": "restart", "services": ["api"]} and
reads one JSON line back {"ok": true, "message": "api: restarted"}.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noah_ark.config import Project
    from noah_ark.supervisor import Supervisor

COMMANDS = ("restart", "stop", "start")
MAX_SOCKET_PATH = 100  # sockaddr_un.sun_path is 104 bytes on macOS, 108 on Linux


def socket_path(project: Project) -> Path:
    """`.ark/noah.sock`, or a short path in the temp dir if that would be too long."""
    path = project.state_dir / "noah.sock"
    if len(os.fsencode(path)) <= MAX_SOCKET_PATH:
        return path
    digest = hashlib.sha1(os.fsencode(project.root)).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"noah-{os.getuid()}-{digest}.sock"


class ControlServer:
    def __init__(self, supervisor: Supervisor, path: Path):
        self.supervisor = supervisor
        self.path = path
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()  # stale socket from a run that was killed
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            reply = await self.dispatch(json.loads(line))
        except Exception as exc:  # never let a bad client hurt the supervisor
            reply = {"ok": False, "message": f"bad request: {exc}"}
        try:
            writer.write(json.dumps(reply).encode() + b"\n")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def dispatch(self, request: Any) -> dict[str, Any]:
        from noah_ark.supervisor import ControlError

        if not isinstance(request, dict) or request.get("cmd") not in COMMANDS:
            return {"ok": False, "message": f"unknown command (expected {', '.join(COMMANDS)})"}
        cmd: str = request["cmd"]
        names = [str(n) for n in request.get("services") or []]
        if not names:
            if cmd != "restart":
                return {"ok": False, "message": f"`{cmd}` needs at least one service name"}
            names = list(self.supervisor.procs)
        action = getattr(self.supervisor, cmd)

        async def one(name: str) -> tuple[bool, str]:
            try:
                return True, await action(name)
            except ControlError as exc:
                return False, str(exc)

        results = await asyncio.gather(*(one(n) for n in names))
        return {
            "ok": all(ok for ok, _ in results),
            "message": "\n".join(msg for _, msg in results),
        }


def send(path: Path, cmd: str, services: list[str], timeout: float = 120.0) -> dict[str, Any]:
    """Client side: send one request, return the decoded reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps({"cmd": cmd, "services": services}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    reply = json.loads(data)
    if not isinstance(reply, dict):
        raise ValueError("unexpected reply")
    return reply
