"""`noah restart|stop|start` over the control socket, against a live supervisor."""

import asyncio
import json
import stat

from test_control_methods import SLEEPER, make_project, run, status, until

from noah_ark.cli import main
from noah_ark.control import MAX_SOCKET_PATH, socket_path
from noah_ark.process import Status


def test_cli_restart_stop_start(tmp_path, capsys):
    project = make_project(tmp_path, f"""
  a: {{cmd: {SLEEPER}}}
  b: {{cmd: {SLEEPER}}}
""")
    cfg = str(tmp_path / "ark.yaml")

    def cli(*argv):
        return asyncio.to_thread(main, [*argv, "-f", cfg])

    async def scenario(sup):
        await until(lambda: status(sup, "a") is Status.RUNNING)
        await until(lambda: status(sup, "b") is Status.RUNNING)
        sock = socket_path(project)
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
        state = json.loads((tmp_path / ".ark/state.json").read_text())
        assert state["control"] == str(sock)

        old = sup.procs["a"].pid
        assert await cli("restart", "a") == 0
        await until(lambda: status(sup, "a") is Status.RUNNING and sup.procs["a"].pid)
        assert sup.procs["a"].pid != old

        assert await cli("stop", "b") == 0
        assert status(sup, "b") is Status.STOPPED
        assert status(sup, "a") is Status.RUNNING
        assert await cli("start", "b") == 0
        await until(lambda: status(sup, "b") is Status.RUNNING)

        assert await cli("restart") == 0  # everything
        return capsys.readouterr()

    out, code = run(project, scenario)
    assert code == 0
    assert "a: restarted" in out.out
    assert "b: stopped" in out.out and "b: started" in out.out
    assert "a: restarted\nb: restarted" in out.out
    assert not socket_path(project).exists()  # cleaned up on exit


def test_cli_control_when_not_running(tmp_path, capsys):
    make_project(tmp_path, f"  a: {{cmd: {SLEEPER}}}\n")
    cfg = str(tmp_path / "ark.yaml")
    assert main(["restart", "a", "-f", cfg]) == 1
    assert "not running" in capsys.readouterr().err
    assert main(["stop", "ghost", "-f", cfg]) == 2
    assert "unknown service" in capsys.readouterr().err


def test_long_project_path_uses_short_socket(tmp_path):
    deep = tmp_path / ("x" * 60) / ("y" * 60)
    deep.mkdir(parents=True)
    project = make_project(deep, f"  a: {{cmd: {SLEEPER}}}\n")
    path = socket_path(project)
    assert len(str(path)) <= MAX_SOCKET_PATH
    assert path != project.state_dir / "noah.sock"
