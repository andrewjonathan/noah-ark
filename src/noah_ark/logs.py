"""Log formatting, console output and rotating per-service log files."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TextIO

ARK = "ark"  # label used for the supervisor's own messages
PALETTE = ("36", "33", "32", "35", "34", "96", "93", "92", "95", "94")
RESET = "\033[0m"
DIM = "\033[2m"
STREAMS = ("out", "err", "ark")


def should_color(stream: TextIO, force: bool | None = None) -> bool:
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class Formatter:
    """Turns (service, stream, text) into one labelled, optionally coloured line."""

    def __init__(self, names: Iterable[str], color: bool = False, timestamps: bool = False):
        names = list(names)
        self.width = max([len(n) for n in names] + [len(ARK)])
        self.colors = {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(names)}
        self.colors[ARK] = "1;37"
        self.color = color
        self.timestamps = timestamps

    def format(self, name: str, stream: str, text: str, ts: str | None = None) -> str:
        label = f"{name:<{self.width}} |"
        clock = f"{(ts or now_iso())[11:23]} " if self.timestamps else ""
        if not self.color:
            return f"{clock}{label} {'» ' if stream == 'ark' else ''}{text}"
        code = self.colors.get(name, "37")
        prefix = (f"{DIM}{clock}{RESET}" if clock else "") + f"\033[{code}m{label}{RESET} "
        if stream == "ark":
            return f"{prefix}{DIM}» {text}{RESET}"
        return prefix + text


def parse_file_line(line: str) -> tuple[str, str, str]:
    """Split a log-file line written by LogSink: '<iso-ts> <stream> <text>'."""
    parts = line.rstrip("\r\n").split(" ", 2)
    if len(parts) == 3 and parts[1] in STREAMS and len(parts[0]) >= 19:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2 and parts[1] in STREAMS and len(parts[0]) >= 19:
        return parts[0], parts[1], ""
    return "", "out", line.rstrip("\r\n")


class RotatingFile:
    """Append-only, line-buffered log file rotated by size (name.log -> name.log.1 ...)."""

    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 3):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        self._size = path.stat().st_size

    def write(self, line: str) -> None:
        data = line + "\n"
        self._fh.write(data)
        self._size += len(data)
        if self._size >= self.max_bytes:
            self._rotate()

    def _rotate(self) -> None:
        self._fh.close()
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        self._size = 0

    def close(self) -> None:
        self._fh.close()


class LogSink:
    """Where every log line goes: the console (labelled) and per-service files."""

    def __init__(
        self,
        names: Iterable[str],
        log_dir: Path | None,
        *,
        console: bool = True,
        color: bool | None = None,
        timestamps: bool = False,
        stream: TextIO | None = None,
    ):
        names = list(names)
        self.stream = stream or sys.stdout
        self.console = console
        self.fmt = Formatter(names, should_color(self.stream, color), timestamps)
        self.files: dict[str, RotatingFile] = {}
        if log_dir is not None:
            for name in [*names, ARK]:
                self.files[name] = RotatingFile(log_dir / f"{name}.log")

    def emit(self, name: str, stream: str, text: str) -> None:
        ts = now_iso()
        fh = self.files.get(name)
        if fh is not None:
            fh.write(f"{ts} {stream} {text}")
        if self.console:
            try:
                self.stream.write(self.fmt.format(name, stream, text, ts) + "\n")
                self.stream.flush()
            except (BrokenPipeError, ValueError):
                self.console = False  # stdout went away; keep writing files

    def line(self, name: str, text: str, stream: str = "out") -> None:
        self.emit(name, stream, text)

    def event(self, name: str, text: str) -> None:
        """A message from noah-ark itself about a service (started, crashed...)."""
        self.emit(name, "ark", text)
        if name != ARK and ARK in self.files:
            self.files[ARK].write(f"{now_iso()} ark [{name}] {text}")

    def close(self) -> None:
        for fh in self.files.values():
            fh.close()


def last_lines(path: Path, count: int) -> tuple[list[str], int]:
    """Read the last `count` lines of a file without loading all of it.

    Returns the lines and the file size at the time of reading (the follow offset).
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = end = fh.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= count:
                step = min(8192, end)
                end -= step
                fh.seek(end)
                data = fh.read(step) + data
    except FileNotFoundError:
        return [], 0
    lines = data.splitlines()[-count:] if count > 0 else []
    return [raw.decode("utf-8", "replace") for raw in lines], size
