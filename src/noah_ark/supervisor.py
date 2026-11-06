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
        self._install_signal_handlers()
        self.store.write_pid(os.getpid())
        self.sink.event(
            ARK,
            f"noah-ark {__version__} · '{self.project.name}' · "
            f"{len(self.names)} service(s) · Ctrl+C to stop",
        )
        try:
            tasks = [asyncio.create_task(self._guarded(n)) for n in self.names]
            everything = asyncio.gather(*tasks)
            shutdown = asyncio.create_task(self._shutdown.wait())
            await asyncio.wait({everything, shutdown}, return_when=asyncio.FIRST_COMPLETED)
            if self._shutdown.is_set():
                await self._stop_all()
            else:
                self.sink.event(ARK, "all services have exited")
            await everything
            shutdown.cancel()
        finally:
            self._save_state()
            self.store.clear_pid()
            self.sink.close()
        return 1 if any(p.status is Status.CRASHED for p in self.procs.values()) else 0

    async def _guarded(self, name: str) -> None:
        proc = self.procs[name]
        deps = [self.procs[d] for d in self.project.services[name].depends_on]
        try:
            await proc.run(deps)
        except Exception as exc:  # a bug in noah-ark must not take down other services
            self.sink.event(name, f"internal error: {exc!r}")
            proc.status = Status.CRASHED
            proc.ready.set()

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
