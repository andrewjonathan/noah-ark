import asyncio
import os
import signal
import subprocess
import sys
import threading

from noah_ark.config import Service, load_project
from noah_ark.logs import LogSink
from noah_ark.process import ManagedProcess, Status
from noah_ark.supervisor import Supervisor

PY = sys.executable


def make_project(tmp_path, services_yaml):
    (tmp_path / "ark.yaml").write_text("services:\n" + services_yaml)
    return load_project(tmp_path / "ark.yaml")


def test_runs_services_and_labels_logs(tmp_path, capsys):
    (tmp_path / "hello.py").write_text(
        "import sys\nprint('hello out')\nprint('hello err', file=sys.stderr)\n"
    )
    project = make_project(tmp_path, f"""
  one: {{cmd: ["{PY}", "hello.py"], restart: "no"}}
  two: {{cmd: ["{PY}", "hello.py"], restart: "no", depends_on: [one]}}
""")
    assert Supervisor(project, color=False).run() == 0
    out = capsys.readouterr().out
    assert "one | hello out" in out
    assert "two | hello err" in out
    log = (tmp_path / ".ark" / "logs" / "one.log").read_text()
    assert " out hello out" in log and " err hello err" in log
    assert not (tmp_path / ".ark" / "noah.pid").exists()


def test_restarts_then_gives_up(tmp_path):
    project = make_project(tmp_path, f"""
  flaky: {{cmd: ["{PY}", "-c", "raise SystemExit(3)"], max_restarts: 2}}
""")
    assert Supervisor(project, console=False).run() == 1
    log = (tmp_path / ".ark" / "logs" / "flaky.log").read_text()
    assert "restart #2" in log
    assert "giving up after 2 restarts" in log


def test_sigterm_stops_everything_in_order(tmp_path):
    sleeper = f'["{PY}", "-c", "import time; print(1, flush=True); time.sleep(60)"]'
    project = make_project(tmp_path, f"""
  base: {{cmd: {sleeper}}}
  top: {{cmd: {sleeper}, depends_on: [base]}}
""")
    timer = threading.Timer(1.5, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        assert Supervisor(project, console=False).run() == 0
    finally:
        timer.cancel()
    ark_log = (tmp_path / ".ark" / "logs" / "ark.log").read_text()
    assert ark_log.index("[top] stopping") < ark_log.index("[base] stopping")


def test_stop_kills_whole_process_group(tmp_path):
    # The service spawns a grandchild; stopping must take the grandchild down too.
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )

    async def scenario():
        sink = LogSink(["tree"], tmp_path / "logs", console=False)
        proc = ManagedProcess(Service(name="tree", cmd=[PY, "-c", code], cwd=tmp_path), sink)
        task = asyncio.create_task(proc.run([]))
        await asyncio.wait_for(proc.ready.wait(), 5)
        log = tmp_path / "logs" / "tree.log"
        for _ in range(50):
            lines = [ln for ln in log.read_text().splitlines() if " out " in ln]
            if lines:
                break
            await asyncio.sleep(0.1)
        grandchild = int(lines[0].split(" out ")[1])
        await proc.stop()
        await asyncio.wait_for(task, 5)
        sink.close()
        return proc.status, grandchild

    status, grandchild = asyncio.run(scenario())
    assert status is Status.STOPPED
    assert not running(grandchild)


def running(pid):
    """True if pid is a live process (zombies awaiting reaping count as dead)."""
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    state = out.stdout.strip()
    return bool(state) and not state.startswith("Z")


def test_no_restarts_during_shutdown(tmp_path):
    # 'base' dies the moment it gets SIGTERM'd... but only after 'top' (slow to stop)
    # has started stopping. With restart: always, it must still not come back.
    slow = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: (time.sleep(1), sys.exit(0)))\n"
        "print('up', flush=True)\n"
        "time.sleep(60)\n"
    )
    project = make_project(tmp_path, f"""
  base: {{cmd: ["{PY}", "-c", "import time; time.sleep(60)"], restart: always}}
  top: {{cmd: ["{PY}", "-c", {slow!r}], depends_on: [base]}}
""")
    timer = threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM))
    killer = threading.Timer(1.5, lambda: os.killpg(
        int(next(ln for ln in (tmp_path / ".ark/logs/base.log").read_text().splitlines()
                 if "started (pid" in ln).split("pid ")[1].rstrip(")")), signal.SIGKILL))
    timer.start()
    killer.start()
    try:
        Supervisor(project, console=False).run()
    finally:
        timer.cancel()
        killer.cancel()
    assert "restarting" not in (tmp_path / ".ark/logs/base.log").read_text()
