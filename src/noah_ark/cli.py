"""Command line interface: noah up | down | restart | stop | start | ps | logs | init."""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from noah_ark import __version__, control
from noah_ark.config import ConfigError, Project, load_project
from noah_ark.logs import ARK, Formatter, last_lines, parse_file_line, should_color
from noah_ark.procstats import fmt_bytes, group_usage
from noah_ark.state import StateStore, pid_alive


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        print("noah-ark supports Linux and macOS for now. On Windows, use WSL.", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except ConfigError as exc:
        print(f"noah: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-f", "--file", help="path to ark.yaml (default: search upwards from here)")

    parser = argparse.ArgumentParser(
        prog="noah", description="noah-ark: run all your services with one YAML file."
    )
    parser.add_argument("-V", "--version", action="version", version=f"noah-ark {__version__}")
    sub = parser.add_subparsers(title="commands", metavar="<command>")

    p = sub.add_parser("up", parents=[common], help="start services and stream labelled logs")
    p.add_argument("services", nargs="*", help="only these services (plus their dependencies)")
    p.add_argument("-d", "--detach", action="store_true", help="run in the background")
    p.add_argument("-p", "--profile", action="append", default=[], metavar="NAME",
                   help="also start services in this profile (repeatable)")
    p.add_argument("-t", "--timestamps", action="store_true", help="prefix lines with a clock")
    p.add_argument("--no-color", action="store_true", help="disable colours")
    p.add_argument("--no-console", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_up)

    p = sub.add_parser("down", parents=[common], help="stop a background (-d) run")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait (default 30)")
    p.add_argument("--force", action="store_true", help="SIGKILL everything if it won't stop")
    p.set_defaults(func=cmd_down)

    for name, nargs, text in (
        ("restart", "*", "restart services in a running noah-ark (default: all)"),
        ("stop", "+", "stop services in a running noah-ark (others keep running)"),
        ("start", "+", "start services that were stopped, exited or crashed"),
    ):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("services", nargs=nargs, help="service names")
        p.set_defaults(func=cmd_control, command=name)

    p = sub.add_parser("ps", parents=[common], help="show service status")
    p.set_defaults(func=cmd_ps)

    p = sub.add_parser("logs", help="show (and follow) service logs")
    p.add_argument("--file", help="path to ark.yaml")  # -f means --follow here, like tail
    p.add_argument("services", nargs="*", help=f"only these services ('{ARK}' = noah-ark itself)")
    p.add_argument("-f", "--follow", action="store_true", help="keep streaming new lines")
    p.add_argument("-n", "--lines", type=int, default=100, help="history lines (default 100)")
    p.add_argument("-g", "--grep", metavar="REGEX", help="only lines matching this pattern")
    p.add_argument("-i", "--ignore-case", action="store_true", help="case-insensitive --grep")
    p.add_argument("-t", "--timestamps", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("init", help="create ark.yaml (auto-detects ./services/*)")
    p.add_argument("--scan", default="services", help="folder holding one sub-folder per service")
    p.add_argument("--force", action="store_true", help="overwrite an existing ark.yaml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("validate", parents=[common], help="check ark.yaml and show start order")
    p.set_defaults(func=cmd_validate)
    return parser


# --------------------------------------------------------------------------- up
def cmd_up(args: argparse.Namespace) -> int:
    from noah_ark.supervisor import Supervisor

    project = load_project(args.file)
    names = project.select(args.services, args.profile)
    store = StateStore(project.state_dir)
    running = store.running_pid()
    if running:
        print(
            f"noah: already running for this project (pid {running}). "
            "Use `noah logs -f`, `noah ps` or `noah down`.",
            file=sys.stderr,
        )
        return 1
    if args.detach:
        return _start_detached(project, args.services, args.profile, len(names))
    return Supervisor(
        project,
        args.services,
        profiles=args.profile,
        console=not args.no_console,
        color=False if args.no_color else None,
        timestamps=args.timestamps,
    ).run()


def _start_detached(
    project: Project, services: list[str], profiles: list[str], count: int
) -> int:
    store = StateStore(project.state_dir)
    store.ensure()
    out_path = project.state_dir / "daemon.log"
    argv = [sys.executable, "-m", "noah_ark", "up", "-f", str(project.config_path),
            "--no-console", *(f"--profile={p}" for p in profiles), *services]
    with open(out_path, "ab") as out:
        proc = subprocess.Popen(
            argv, cwd=project.root, stdin=subprocess.DEVNULL, stdout=out,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:  # fail fast if it dies on startup
        if proc.poll() is not None:
            print(f"noah: background run exited (code {proc.returncode}); see {out_path}",
                  file=sys.stderr)
            return 1
        if store.read_pid() == proc.pid:
            break
        time.sleep(0.05)
    print(f"noah-ark is running in the background (pid {proc.pid}), {count} service(s)")
    print("  noah logs -f    follow logs\n  noah ps         status\n  noah down       stop all")
    return 0


# ------------------------------------------------------------------------- down
def cmd_down(args: argparse.Namespace) -> int:
    project = load_project(args.file)
    store = StateStore(project.state_dir)
    pid = store.running_pid()
    if pid is None:
        store.clear_pid()
        print("noah-ark is not running for this project.")
        return 0
    print(f"stopping noah-ark (pid {pid})...", end=" ", flush=True)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + args.timeout
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if pid_alive(pid):
        if not args.force:
            print("still running. Retry with `noah down --force`.")
            return 1
        for svc in store.read_state().get("services", {}).values():
            if svc.get("pid"):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(int(svc["pid"]), signal.SIGKILL)
        os.kill(pid, signal.SIGKILL)
        store.clear_pid()
        print("killed.")
        return 0
    print("done.")
    return 0


# ------------------------------------------------------- restart / stop / start
def cmd_control(args: argparse.Namespace) -> int:
    project = load_project(args.file)
    unknown = [n for n in args.services if n not in project.services]
    if unknown:
        raise ConfigError(
            f"unknown service(s): {', '.join(unknown)} (available: {', '.join(project.services)})"
        )
    store = StateStore(project.state_dir)
    if store.running_pid() is None:
        print("noah-ark is not running for this project (start it with `noah up -d`).",
              file=sys.stderr)
        return 1
    path = Path(store.read_state().get("control") or control.socket_path(project))
    try:
        reply = control.send(path, args.command, list(args.services))
    except (OSError, ValueError) as exc:
        print(f"noah: cannot reach the running noah-ark at {path} ({exc})", file=sys.stderr)
        return 1
    print(reply.get("message", ""))
    return 0 if reply.get("ok") else 1


# --------------------------------------------------------------------------- ps
def _fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def cmd_ps(args: argparse.Namespace) -> int:
    project = load_project(args.file)
    store = StateStore(project.state_dir)
    sup_pid = store.running_pid()
    services = store.read_state().get("services", {}) if sup_pid else {}
    usage = group_usage(int(s["pid"]) for s in services.values() if s.get("pid"))
    header = ("NAME", "STATUS", "PID", "CPU", "MEM", "RESTARTS", "UPTIME")
    rows = []
    for name in project.services:
        s = services.get(name)
        if not s:
            rows.append((name, "stopped", "-", "-", "-", "-", "-"))
            continue
        running = s.get("status") == "running" and s.get("started_at")
        cpu_mem = usage.get(int(s["pid"])) if s.get("pid") else None
        rows.append((
            name,
            s.get("status", "?"),
            str(s["pid"]) if s.get("pid") else "-",
            f"{cpu_mem[0]:.1f}%" if cpu_mem else "-",
            fmt_bytes(cpu_mem[1]) if cpu_mem else "-",
            str(s.get("restarts", 0)),
            _fmt_duration(time.time() - s["started_at"]) if running else "-",
        ))
    widths = [max(len(r[i]) for r in (header, *rows)) for i in range(len(header))]
    for row in (header, *rows):
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    print(f"\nsupervisor: {f'running (pid {sup_pid})' if sup_pid else 'not running'}")
    return 0


# ------------------------------------------------------------------------- logs
def cmd_logs(args: argparse.Namespace) -> int:
    project = load_project(args.file)
    names = list(args.services) or list(project.services)
    unknown = [n for n in names if n not in project.services and n != ARK]
    if unknown:
        raise ConfigError(f"unknown service(s): {', '.join(unknown)}")
    fmt = Formatter(names, should_color(sys.stdout, False if args.no_color else None),
                    args.timestamps)
    try:
        pattern = (
            re.compile(args.grep, re.IGNORECASE if args.ignore_case else 0) if args.grep else None
        )
    except re.error as exc:
        raise ConfigError(f"invalid --grep pattern: {exc}") from exc
    paths = {n: project.log_dir / f"{n}.log" for n in names}
    if not args.follow and not any(p.exists() for p in paths.values()):
        print("no logs yet; run `noah up` first.")
        return 0

    # History: last N lines of each service, interleaved by timestamp.
    offsets: dict[str, int] = {}
    inodes: dict[str, int] = {}
    entries = []
    for name, path in paths.items():
        lines, offsets[name] = last_lines(path, args.lines)
        inodes[name] = path.stat().st_ino if path.exists() else -1
        for line in lines:
            ts, stream, text = parse_file_line(line)
            if pattern is None or pattern.search(text):
                entries.append((ts, name, stream, text))
    entries.sort(key=lambda e: e[0])
    for ts, name, stream, text in entries[-args.lines:] if args.lines > 0 else []:
        print(fmt.format(name, stream, text, ts))
    if not args.follow:
        return 0

    # Follow: poll each file for new bytes; handles rotation and truncation.
    partial = {n: b"" for n in names}
    try:
        while True:
            wrote = False
            for name, path in paths.items():
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                if st.st_ino != inodes[name] or st.st_size < offsets[name]:
                    inodes[name], offsets[name], partial[name] = st.st_ino, 0, b""
                if st.st_size == offsets[name]:
                    continue
                with open(path, "rb") as fh:
                    fh.seek(offsets[name])
                    chunk = fh.read(st.st_size - offsets[name])
                offsets[name] += len(chunk)
                *complete, partial[name] = (partial[name] + chunk).split(b"\n")
                for raw in complete:
                    ts, stream, text = parse_file_line(raw.decode("utf-8", "replace"))
                    if pattern is not None and not pattern.search(text):
                        continue
                    sys.stdout.write(fmt.format(name, stream, text, ts) + "\n")
                    wrote = True
            if wrote:
                sys.stdout.flush()
            time.sleep(0.2)
    except KeyboardInterrupt:
        return 0


# ------------------------------------------------------------------------- init
HEADER = """\
# noah-ark configuration. Start everything with:  noah up
version: 1
name: {name}

# Environment shared by every service. ${{VAR}} is expanded from your shell.
env:
  LOG_LEVEL: info

services:
"""

SERVICE_TEMPLATE = """\
  {name}:
    cwd: {cwd}
    cmd: uvicorn {module}:app --host 127.0.0.1 --port {port}
{venv}    ready: {port}               # wait for this port before starting dependents
    restart: on-failure     # no | on-failure | always
"""

EXAMPLE = """\
  api-a:
    cwd: services/api_a
    cmd: uvicorn main:app --host 127.0.0.1 --port 8001
    ready: 8001

  api-b:
    cwd: services/api_b
    cmd: uvicorn main:app --host 127.0.0.1 --port 8002
    depends_on: [api-a]
    ready: 8002
"""


def cmd_init(args: argparse.Namespace) -> int:
    target = Path("ark.yaml")
    if target.exists() and not args.force:
        print("noah: ark.yaml already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    scan = Path(args.scan)
    blocks = []
    if scan.is_dir():
        for folder in sorted(d for d in scan.iterdir() if d.is_dir()):
            entry = next((f for f in ("main.py", "app.py") if (folder / f).is_file()), None)
            if entry is None:
                continue
            venv = next((v for v in (".venv", "venv") if (folder / v).is_dir()), None)
            blocks.append(SERVICE_TEMPLATE.format(
                name=folder.name.replace("_", "-").lower(),
                cwd=folder.as_posix(),
                module=entry[:-3],
                port=8001 + len(blocks),
                venv=f"    venv: {venv}\n" if venv else "",
            ))
    body = "\n".join(blocks) if blocks else EXAMPLE
    target.write_text(HEADER.format(name=Path.cwd().name) + body)
    found = f"found {len(blocks)} service(s) in {scan}/" if blocks else "wrote an example"
    print(f"created ark.yaml ({found}). Edit it, then run `noah up`.")
    return 0


# --------------------------------------------------------------------- validate
def cmd_validate(args: argparse.Namespace) -> int:
    project = load_project(args.file)
    print(f"{project.config_path} is valid · project '{project.name}'\n\nstart order:")
    for i, level in enumerate(project.levels(), 1):
        for name in level:
            svc = project.services[name]
            extra = f"  (after {', '.join(svc.depends_on)})" if svc.depends_on else ""
            if svc.profiles:
                extra += f"  [profiles: {', '.join(svc.profiles)}]"
            print(f"  {i}. {name:<16} {' '.join(svc.cmd)}{extra}")
    return 0
