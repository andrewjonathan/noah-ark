"""Runtime state on disk under `.ark/`: supervisor pid and a status snapshot."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.pid_file = state_dir / "noah.pid"
        self.state_file = state_dir / "state.json"

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("# created by noah-ark: runtime files, never commit\n*\n")

    def write_pid(self, pid: int) -> None:
        self.ensure()
        self.pid_file.write_text(str(pid))

    def read_pid(self) -> int | None:
        try:
            return int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def running_pid(self) -> int | None:
        pid = self.read_pid()
        return pid if pid and pid_alive(pid) else None

    def clear_pid(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.pid_file.unlink()

    def write_state(self, data: dict[str, Any]) -> None:
        self.ensure()
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.state_file)  # atomic: readers never see half a file

    def read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}
