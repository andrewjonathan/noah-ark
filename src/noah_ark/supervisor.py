"""Runs a project: start services in dependency order, stop them in reverse."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Iterable

from noah_ark import __version__
from noah_ark.config import Project
from noah_ark.logs import ARK, LogSink
from noah_ark.process import ManagedProcess, Status
from noah_ark.state import StateStore


class ControlError(Exception):
    """A per-service control request that cannot be carried out."""


class Supervisor:
    def __init__(
        self,
        project: Project,
        names: Iterable[str] | None = None,
        *,
        profiles: Iterable[str] = (),
        console: bool = True,
        color: bool | None = None,
        timestamps: bool = False,
    ):
        self.project = project
        self.levels = project.levels(project.select(names, profiles))
        self.names = [n for level in self.levels for n in level]
        self.console = console
        self.color = color
        self.timestamps = timestamps
        self.store = StateStore(project.state_dir)
        self.procs: dict[str, ManagedProcess] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._started_at = time.time()
        self._forced = False

    def run(self) -> int:
        """Blocking entry point. Returns 0, or 1 if any service crashed."""
        return asyncio.run(self._main())

    async def _main(self) -> int:
        self.store.ensure()
        self.sink = LogSink(
            self.names,
            self.project.log_dir,
            console=self.console,
            color=self.color,
            timestamps=self.timestamps,
        )
        self._started_at = time.time()
        self.procs = {
            n: ManagedProcess(self.project.services[n], self.sink, self._save_state)
            for n in self.names
        }
        self._shutdown = asyncio.Event()
        self._idle = asyncio.Event()  # set when no service task is left running
        self._install_signal_handlers()
        self.store.write_pid(os.getpid())
        self.sink.event(
            ARK,
            f"noah-ark {__version__} · '{self.project.name}' · "
            f"{len(self.names)} service(s) · Ctrl+C to stop",
        )
        try:
            for name in self.names:
                self._launch(name)
            shutdown = asyncio.create_task(self._shutdown.wait())
            idle = asyncio.create_task(self._idle.wait())
            await asyncio.wait({idle, shutdown}, return_when=asyncio.FIRST_COMPLETED)
            if self._shutdown.is_set():
                await self._stop_all()
            else:
                self.sink.event(ARK, "all services have exited")
            await asyncio.gather(*self.tasks.values())
            shutdown.cancel()
            idle.cancel()
        finally:
            self._save_state()
            self.store.clear_pid()
            self.sink.close()
        return 1 if any(p.status is Status.CRASHED for p in self.procs.values()) else 0

    def _launch(self, name: str, *, wait_for_deps: bool = True) -> None:
        task = asyncio.create_task(self._guarded(name, wait_for_deps))
        self.tasks[name] = task
        self._idle.clear()
        task.add_done_callback(self._task_done)

    def _task_done(self, _task: asyncio.Task[None]) -> None:
        if all(t.done() for t in self.tasks.values()):
            self._idle.set()

    async def _guarded(self, name: str, wait_for_deps: bool = True) -> None:
        proc = self.procs[name]
        deps = (
            [self.procs[d] for d in self.project.services[name].depends_on]
            if wait_for_deps else []
        )
        try:
            await proc.run(deps)
        except Exception as exc:  # a bug in noah-ark must not take down other services
            self.sink.event(name, f"internal error: {exc!r}")
            proc.status = Status.CRASHED
            proc.ready.set()

    # ------------------------------------------------- per-service control
    # Used by the control socket (`noah restart|stop|start`) and file watchers.
    # Each returns a one-line message for the user; ControlError means "refused".

    def _proc(self, name: str) -> ManagedProcess:
        if name not in self.procs:
            raise ControlError(
                f"'{name}' is not part of this run (running: {', '.join(self.procs)})"
            )
        if self._shutdown.is_set():
            raise ControlError("noah-ark is shutting down")
        return self.procs[name]

    def _running(self, name: str) -> bool:
        task = self.tasks.get(name)
        return task is not None and not task.done()

    async def start(self, name: str) -> str:
        proc = self._proc(name)
        if self._running(name):
            return f"{name}: already {proc.status.value}"
        proc.reset()
        self._launch(name, wait_for_deps=False)
        return f"{name}: started"

    async def stop(self, name: str) -> str:
        proc = self._proc(name)
        if not self._running(name):
            return f"{name}: not running ({proc.status.value})"
        await proc.stop()
        await self.tasks[name]
        return f"{name}: stopped"

    async def restart(self, name: str, reason: str = "requested") -> str:
        proc = self._proc(name)
        if not self._running(name):
            return await self.start(name)
        if proc.status is Status.WAITING:
            return f"{name}: still waiting for its dependencies, not restarted"
        await proc.restart(reason)
        return f"{name}: restarted"

    async def _stop_all(self) -> None:
        self.sink.event(ARK, "shutting down (press Ctrl+C again to force)")
        for proc in self.procs.values():  # nothing restarts once shutdown begins
            proc.request_stop()
        for level in reversed(self.levels):  # dependents first, dependencies last
            await asyncio.gather(*(self.procs[n].stop() for n in level))
        self.sink.event(ARK, "all services stopped")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(sig, self._on_signal)

    def _on_signal(self) -> None:
        if not self._shutdown.is_set():
            self._shutdown.set()
        elif not self._forced:
            self._forced = True
            self.sink.event(ARK, "forcing shutdown: SIGKILL to every service")
            for proc in self.procs.values():
                proc.kill()

    def _save_state(self) -> None:
        self.store.write_state(
            {
                "project": self.project.name,
                "supervisor_pid": os.getpid(),
                "started_at": self._started_at,
                "updated_at": time.time(),
                "services": {n: p.snapshot() for n, p in self.procs.items()},
            }
        )
