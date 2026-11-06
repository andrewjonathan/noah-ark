import textwrap

import pytest

from noah_ark.config import ConfigError, load_project


def write(tmp_path, text):
    path = tmp_path / "ark.yaml"
    path.write_text(textwrap.dedent(text))
    return path


def test_parses_services_and_start_order(tmp_path):
    (tmp_path / "a").mkdir()
    path = write(tmp_path, """
        services:
          api:
            cwd: a
            cmd: uvicorn main:app --port 8001
            ready: 8001
          worker: python3 worker.py
          web:
            cmd: ["node", "server.js"]
            depends_on: [api]
            restart: no
    """)
    project = load_project(path)
    assert project.services["api"].cmd == ["uvicorn", "main:app", "--port", "8001"]
    assert project.services["api"].ready.port == 8001
    assert project.services["api"].cwd == (tmp_path / "a").resolve()
    assert project.services["worker"].cmd == ["python3", "worker.py"]
    assert project.services["web"].restart == "no"
    assert project.levels() == [["api", "worker"], ["web"]]


def test_select_pulls_in_dependencies(tmp_path):
    path = write(tmp_path, """
        services:
          db: sleep 1
          api: {cmd: sleep 1, depends_on: [db]}
          web: {cmd: sleep 1, depends_on: [api]}
          other: sleep 1
    """)
    assert load_project(path).select(["web"]) == ["db", "api", "web"]


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "abc123")
    path = write(tmp_path, """
        env: {SHARED: one}
        services:
          api:
            cmd: sleep 1
            env: {TOKEN: "${SECRET_TOKEN}"}
    """)
    env = load_project(path).services["api"].env
    assert env == {"SHARED": "one", "TOKEN": "abc123"}


@pytest.mark.parametrize("body, message", [
    ("a: {cmd: x, depends_on: [b]}\n  b: {cmd: x, depends_on: [a]}", "cycle"),
    ("a: {cmd: x, depends_on: [ghost]}", "unknown service"),
    ("a: {comand: x}", "unknown key"),
    ("a: {cmd: x, restart: sometimes}", "restart must be"),
    ("a: {cmd: x, ready: 99999}", "between 1 and 65535"),
    ("ark: {cmd: x}", "reserved"),
    ("a: {cmd: x, cwd: nope}", "cwd does not exist"),
    ("a: {cmd: x, ready: {port: 80, http: health}}", "starting with '/'"),
    ("a: {cmd: x, ready: {port: 80, status: 200}}", "needs ready.http"),
    ("a: {cmd: x, ready: {port: 80, http: /h, status: 42}}", "HTTP status code"),
    ("a: {cmd: x, ready: 'https://localhost:80/h'}", "ready URL must look like"),
    ("a: {cmd: x, ready: 'http://localhost/h'}", "ready URL must look like"),
])
def test_rejects_invalid_config(tmp_path, body, message):
    path = tmp_path / "ark.yaml"
    path.write_text("services:\n  " + body + "\n")
    with pytest.raises(ConfigError, match=message):
        load_project(path)


def test_http_ready_check(tmp_path):
    path = write(tmp_path, """
        services:
          a:
            cmd: sleep 1
            ready: {port: 8001, http: /health, status: 204, timeout: 5}
          b:
            cmd: sleep 1
            ready: http://localhost:8002/ready?deep=1
          c:
            cmd: sleep 1
            ready: 8003
    """)
    services = load_project(path).services
    a, b, c = services["a"].ready, services["b"].ready, services["c"].ready
    assert (a.port, a.http, a.status, a.timeout) == (8001, "/health", 204, 5.0)
    assert a.accepts(204) and not a.accepts(200)
    assert (b.host, b.port, b.http, b.status) == ("localhost", 8002, "/ready?deep=1", None)
    assert b.accepts(200) and b.accepts(302) and not b.accepts(503)
    assert c.http is None


def test_profiles_selection(tmp_path):
    path = write(tmp_path, """
        services:
          db: sleep 1
          api: {cmd: sleep 1, depends_on: [db]}
          jobs: {cmd: sleep 1, profiles: [workers], depends_on: [db]}
          admin: {cmd: sleep 1, profiles: [tools, workers]}
    """)
    project = load_project(path)
    assert project.select() == ["db", "api"]
    assert project.select(profiles=["workers"]) == ["db", "api", "jobs", "admin"]
    assert project.select(profiles=["tools"]) == ["db", "api", "admin"]
    assert project.select(["jobs"]) == ["db", "jobs"]  # named: profiles don't matter
    with pytest.raises(ConfigError, match="unknown profile"):
        project.select(profiles=["nope"])
