"""CPU and memory per service, summed over its whole process group."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable


def group_usage(pgids: Iterable[int]) -> dict[int, tuple[float, int]]:
    """Map pgid -> (cpu percent, resident bytes), summed over every process in the group.

    Each service runs in its own session (start_new_session), so its pid is also the
    process group id and the group holds its whole tree: reloaders, workers, children.
    Uses `ps`, which takes the same flags on Linux and macOS. Returns {} if ps fails.
    """
    wanted = set(pgids)
    if not wanted:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pgid=,rss=,%cpu="],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    usage: dict[int, tuple[float, int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pgid, rss_kb, cpu = int(parts[0]), int(parts[1]), float(parts[2].replace(",", "."))
        except ValueError:
            continue
        if pgid in wanted:
            old_cpu, old_rss = usage.get(pgid, (0.0, 0))
            usage[pgid] = (old_cpu + cpu, old_rss + rss_kb * 1024)
    return usage


def fmt_bytes(n: int) -> str:
    """1536 -> '2K', 85 MiB -> '85M', 1.5 GiB -> '1.5G'."""
    size = float(n)
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            break
        size /= 1024
    return f"{size:.1f}{unit}" if size < 10 and unit in ("M", "G") else f"{size:.0f}{unit}"
