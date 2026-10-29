"""Client hooks: reminders at turn boundaries and the task-end gate.

Claude Code calls ``hook claude <event>`` from its hooks settings; the Pi extension calls
``hook pi <event>`` (see ``pi_extension/``). Both share the rules below.

Hooks only read the database and print message headers. They never print bodies, and
they fail open: any problem means empty output and exit 0, so a broken mailbox can
never break the client.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from pathlib import Path
from typing import Any

from . import procs
from .db import connect, now, tx
from .identity import is_live, open_counts
from .mail import age, expire_due, open_deliveries
from .paths import open_private_file, state_dir

MAX_LINES = 5
GATE_MAX_BLOCKS = 2
SUBJECT_CHARS = 100
EVENTS = ("session-start", "user-prompt-submit", "post-tool-use", "stop")
PI_EVENTS = ("session-start", "turn-start", "tool-result", "settle", "settled", "idle-check")
WAKE_DEBOUNCE_SECONDS = 30
TOOL_HINT = (
    "Use the agent-mailbox tools: read(id), then reply (send with reply_to, which closes it), "
    "close(id), or defer(id, until='task_end') if you are mid-task."
)


# --- rendering ------------------------------------------------------------------------


def clean(text: object, limit: int = SUBJECT_CHARS) -> str:
    """Make a peer-controlled string safe to inject: one line, no angle brackets."""
    s = " ".join(str(text).split())
    s = s.replace("<", "‹").replace(">", "›")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def line(row: sqlite3.Row, t: float) -> str:
    out = (
        f"[{row['priority'].upper()}] from {clean(row['from_alias'], 64)} · "
        f'"{clean(row["subject"])}" · {age(t - row["created_at"])} · id {row["message_id"]}'
    )
    if row["state"] == "deferred":
        when = "task end" if row["defer_kind"] == "task_end" else "a set time"
        out += f" · deferred to {when}: {clean(row['defer_note'] or '', 80)}"
    return out


def render(rows: list[sqlite3.Row]) -> list[str]:
    t = now()
    lines = [f"- {line(r, t)}" for r in rows[:MAX_LINES]]
    if len(rows) > MAX_LINES:
        lines.append(f"- +{len(rows) - MAX_LINES} more (call the inbox tool)")
    return lines


# --- session resolution --------------------------------------------------------------


def _live(root: Path, row: sqlite3.Row | None) -> bool:
    return row is not None and row["alias"] is not None and is_live(root, row["alias"])


def resolve_session(
    conn: sqlite3.Connection, root: Path, data: dict[str, Any], *, wait: float = 0.0
) -> sqlite3.Row | None:
    """Find the live MCP-server session that belongs to the Claude Code session in ``data``."""
    claude_sid = data.get("session_id")
    deadline = time.monotonic() + wait
    while True:
        if claude_sid:
            row = conn.execute(
                "SELECT * FROM sessions WHERE claude_session_id = ? AND ended_at IS NULL"
                " ORDER BY started_at DESC LIMIT 1",
                (claude_sid,),
            ).fetchone()
            if _live(root, row):
                return row
        rows = conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchall()
        chains = {r["id"]: json.loads(r["ancestor_pids"]) for r in rows if _live(root, r)}
        match = procs.unique_descendant_session(procs.client_pid(), chains) if chains else None
        if match:
            if claude_sid:
                with tx(conn):
                    conn.execute(
                        "UPDATE sessions SET claude_session_id = ? WHERE id = ?",
                        (claude_sid, match),
                    )
            return conn.execute("SELECT * FROM sessions WHERE id = ?", (match,)).fetchone()
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def resolve_pi_session(
    conn: sqlite3.Connection, root: Path, data: dict[str, Any], *, wait: float = 0.0
) -> sqlite3.Row | None:
    """The live session whose MCP server descends from the Pi process in ``data``.

    The extension runs inside Pi, so it passes Pi's own pid; no ancestry walk is needed.
    """
    pid = data.get("client_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    deadline = time.monotonic() + wait
    while True:
        rows = conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL").fetchall()
        chains = {r["id"]: json.loads(r["ancestor_pids"]) for r in rows if _live(root, r)}
        match = procs.unique_descendant_session(pid, chains) if chains else None
        if match:
            return conn.execute("SELECT * FROM sessions WHERE id = ?", (match,)).fetchone()
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


# --- events ---------------------------------------------------------------------------


def _context(event_name: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text}}


def _mark_surfaced(conn: sqlite3.Connection, alias: str, ids: list[str]) -> None:
    if not ids:
        return
    t = now()
    with tx(conn):
        conn.executemany(
            "UPDATE deliveries SET state = 'surfaced', surfaced_at = ?,"
            " remind_count = remind_count + 1, last_reminded_at = ?"
            " WHERE message_id = ? AND to_alias = ? AND state = 'new'",
            [(t, t, mid, alias) for mid in ids],
        )


def _set_turn(conn: sqlite3.Connection, session_id: str, state: str) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE sessions SET turn_state = ?, claude_hooks_seen_at = ? WHERE id = ?",
            (state, now(), session_id),
        )


def session_start(conn: sqlite3.Connection, root: Path, sess: sqlite3.Row) -> dict | None:
    alias = sess["alias"]
    agent = conn.execute("SELECT * FROM agents WHERE alias = ?", (alias,)).fetchone()
    _set_turn(conn, sess["id"], "idle")
    expire_due(conn, alias)
    parts = [f"agent-mailbox: this session's mailbox address is {alias}."]
    if agent is not None and agent["provisional"]:
        parts.append(
            "It is a temporary name. When your purpose is clear (or the user names you), call "
            "the agent-mailbox set_role tool with a 1-4 word role, e.g. set_role('refactor-agent')."
        )
        offline = conn.execute(
            "SELECT alias, role FROM agents WHERE project = ? AND client = ? AND provisional = 0"
            " AND retired_at IS NULL AND renamed_to IS NULL ORDER BY last_seen_at DESC",
            (agent["project"], agent["client"]),
        ).fetchall()
        offline = [r for r in offline if not is_live(root, r["alias"])]
        if offline:
            listed = ", ".join(
                f"{clean(r['role'], 40)} ({open_counts(conn, r['alias'])['total']} open)"
                for r in offline[:MAX_LINES]
            )
            parts.append(f"Offline roles in this project you can resume with set_role: {listed}.")
    rows = open_deliveries(conn, alias)
    if rows:
        parts.append(f"{len(rows)} open message(s):")
        parts.extend(render(rows))
        parts.append(TOOL_HINT)
        _mark_surfaced(conn, alias, [r["message_id"] for r in rows if r["state"] == "new"])
    return _context("SessionStart", "\n".join(parts))


def user_prompt_submit(conn: sqlite3.Connection, root: Path, sess: sqlite3.Row) -> dict | None:
    alias = sess["alias"]
    _set_turn(conn, sess["id"], "busy")
    expire_due(conn, alias)
    parts = []
    if sess["pending_role"]:
        parts.append(
            f"agent-mailbox: the owner asked this session to take the role "
            f"'{clean(sess['pending_role'], 40)}'. Call the agent-mailbox whoami tool to apply it."
        )
    rows = open_deliveries(conn, alias)
    if rows:
        parts.append(f"agent-mailbox ({alias}): {len(rows)} open message(s):")
        parts.extend(render(rows))
        parts.append(TOOL_HINT)
        _mark_surfaced(conn, alias, [r["message_id"] for r in rows if r["state"] == "new"])
    return _context("UserPromptSubmit", "\n".join(parts)) if parts else None


def post_tool_use(
    conn: sqlite3.Connection, root: Path, sess: sqlite3.Row, data: dict[str, Any]
) -> dict | None:
    if data.get("agent_id"):  # a subagent's tool call: never interrupt subagents
        return None
    alias = sess["alias"]
    rows = [
        r
        for r in open_deliveries(conn, alias)
        if r["state"] == "new" and r["priority"] in ("urgent", "normal")
    ]
    if not rows:
        return None
    _mark_surfaced(conn, alias, [r["message_id"] for r in rows])
    urgent = any(r["priority"] == "urgent" for r in rows)
    head = (
        "agent-mailbox: URGENT mail arrived — read it now before continuing:"
        if urgent
        else "agent-mailbox: new mail arrived (handle it, or defer it to task end if you are busy):"
    )
    return _context("PostToolUse", "\n".join([head, *render(rows), TOOL_HINT]))


def gate_candidates(
    conn: sqlite3.Connection, alias: str, *, task_running: bool = False
) -> list[sqlite3.Row]:
    """Open deliveries that may block the end of a turn, ignoring the guard.

    While background work is still running the task has not ended, so items deferred to
    task end wait; only mail the agent has not triaged yet blocks.
    """
    t = now()
    out = []
    for r in open_deliveries(conn, alias):
        if r["priority"] == "low" or r["overdue_at"]:
            continue
        if r["state"] == "deferred":
            if r["defer_kind"] == "time" and r["deferred_until"] > t:
                continue
            if r["defer_kind"] == "task_end" and task_running:
                continue
        out.append(r)
    return out


def stop(
    conn: sqlite3.Connection, root: Path, sess: sqlite3.Row, data: dict[str, Any]
) -> dict | None:
    alias = sess["alias"]
    expire_due(conn, alias)
    t = now()
    candidates = gate_candidates(conn, alias, task_running=bool(data.get("background_tasks")))
    exhausted = [r for r in candidates if r["gate_blocks"] >= GATE_MAX_BLOCKS]
    blocking = [r for r in candidates if r["gate_blocks"] < GATE_MAX_BLOCKS]
    with tx(conn):
        conn.executemany(
            "UPDATE deliveries SET overdue_at = ? WHERE message_id = ? AND to_alias = ?",
            [(t, r["message_id"], alias) for r in exhausted],
        )
        conn.executemany(
            "UPDATE deliveries SET gate_blocks = gate_blocks + 1, last_reminded_at = ?,"
            " remind_count = remind_count + 1, surfaced_at = COALESCE(surfaced_at, ?),"
            " state = CASE WHEN state = 'new' THEN 'surfaced' ELSE state END"
            " WHERE message_id = ? AND to_alias = ?",
            [(t, t, r["message_id"], alias) for r in blocking],
        )
    if not blocking:
        _set_turn(conn, sess["id"], "idle")
        # Low-priority and overdue leftovers are not emitted here: Stop additionalContext
        # would keep the agent going. They appear in the next turn-start digest.
        return None
    reason = "\n".join(
        [
            f"agent-mailbox: before finishing, deal with {len(blocking)} open message(s) "
            f"for {alias}:",
            *render(blocking),
            "Read each one, then reply (send with reply_to), close it, or — only if it truly "
            "has to wait — defer it to a later time with a note.",
        ]
    )
    # additionalContext keeps the agent going under the same loop protections as
    # decision=block, but Claude Code shows it as hook feedback rather than an error.
    return _context("Stop", reason)


# --- Pi adapter (the agent-mailbox Pi extension) --------------------------------------


def _text(out: dict | None) -> str | None:
    return out["hookSpecificOutput"]["additionalContext"] if out else None


def idle_check(conn: sqlite3.Connection, sess: sqlite3.Row) -> dict | None:
    """Should the extension wake this idle session in-process? Debounced per session."""
    if sess["turn_state"] != "idle":
        return None
    rows = [
        r
        for r in open_deliveries(conn, sess["alias"])
        if r["state"] == "new" and r["priority"] in ("urgent", "normal")
    ]
    if not rows:
        return None
    t = now()
    with tx(conn):
        claimed = conn.execute(
            "UPDATE sessions SET last_wake_at = ? WHERE id = ?"
            " AND (last_wake_at IS NULL OR last_wake_at < ?)",
            (t, sess["id"], t - WAKE_DEBOUNCE_SECONDS),
        ).rowcount
    if not claimed:
        return None
    return {
        "wake": True,
        "text": f"agent-mailbox: {len(rows)} new message(s) arrived for {sess['alias']} "
        "while you were idle. Handle them now.",
    }


def pi_event(
    conn: sqlite3.Connection, root: Path, sess: sqlite3.Row, event: str, data: dict[str, Any]
) -> dict | None:
    """Run one Pi event on the shared rules and answer in the extension's shape:
    ``{"text": ...}`` to inject, plus ``"continue": true`` from the gate."""
    if event == "session-start":
        _set_turn(conn, sess["id"], "idle")
        return None
    if event == "turn-start":
        if data.get("first"):
            text = _text(session_start(conn, root, sess))
            _set_turn(conn, sess["id"], "busy")
        else:
            text = _text(user_prompt_submit(conn, root, sess))
        return {"text": text} if text else None
    if event == "tool-result":
        if data.get("nested"):  # a call made by another tool; the model never sees it
            return None
        text = _text(post_tool_use(conn, root, sess, data))
        return {"text": text} if text else None
    if event == "settle":
        text = _text(stop(conn, root, sess, data))
        return {"continue": True, "text": text} if text else None
    if event == "settled":
        _set_turn(conn, sess["id"], "idle")
        return None
    return idle_check(conn, sess)


# --- entry point ----------------------------------------------------------------------


def _debug(root: Path, message: str) -> None:
    if os.environ.get("AGENT_MAILBOX_DEBUG") != "1":
        return
    try:
        path = root / "hooks.log"
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(root / "hooks.log.1")
        fd = open_private_file(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def run(client: str, event: str, raw: str, root: Path | None = None) -> str:
    """Handle one hook call; returns the JSON to print ('' for nothing). Never raises."""
    root = root or state_dir()
    try:
        if client == "pi":
            return _run_pi(root, event, raw)
        if client != "claude" or event not in EVENTS:
            return ""
        data = json.loads(raw) if raw.strip() else {}
        conn = connect(root)
        try:
            sess = resolve_session(conn, root, data, wait=3.0 if event == "session-start" else 0)
            if sess is None:
                _debug(root, f"{event}: no session for {data.get('session_id')}")
                return ""
            if event == "session-start":
                out = session_start(conn, root, sess)
            elif event == "user-prompt-submit":
                out = user_prompt_submit(conn, root, sess)
            elif event == "post-tool-use":
                out = post_tool_use(conn, root, sess, data)
            else:
                out = stop(conn, root, sess, data)
            _debug(
                root,
                f"{event}: claude {data.get('session_id')} -> session {sess['id']} "
                f"alias {sess['alias']} -> {bool(out)}",
            )
            return json.dumps(out) if out else ""
        finally:
            conn.close()
    except Exception:  # fail open, always
        _debug(root, f"{event}: error\n{traceback.format_exc()}")
        return ""


def _run_pi(root: Path, event: str, raw: str) -> str:
    if event not in PI_EVENTS:
        return ""
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        return ""
    conn = connect(root)
    try:
        # Pi connects MCP servers in the background, so the server may still be starting.
        wait = 5.0 if event == "session-start" else 3.0 if data.get("first") else 0.0
        sess = resolve_pi_session(conn, root, data, wait=wait)
        if sess is None:
            _debug(root, f"pi {event}: no session for pid {data.get('client_pid')}")
            return ""
        out = pi_event(conn, root, sess, event, data)
        _debug(
            root,
            f"pi {event}: pid {data.get('client_pid')} -> session {sess['id']} "
            f"alias {sess['alias']} -> {bool(out)}",
        )
        return json.dumps(out) if out else ""
    finally:
        conn.close()
