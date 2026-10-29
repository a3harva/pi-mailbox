"""Process ancestry, used to tie a hook process to the MCP server of the same client."""

from __future__ import annotations

import os
import subprocess

# Processes that may sit between the client and a hook it spawns.
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "env", "timeout"})


def process_table() -> dict[int, tuple[int, str]]:
    """Map pid -> (ppid, command name) for every process, from one ``ps`` call."""
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,comm="],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    ).stdout
    table: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            name = os.path.basename(parts[2].strip()) if len(parts) == 3 else ""
            table[int(parts[0])] = (int(parts[1]), name.lstrip("-"))
    return table


def ancestors(pid: int | None = None, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    """Ancestors of ``pid`` (default: this process), nearest first, excluding pid 0/1."""
    pid = pid if pid is not None else os.getpid()
    try:
        table = table if table is not None else process_table()
    except (OSError, subprocess.SubprocessError):
        return []
    chain: list[int] = []
    current = table.get(pid, (0, ""))[0]
    while current and current > 1 and current not in chain:
        chain.append(current)
        current = table.get(current, (0, ""))[0]
    return chain


def nearest_named(
    names: dict[str, str], pid: int | None = None, table: dict[int, tuple[int, str]] | None = None
) -> str | None:
    """``names[comm]`` for the nearest ancestor whose command name is in ``names``."""
    try:
        table = table if table is not None else process_table()
    except (OSError, subprocess.SubprocessError):
        return None
    for ancestor in ancestors(pid, table):
        name = table.get(ancestor, (0, ""))[1]
        if name in names:
            return names[name]
    return None


def client_pid(
    pid: int | None = None, table: dict[int, tuple[int, str]] | None = None
) -> int | None:
    """The process that spawned this hook: its nearest ancestor that is not a shell."""
    try:
        table = table if table is not None else process_table()
    except (OSError, subprocess.SubprocessError):
        return None
    for ancestor in ancestors(pid, table):
        if table.get(ancestor, (0, ""))[1] not in SHELLS:
            return ancestor
    return None


def unique_descendant_session(
    client: int | None, session_chains: dict[str, list[int]]
) -> str | None:
    """The one session whose server descends from ``client``; None if zero or several.

    Only the hook's own client process is considered, never anything further up (a
    terminal, tmux server or login shell is shared by unrelated sessions, and the right
    session may not have registered yet).
    """
    if not client:
        return None
    holders = [sid for sid, chain in session_chains.items() if client in chain]
    return holders[0] if len(holders) == 1 else None
