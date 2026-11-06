import asyncio
import os
import sys
import time

import pytest
from test_control_methods import SLEEPER, make_project, run, status, until

from noah_ark.config import ConfigError, load_project
from noah_ark.process import Status
from noah_ark.watch import DEFAULT_IGNORE, compile_glob, describe, diff, snapshot

PY = sys.executable


@pytest.mark.parametrize("pattern, path, ok", [
    ("*.py", "main.py", True),
    ("*.py", "a/main.py", False),  # (name-only matching is done by the caller)
    ("app/*.py", "app/x.py", True),
    ("app/*.py", "app/sub/x.py", False),
    ("app/**/*.py", "app/x.py", True),
    ("app/**/*.py", "app/a/b/x.py", True),
    ("**/*.html", "t/x.html", True),
    ("templates/**", "templates/a/b.txt", True),
    ("?.txt", "a.txt", True),
    ("?.txt", "ab.txt", False),
    ("a.py", "aXpy", False),  # '.' is literal
])
def test_compile_glob(pattern, path, ok):
    assert bool(compile_glob(pattern).match(path)) is ok


def make_tree(root, files):
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)


def test_snapshot_include_ignore_and_prune(tmp_path):
    make_tree(tmp_path, [
        "main.py", "pkg/util.py", "pkg/data.json", "README.md",
        ".venv/lib/site.py", "node_modules/x/index.py", "pkg/__pycache__/util.cpython.pyc",
        "build/gen.py", "templates/index.html",
    ])
    snap = snapshot(tmp_path, ["*.py", "templates/**"], [*DEFAULT_IGNORE, "build"])
    assert sorted(snap) == ["main.py", "pkg/util.py", "templates/index.html"]


def test_diff_and_describe(tmp_path):
    make_tree(tmp_path, ["a.py", "b.py"])
    before = snapshot(tmp_path, ["*.py"], DEFAULT_IGNORE)
    st = os.stat(tmp_path / "a.py")
    os.utime(tmp_path / "a.py", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    (tmp_path / "b.py").unlink()
    (tmp_path / "c.py").write_text("")
    changed = diff(before, snapshot(tmp_path, ["*.py"], DEFAULT_IGNORE))
    assert changed == ["a.py", "b.py", "c.py"]
    assert describe(changed) == "changed: a.py (+2 more)"
    assert describe(["x.py"]) == "changed: x.py"


def test_watch_config(tmp_path):
    (tmp_path / "ark.yaml").write_text(
        "services:\n"
        "  a: {cmd: x, watch: '*.py'}\n"
        "  b: {cmd: x, watch: {paths: ['*.py', 'templates/**'], ignore: [build],"
        " interval: 0.5, debounce: 0}}\n"
        "  c: {cmd: x}\n"
    )
    s = load_project(tmp_path / "ark.yaml").services
    assert s["a"].watch.paths == ["*.py"] and s["a"].watch.interval == 1.0
    assert s["b"].watch.ignore == ["build"] and s["b"].watch.debounce == 0
    assert s["c"].watch is None


@pytest.mark.parametrize("watch, message", [
    ("{paths: []}", "must not be empty"),
    ("{paths: '*.py', interval: 0.01}", "at least 0.1"),
    ("'../shared/*.py'", "inside the service"),
    ("{path: '*.py'}", "unknown key"),
    ("42", "must be a pattern"),
])
def test_watch_config_errors(tmp_path, watch, message):
    (tmp_path / "ark.yaml").write_text(f"services:\n  a: {{cmd: x, watch: {watch}}}\n")
    with pytest.raises(ConfigError, match=message):
        load_project(tmp_path / "ark.yaml")


def touch_later(path):
    time.sleep(0.05)
    path.write_text(str(time.time()))


def test_file_change_restarts_service(tmp_path):
    (tmp_path / "conf.txt").write_text("1")
    project = make_project(tmp_path, f"""
  w: {{cmd: {SLEEPER}, watch: {{paths: "*.txt", interval: 0.1, debounce: 0.05}}}}
  other: {{cmd: {SLEEPER}}}
""")

    async def scenario(sup):
        await until(lambda: status(sup, "w") is Status.RUNNING and sup.procs["w"].pid)
        await until(lambda: status(sup, "other") is Status.RUNNING)
        log = tmp_path / ".ark/logs/w.log"
        await until(lambda: "watching 1 file(s)" in log.read_text())
        old, other = sup.procs["w"].pid, sup.procs["other"].pid
        await asyncio.to_thread(touch_later, tmp_path / "conf.txt")
        await until(lambda: sup.procs["w"].pid not in (None, old))

        # Stopped on request: a change must not bring it back...
        await sup.stop("w")
        await asyncio.to_thread(touch_later, tmp_path / "conf.txt")
        await asyncio.sleep(0.5)
        held = status(sup, "w")
        # ...until it is started again explicitly.
        await sup.start("w")
        await until(lambda: status(sup, "w") is Status.RUNNING)
        return log.read_text(), held, other == sup.procs["other"].pid

    (log, held, other_untouched), code = run(project, scenario)
    assert "restarting (changed: conf.txt)" in log
    assert held is Status.STOPPED
    assert other_untouched
    assert code == 0


def test_watch_reruns_finished_one_shot(tmp_path):
    # A one-shot task (restart: no) with watch is re-run on change, and the supervisor
    # keeps running after it exits instead of quitting.
    (tmp_path / "src.txt").write_text("1")
    project = make_project(tmp_path, f"""
  build: {{cmd: ["{PY}", "-c", "print('built')"], restart: "no",
           watch: {{paths: "*.txt", interval: 0.1, debounce: 0.05}}}}
""")

    async def scenario(sup):
        log = tmp_path / ".ark/logs/build.log"
        await until(lambda: status(sup, "build") is Status.EXITED)
        await until(lambda: "watching" in log.read_text())
        await asyncio.sleep(0.3)
        assert status(sup, "build") is Status.EXITED  # still alive, waiting
        await asyncio.to_thread(touch_later, tmp_path / "src.txt")
        await until(lambda: log.read_text().count(" out built") == 2)
        return True

    assert run(project, scenario) == (True, 0)
