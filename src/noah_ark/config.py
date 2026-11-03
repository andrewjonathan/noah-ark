"""Loading and validating ``ark.yaml``."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAMES = ("ark.yaml", "ark.yml")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
RESERVED_NAMES = {"ark"}  # used for the supervisor's own log stream
RESTART_POLICIES = ("no", "on-failure", "always")
TOP_LEVEL_KEYS = {"version", "name", "env", "services"}
SERVICE_KEYS = {
    "cmd", "cwd", "env", "env_file", "venv", "shell",
    "restart", "max_restarts", "stop_timeout", "depends_on", "ready",
}


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass
class ReadyCheck:
    """A service is 'ready' once this TCP port accepts connections."""

    port: int
    host: str = "127.0.0.1"
    timeout: float = 30.0


@dataclass
class Service:
    name: str
    cmd: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    restart: str = "on-failure"
    max_restarts: int = 10
    stop_timeout: float = 10.0
    depends_on: list[str] = field(default_factory=list)
    ready: ReadyCheck | None = None


@dataclass
class Project:
    name: str
    root: Path
    config_path: Path
    services: dict[str, Service]

    @property
    def state_dir(self) -> Path:
        return self.root / ".ark"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    def select(self, names: Iterable[str] | None = None) -> list[str]:
        """The requested services plus everything they (transitively) depend on."""
        wanted = list(names or [])
        if not wanted:
            return list(self.services)
        unknown = [n for n in wanted if n not in self.services]
        if unknown:
            raise ConfigError(
                f"unknown service(s): {', '.join(unknown)} "
                f"(available: {', '.join(self.services)})"
            )
        selected: set[str] = set()

        def visit(name: str) -> None:
            if name not in selected:
                selected.add(name)
                for dep in self.services[name].depends_on:
                    visit(dep)

        for name in wanted:
            visit(name)
        return [n for n in self.services if n in selected]

    def levels(self, names: Iterable[str] | None = None) -> list[list[str]]:
        """Group services into start 'waves': each wave only depends on earlier waves."""
        chosen = list(names) if names is not None else list(self.services)
        remaining = {
            n: {d for d in self.services[n].depends_on if d in chosen} for n in chosen
        }
        result: list[list[str]] = []
        while remaining:
            wave = [n for n in chosen if n in remaining and not remaining[n]]
            if not wave:
                raise ConfigError(f"dependency cycle between: {', '.join(sorted(remaining))}")
            result.append(wave)
            for n in wave:
                del remaining[n]
            for deps in remaining.values():
                deps.difference_update(wave)
        return result


def find_config(path: str | os.PathLike[str] | None = None) -> Path:
    """Find ark.yaml: an explicit path, or the current directory and its parents."""
    if path:
        p = Path(path).expanduser()
        if p.is_dir():
            for name in CONFIG_FILENAMES:
                if (p / name).is_file():
                    return (p / name).resolve()
            raise ConfigError(f"no ark.yaml found in {p}")
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        return p.resolve()
    here = Path.cwd().resolve()
    for directory in (here, *here.parents):
        for name in CONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    raise ConfigError(
        "no ark.yaml found in this directory or any parent (run `noah init` to create one)"
    )


def load_project(path: str | os.PathLike[str] | None = None) -> Project:
    config_path = find_config(path)
    root = config_path.parent
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: the top level must be a mapping")
    _check_keys(data, TOP_LEVEL_KEYS, "top level")
    version = data.get("version", 1)
    if version != 1:
        raise ConfigError(f"unsupported config version {version!r} (expected 1)")

    raw_services = data.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ConfigError("`services` must be a non-empty mapping")
    global_env = _env_mapping(data.get("env"), "env")

    services: dict[str, Service] = {}
    for raw_name, raw in raw_services.items():
        name = str(raw_name)
        if not NAME_RE.match(name):
            raise ConfigError(f"invalid service name {name!r}: use letters, digits, '-', '_', '.'")
        if name in RESERVED_NAMES:
            raise ConfigError(f"service name {name!r} is reserved")
        services[name] = _build_service(name, raw, root, global_env)

    for svc in services.values():
        for dep in svc.depends_on:
            if dep == svc.name:
                raise ConfigError(f"service '{svc.name}' cannot depend on itself")
            if dep not in services:
                raise ConfigError(f"service '{svc.name}' depends on unknown service '{dep}'")

    project = Project(
        name=str(data.get("name") or root.name),
        root=root,
        config_path=config_path,
        services=services,
    )
    project.levels()  # fail early on dependency cycles
    return project


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple .env file (KEY=value, comments, optional quotes, `export`)."""
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def _check_keys(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(map(str, raw)) - allowed)
    if unknown:
        raise ConfigError(
            f"{where}: unknown key(s) {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _env_mapping(raw: Any, where: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping of NAME: value")
    return {str(k): os.path.expandvars("" if v is None else str(v)) for k, v in raw.items()}


def _number(raw: dict[str, Any], key: str, default: float, kind: type, where: str) -> Any:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{where}: `{key}` must be a non-negative number")
    return kind(value)


def _parse_ready(raw: Any, where: str) -> ReadyCheck | None:
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = {"port": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: `ready` must be a port number or {{port, host, timeout}}")
    _check_keys(raw, {"port", "host", "timeout"}, f"{where}: ready")
    port = raw.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"{where}: ready.port must be a number between 1 and 65535")
    timeout = _number(raw, "timeout", 30.0, float, f"{where}: ready")
    return ReadyCheck(port=port, host=str(raw.get("host", "127.0.0.1")), timeout=timeout)


def _build_service(name: str, raw: Any, root: Path, global_env: dict[str, str]) -> Service:
    where = f"service '{name}'"
    if isinstance(raw, str):  # shorthand:  worker: python3 worker.py
        raw = {"cmd": raw}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a mapping or a command string")
    _check_keys(raw, SERVICE_KEYS, where)

    cmd_raw = raw.get("cmd")
    if isinstance(cmd_raw, str) and cmd_raw.strip():
        cmd = shlex.split(cmd_raw)
    elif isinstance(cmd_raw, list) and cmd_raw:
        cmd = [str(part) for part in cmd_raw]
    else:
        raise ConfigError(f"{where}: `cmd` is required (a string or a list)")
    if raw.get("shell"):
        cmd = ["/bin/sh", "-c", cmd_raw if isinstance(cmd_raw, str) else shlex.join(cmd)]

    cwd = (root / str(raw.get("cwd", "."))).resolve()
    if not cwd.is_dir():
        raise ConfigError(f"{where}: cwd does not exist: {cwd}")

    env = dict(global_env)
    if raw.get("env_file"):
        env_path = (root / str(raw["env_file"])).resolve()
        if not env_path.is_file():
            raise ConfigError(f"{where}: env_file not found: {env_path}")
        env.update(read_env_file(env_path))
    env.update(_env_mapping(raw.get("env"), f"{where}: env"))

    if raw.get("venv"):
        venv = (cwd / str(raw["venv"])).resolve()
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        if not bin_dir.is_dir():
            raise ConfigError(f"{where}: virtualenv not found: {venv}")
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = os.pathsep.join([str(bin_dir), env.get("PATH", os.environ.get("PATH", ""))])

    restart_raw = raw.get("restart", "on-failure")
    restart = "no" if restart_raw is False else str(restart_raw)  # YAML parses `no` as false
    if restart not in RESTART_POLICIES:
        raise ConfigError(f"{where}: restart must be one of {', '.join(RESTART_POLICIES)}")

    depends_on = raw.get("depends_on") or []
    if isinstance(depends_on, str):
        depends_on = [depends_on]
    if not isinstance(depends_on, list):
        raise ConfigError(f"{where}: depends_on must be a list of service names")

    return Service(
        name=name,
        cmd=cmd,
        cwd=cwd,
        env=env,
        restart=restart,
        max_restarts=_number(raw, "max_restarts", 10, int, where),
        stop_timeout=_number(raw, "stop_timeout", 10.0, float, where),
        depends_on=[str(d) for d in depends_on],
        ready=_parse_ready(raw.get("ready"), where),
    )
