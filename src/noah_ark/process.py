"""One supervised service: spawn, stream logs, health-check, restart, stop."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from noah_ark.config import ReadyCheck, Service
from noah_ark.logs import LogSink

READ_LIMIT = 1024 * 1024  # longest single log line we keep (bytes)
HEALTHY_UPTIME = 30.0  # running this long resets the restart backoff
MAX_BACKOFF = 30.0
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


class Status(str, Enum):
    PENDING = "pending"
    WAITING = "waiting"  # waiting for dependencies to become ready
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"  # crashed, restart scheduled
    STOPPING = "stopping"
    STOPPED = "stopped"
    EXITED = "exited"  # finished cleanly and not restarted
    CRASHED = "crashed"  # failed and gave up


def describe_exit(code: int) -> str:
    if code < 0:
        try:
            return f"killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"killed by signal {-code}"
    return f"exited with code {code}"


class ManagedProcess:
    def __init__(
        self,
        service: Service,
        sink: LogSink,
        on_change: Callable[[], None] = lambda: None,
    ):
        self.service = service
        self.name = service.name
        self.sink = sink
        self._on_change = on_change
        self.status = Status.PENDING
        self.pid: int | None = None
        self.restarts = 0
        self.exit_code: int | None = None
        self.started_at: float | None = None
        self.ready = asyncio.Event()  # set once healthy, or once it has given up
        self._proc: asyncio.subprocess.Process | None = None
        self._pumps: list[asyncio.Task[None]] = []
        self._stop_requested = asyncio.Event()
        self._restart_requested = False  # the next exit is ours: respawn right away
        self._kick = asyncio.Event()  # cuts a backoff sleep short

    # ---------------------------------------------------------------- status
    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "pid": self.pid,
            "restarts": self.restarts,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "cmd": self.service.cmd,
            "cwd": str(self.service.cwd),
        }

    def _set(self, status: Status) -> None:
        if status is not self.status:
            self.status = status
            self._on_change()

    # ----------------------------------------------------------------- spawn
    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"  # Python services: logs show up immediately
        env.update(self.service.env)
        return env

    def _resolve(self, env: dict[str, str]) -> list[str]:
        exe = self.service.cmd[0]
        if os.sep in exe:  # explicit path, relative to the service's cwd
            return self.service.cmd
        found = shutil.which(exe, path=env.get("PATH"))
        if found is None:
            raise FileNotFoundError(f"command not found: {exe!r} (is it installed? set `venv`?)")
        return [found, *self.service.cmd[1:]]

    async def _spawn(self) -> asyncio.subprocess.Process:
        env = self._build_env()
        argv = self._resolve(env)
        self._set(Status.STARTING)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.service.cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=READ_LIMIT,
            # Own process group: Ctrl+C in the terminal reaches only noah-ark, which then
            # stops services in order; and we can signal a service's whole process tree.
            start_new_session=True,
        )
        self._proc = proc
        self.pid = proc.pid
        self.started_at = time.time()
        self.exit_code = None
        assert proc.stdout is not None and proc.stderr is not None
        self._pumps = [
            asyncio.create_task(self._pump(proc.stdout, "out")),
            asyncio.create_task(self._pump(proc.stderr, "err")),
        ]
        self.sink.event(self.name, f"started (pid {proc.pid})")
        self._on_change()
        return proc

    async def _pump(self, reader: asyncio.StreamReader, stream: str) -> None:
        while True:
            try:
                raw = await reader.readline()
            except ValueError:  # a single line longer than READ_LIMIT
                self.sink.event(self.name, f"dropped a log line longer than {READ_LIMIT} bytes")
                continue
            if not raw:
                return
            self.sink.line(self.name, raw.decode("utf-8", "replace").rstrip("\r\n"), stream)

    async def _wait_ready(self) -> None:
        check = self.service.ready
        if check is None:
            self._set(Status.RUNNING)
            self.ready.set()
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + check.timeout
        last = f"port {check.port} not open"
        while loop.time() < deadline:
            ok, detail = await self._probe(check)
            if ok:
                self._set(Status.RUNNING)
                self.sink.event(self.name, f"ready {detail}")
                self.ready.set()
                return
            last = detail or last
            await asyncio.sleep(0.25)
        self._set(Status.RUNNING)
        self.sink.event(
            self.name, f"not ready after {check.timeout:g}s ({last}), continuing anyway"
        )
        self.ready.set()

    @staticmethod
    async def _probe(check: ReadyCheck) -> tuple[bool, str]:
        """One readiness attempt. Returns (ok, detail for the log)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(check.host, check.port), timeout=1.0
            )
        except (OSError, asyncio.TimeoutError):
            return False, ""
        try:
            if check.http is None:
                return True, f"on {check.host}:{check.port}"
            request = (
                f"GET {check.http} HTTP/1.0\r\nHost: {check.host}:{check.port}\r\n"
                "User-Agent: noah-ark\r\nConnection: close\r\n\r\n"
            )
            writer.write(request.encode("latin-1"))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            parts = line.decode("latin-1").split()
            if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
                return False, f"GET {check.http}: not an HTTP response"
            code = int(parts[1])
            return check.accepts(code), f"(GET {check.http} → {code})"
        except (OSError, asyncio.TimeoutError) as exc:
            return False, f"GET {check.http}: {exc.__class__.__name__}"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # ------------------------------------------------------------- lifecycle
    async def _wait_or_stop(self, aw: Awaitable[Any], *, kickable: bool = False) -> bool:
        """Await `aw` unless a stop (or, if kickable, a kick) comes first.

        True if `aw` finished."""
        work = asyncio.ensure_future(aw)
        waiters = {work, asyncio.create_task(self._stop_requested.wait())}
        if kickable:
            waiters.add(asyncio.create_task(self._kick.wait()))
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                if not task.done():
                    task.cancel()
        return work.done() and not work.cancelled()

    def _should_restart(self, code: int) -> bool:
        policy = self.service.restart
        return policy == "always" or (policy == "on-failure" and code != 0)

    async def run(self, deps: list[ManagedProcess]) -> None:
        """Supervise this service until it is stopped or gives up."""
        if deps:
            self._set(Status.WAITING)
            self.sink.event(self.name, f"waiting for {', '.join(d.name for d in deps)}")
            if not await self._wait_or_stop(asyncio.gather(*(d.ready.wait() for d in deps))):
                self._set(Status.STOPPED)
                return
            failed = [d.name for d in deps if d.status is Status.CRASHED]
            if failed:
                names = ", ".join(failed)
                self.sink.event(self.name, f"dependency failed ({names}); starting anyway")

        attempt = 0
        while not self._stop_requested.is_set():
            self._kick.clear()
            try:
                proc = await self._spawn()
            except OSError as exc:
                self.sink.event(self.name, f"failed to start: {exc}")
                self._set(Status.CRASHED)
                self.ready.set()
                return
            if self._stop_requested.is_set():  # stop() raced with the spawn
                await self._terminate(proc)

            started = time.monotonic()
            ready_task = asyncio.create_task(self._wait_ready())
            code = await proc.wait()
            ready_task.cancel()
            await asyncio.gather(*self._pumps, return_exceptions=True)
            self._reap_group(proc)
            self.exit_code, self.pid = code, None

            if self._stop_requested.is_set():
                break
            if self._restart_requested:  # we stopped it on purpose: no backoff, no count
                self._restart_requested = False
                attempt = 0
                continue
            if time.monotonic() - started >= HEALTHY_UPTIME:
                attempt = 0
            reason = describe_exit(code)
            if not self._should_restart(code):
                self.sink.event(self.name, reason)
                self._set(Status.EXITED if code == 0 else Status.CRASHED)
                self.ready.set()
                return
            if self.restarts >= self.service.max_restarts:
                self.sink.event(self.name, f"{reason}; giving up after {self.restarts} restarts")
                self._set(Status.CRASHED)
                self.ready.set()
                return

            self.restarts += 1
            attempt += 1
            delay = min(MAX_BACKOFF, 0.5 * 2 ** (attempt - 1))
            self.sink.event(
                self.name, f"{reason}; restarting in {delay:.1f}s (restart #{self.restarts})"
            )
            self._set(Status.BACKOFF)
            if not await self._wait_or_stop(asyncio.sleep(delay), kickable=True):
                if self._stop_requested.is_set():
                    break
                attempt = 0  # kicked by restart(): respawn now

        self._set(Status.STOPPED)
        self.sink.event(self.name, "stopped")

    async def restart(self, reason: str = "requested") -> None:
        """Stop the running process (whole group) and start it again immediately.

        Does not count toward max_restarts. In backoff, skips the remaining delay."""
        if self._stop_requested.is_set():
            return
        self.sink.event(self.name, f"restarting ({reason})")
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self._restart_requested = True
            self._set(Status.STOPPING)
            await self._terminate(proc)
        else:
            self._kick.set()

    def reset(self) -> None:
        """Make a finished service (stopped, exited, crashed) runnable again."""
        self._stop_requested.clear()
        self._restart_requested = False
        self._kick.clear()
        self.restarts = 0

    def request_stop(self) -> None:
        """No more restarts; anything waiting or backing off exits right away."""
        self._stop_requested.set()

    async def stop(self) -> None:
        self._stop_requested.set()
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        self._set(Status.STOPPING)
        self.sink.event(self.name, "stopping")
        await self._terminate(proc)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        self._signal(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.service.stop_timeout)
        except asyncio.TimeoutError:
            self.sink.event(
                self.name, f"still running after {self.service.stop_timeout:g}s, sending SIGKILL"
            )
            self._signal(proc, SIGKILL)
            await proc.wait()

    def kill(self) -> None:
        self._stop_requested.set()
        if self._proc is not None and self._proc.returncode is None:
            self._signal(self._proc, SIGKILL)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)  # whole tree: reloaders, workers, shell children

    @classmethod
    def _reap_group(cls, proc: asyncio.subprocess.Process) -> None:
        """The main process is gone; kill anything it left behind in its group."""
        cls._signal(proc, SIGKILL)
