"""tmux wake-ups for idle recipients. Optional, debounced, fail-open."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from .db import now, tx
from .identity import live_session_row

WAKE_TEXT = "check your mailbox"
DEBOUNCE_SECONDS = 60


BUSY_STALE_SECONDS = 15 * 60


def should_wake(turn_state: str, priority: str, quiet_seconds: float = 0.0) -> bool:
    """Idle Claude Code sessions wake for urgent/normal; unknown-state ones only for urgent.

    A session that says 'busy' but whose hooks have been quiet for a long time was
    probably interrupted (no Stop hook runs then), so it counts as unknown.
    """
    if turn_state == "busy" and quiet_seconds > BUSY_STALE_SECONDS:
        turn_state = "unknown"
    if turn_state == "idle":
        return priority in ("urgent", "normal")
    if turn_state == "unknown":
        return priority == "urgent"
    return False


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    tmux = shutil.which("tmux")
    if not tmux:
        raise FileNotFoundError("tmux is not installed")
    return subprocess.run([tmux, *args], capture_output=True, text=True, timeout=2)


def type_into_pane(pane: str, text: str = WAKE_TEXT) -> None:
    check = _tmux("display-message", "-p", "-t", pane, "#{pane_id}")
    if check.returncode != 0 or check.stdout.strip() != pane:
        raise LookupError(f"tmux pane {pane} no longer exists")
    _tmux("send-keys", "-t", pane, "-l", text).check_returncode()
    _tmux("send-keys", "-t", pane, "Enter").check_returncode()


def maybe_wake(
    conn: sqlite3.Connection, root: Path, recipients: list[str], priority: str
) -> list[dict[str, Any]]:
    results = []
    for alias in recipients:
        sess = live_session_row(conn, root, alias)
        if sess is None:
            continue
        if sess["client"] == "pi" and sess["claude_hooks_seen_at"]:
            # The Pi extension is reporting turns, so it wakes the session in-process.
            results.append({"alias": alias, "woke": False, "reason": "in-process (pi extension)"})
            continue
        if not sess["tmux_pane"]:
            continue
        t = now()
        if not should_wake(sess["turn_state"], priority, t - (sess["claude_hooks_seen_at"] or t)):
            continue
        with tx(conn):
            # The debounce only has to cover a wake-up that is still on its way. Once a
            # hook has run since the last one, that wake-up was consumed: a session that
            # finished its turn and went idle again must be woken for the next message.
            claimed = conn.execute(
                "UPDATE sessions SET last_wake_at = ? WHERE id = ?"
                " AND (last_wake_at IS NULL OR last_wake_at < ?"
                " OR COALESCE(claude_hooks_seen_at, 0) > last_wake_at)",
                (t, sess["id"], t - DEBOUNCE_SECONDS),
            ).rowcount
        if not claimed:
            results.append({"alias": alias, "woke": False, "reason": "debounced"})
            continue
        try:
            type_into_pane(sess["tmux_pane"])
            results.append({"alias": alias, "woke": True, "pane": sess["tmux_pane"]})
        except (OSError, LookupError, subprocess.SubprocessError) as exc:
            results.append({"alias": alias, "woke": False, "reason": str(exc)})
    return results
