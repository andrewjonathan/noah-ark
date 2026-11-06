"""File watching by polling: portable, no dependencies, cheap for a project-sized tree.

Pattern rules (gitignore-like):
- a pattern without '/' matches a file *name* anywhere:   *.py, Makefile
- a pattern with '/' matches the path relative to cwd:    app/**/*.py, templates/*
- '**' spans directories, '*' and '?' stay inside one path segment.
Ignore patterns follow the same rules and also prune whole directories.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

DEFAULT_IGNORE = (
    ".git", ".hg", ".svn", ".ark", ".venv", "venv", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "*.pyc", "*.pyo", "*.swp", "*.swx", "*~", ".#*", "4913",  # 4913: vim's write probe
)

Rule = tuple[re.Pattern[str], bool]  # (regex, match the name only)


def compile_glob(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def compile_rules(patterns: Iterable[str]) -> list[Rule]:
    rules = []
    for raw in patterns:
        pattern = raw.strip().removeprefix("./")
        if pattern:
            rules.append((compile_glob(pattern.rstrip("/")), "/" not in pattern.rstrip("/")))
    return rules


def matches(rules: list[Rule], rel: str, name: str) -> bool:
    return any(regex.match(name if name_only else rel) for regex, name_only in rules)


def snapshot(root: Path, include: Iterable[str], ignore: Iterable[str]) -> dict[str, int]:
    """Map relative path -> mtime (ns) for every watched file under root."""
    inc, ign = compile_rules(include), compile_rules(ignore)
    result: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        reldir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        prefix = "" if reldir == "." else reldir + "/"
        dirnames[:] = [d for d in dirnames if not matches(ign, prefix + d, d)]
        for name in filenames:
            rel = prefix + name
            if matches(ign, rel, name) or not matches(inc, rel, name):
                continue
            try:
                result[rel] = os.stat(os.path.join(dirpath, name)).st_mtime_ns
            except OSError:  # deleted between listing and stat
                continue
    return result


def diff(old: dict[str, int], new: dict[str, int]) -> list[str]:
    """Files added, removed or modified between two snapshots."""
    return sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))


def describe(changed: list[str]) -> str:
    more = f" (+{len(changed) - 1} more)" if len(changed) > 1 else ""
    return f"changed: {changed[0]}{more}"
