"""Supervisor.start/stop/restart: per-service control inside a running supervisor."""

import asyncio
import sys

import pytest

from noah_ark.config import load_project
from noah_ark.process import Status
from noah_ark.supervisor import ControlError, Supervisor

PY = sys.executable
SLEEPER = f'["{PY}", "-c", "import time; print(1, flush=True); time.sleep(60)"]'


def make_project(tmp_path, services_yaml):
    (tmp_path / "ark.yaml").write_text("services:\n" + services_yaml)
    return load_project(tmp_path / "ark.yaml")


async def until(predicate, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


def run(project, scenario):
    """Run a supervisor and `scenario(sup)` side by side; then shut down."""

    async def main():
        sup = Supervisor(project, console=False)
        task = asyncio.create_task(sup._main())
        await until(lambda: len(sup.procs) == len(sup.names))
        try:
            result = await scenario(sup)
        finally:
            if not task.done():
                sup._on_signal()
        code = await asyncio.wait_for(task, 15)
        return result, code

    return asyncio.run(main())


def status(sup, name):
    return sup.procs[name].status


def test_restart_running_service(tmp_path):
    project = make_project(tmp_path, f"""
  a: {{cmd: {SLEEPER}}}
  b: {{cmd: {SLEEPER}}}
""")

    async def scenario(sup):
        await until(lambda: status(sup, "a") is Status.RUNNING and sup.procs["b"].pid)
        a_pid, b_pid = sup.procs["a"].pid, sup.procs["b"].pid
        assert await sup.restart("a") == "a: restarted"
        await until(lambda: status(sup, "a") is Status.RUNNING and sup.procs["a"].pid)
        return a_pid, sup.procs["a"].pid, b_pid, sup.procs["b"].pid, sup.procs["a"].restarts

    (old_a, new_a, old_b, new_b, restarts), code = run(project, scenario)
    assert old_a != new_a
    assert old_b == new_b  # untouched
    assert restarts == 0  # a requested restart is not a crash
    assert code == 0


def test_restart_skips_backoff(tmp_path):
    project = make_project(tmp_path, f"""
  flaky: {{cmd: ["{PY}", "-c", "raise SystemExit(1)"], max_restarts: 50}}
""")

    async def scenario(sup):
        proc = sup.procs["flaky"]
        await until(lambda: proc.restarts >= 3)  # next backoff is 0.5 * 2**3 = 4s
        await until(lambda: proc.status is Status.BACKOFF)
        before = proc.restarts
        await sup.restart("flaky")
        await until(lambda: proc.restarts > before, timeout=2)  # well under 4s
        return True

    assert run(project, scenario)[0]


def test_stop_one_then_start_again(tmp_path):
    project = make_project(tmp_path, f"""
  a: {{cmd: {SLEEPER}}}
  b: {{cmd: {SLEEPER}}}
""")

    async def scenario(sup):
        await until(lambda: status(sup, "a") is Status.RUNNING)
        await until(lambda: status(sup, "b") is Status.RUNNING)
        assert await sup.stop("a") == "a: stopped"
        assert status(sup, "a") is Status.STOPPED
        assert status(sup, "b") is Status.RUNNING
        assert await sup.stop("a") == "a: not running (stopped)"
        assert await sup.start("a") == "a: started"
        await until(lambda: status(sup, "a") is Status.RUNNING)
        assert await sup.start("a") == "a: already running"
        with pytest.raises(ControlError, match="not part of this run"):
            await sup.restart("ghost")
        return True

    assert run(project, scenario) == (True, 0)


def test_restart_brings_back_crashed_service(tmp_path):
    marker = tmp_path / "fixed"
    code = (
        f"import os, sys, time\nif not os.path.exists({str(marker)!r}): sys.exit(2)\n"
        "time.sleep(60)"
    )
    project = make_project(tmp_path, f"""
  svc: {{cmd: ["{PY}", "-c", {code!r}], max_restarts: 0}}
  other: {{cmd: {SLEEPER}}}
""")

    async def scenario(sup):
        await until(lambda: status(sup, "svc") is Status.CRASHED)
        marker.write_text("")
        assert await sup.restart("svc", "fixed") == "svc: started"
        await until(lambda: status(sup, "svc") is Status.RUNNING)
        return True

    assert run(project, scenario)[0]


def test_supervisor_exits_when_last_service_stopped(tmp_path):
    project = make_project(tmp_path, f"  a: {{cmd: {SLEEPER}}}\n")

    async def main():
        sup = Supervisor(project, console=False)
        task = asyncio.create_task(sup._main())
        await until(lambda: "a" in sup.procs and status(sup, "a") is Status.RUNNING)
        await sup.stop("a")
        return await asyncio.wait_for(task, 5)

    assert asyncio.run(main()) == 0
