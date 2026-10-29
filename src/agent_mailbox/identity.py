"""Sessions, aliases and the locks that make an alias belong to one live session."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import procs
from .db import OWNER, RESERVED, now, tx
from .errors import MailboxError
from .ids import uuid7
from .paths import open_private_file, slug

MAX_ALIAS = 64
MAX_ROLE = 40
MAX_PURPOSE = 200
MAX_FORWARD_DEPTH = 5
PROVISIONAL_TTL = 24 * 3600
FORWARDER_TTL = 7 * 24 * 3600

KNOWN_CLIENTS = {"claude-code": "claude-code", "claude code": "claude-code", "pi": "pi"}
# Process names of the clients, for when the environment is ambiguous: a client started
# from inside another one (Pi from a Claude Code shell) inherits that client's variables.
CLIENT_PROCESSES = {"claude": "claude-code", "pi": "pi"}


# --- naming ---------------------------------------------------------------------


def detect_client(env: dict[str, str], client_info_name: str | None = None) -> str:
    if client_info_name:
        known = KNOWN_CLIENTS.get(client_info_name.strip().lower())
        return known or slug(client_info_name, max_len=20)
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude-code"
    if env.get("PI_CODING_AGENT"):
        return "pi"
    return "mcp"


def project_root(cwd: str) -> str:
    try:
        top = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if top.returncode == 0 and top.stdout.strip():
            return top.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd


def project_slug(root: str) -> str:
    try:
        return slug(Path(root).name or "root", max_len=40)
    except ValueError:
        return "project"


def make_alias(client: str, project: str, tail: str) -> str:
    room = MAX_ALIAS - len(client) - len(tail) - 2
    if room < 1:
        raise MailboxError("role is too long to fit in a 64-character alias")
    return f"{client}-{project[:room].rstrip('-')}-{tail}"


def normalise_role(role: object) -> str:
    try:
        value = slug(role, max_len=MAX_ROLE)
    except ValueError as exc:
        raise MailboxError(f"invalid role: {exc}") from None
    if not 1 <= len(value.split("-")) <= 4:
        raise MailboxError("role must be 1-4 words, e.g. 'refactor-agent'")
    return value


def validate_alias(alias: object) -> str:
    if not isinstance(alias, str) or not alias or len(alias) > MAX_ALIAS:
        raise MailboxError(f"invalid alias {alias!r}")
    if slug(alias, max_len=MAX_ALIAS) != alias:
        raise MailboxError(f"invalid alias {alias!r}: use lowercase a-z, 0-9 and '-'")
    return alias


# --- locks ----------------------------------------------------------------------


def _lock_path(root: Path, alias: str) -> Path:
    return root / "locks" / f"{alias}.lock"


def acquire_lock(root: Path, alias: str, session_id: str) -> int | None:
    """Take the alias's flock for this process. None if another process holds it."""
    fd = open_private_file(_lock_path(root, alias))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{session_id}\n".encode(), 0)
    return fd


def release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_live(root: Path, alias: str) -> bool:
    path = _lock_path(root, alias)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # closing drops the shared lock if we got it
    return False


# --- lookups --------------------------------------------------------------------


def resolve_alias(conn: sqlite3.Connection, alias: str) -> tuple[str, sqlite3.Row | None]:
    """Follow ``renamed_to`` forwarders. Returns (final alias, its agents row or None)."""
    seen = 0
    row = conn.execute("SELECT * FROM agents WHERE alias = ?", (alias,)).fetchone()
    while row is not None and row["renamed_to"] and seen < MAX_FORWARD_DEPTH:
        alias = row["renamed_to"]
        row = conn.execute("SELECT * FROM agents WHERE alias = ?", (alias,)).fetchone()
        seen += 1
    return alias, row


def live_session_row(conn: sqlite3.Connection, root: Path, alias: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE alias = ? AND ended_at IS NULL"
        " ORDER BY started_at DESC LIMIT 1",
        (alias,),
    ).fetchone()
    return row if row is not None and is_live(root, alias) else None


def open_counts(conn: sqlite3.Connection, alias: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT m.priority, d.state, COUNT(*) AS n FROM deliveries d"
        " JOIN messages m ON m.id = d.message_id"
        " WHERE d.to_alias = ? AND d.state != 'closed' GROUP BY m.priority, d.state",
        (alias,),
    ).fetchall()
    counts = {"urgent": 0, "normal": 0, "low": 0, "deferred": 0, "total": 0}
    for r in rows:
        counts[r["priority"]] += r["n"]
        counts["total"] += r["n"]
        if r["state"] == "deferred":
            counts["deferred"] += r["n"]
    return counts


def reconcile_stale_sessions(conn: sqlite3.Connection, root: Path) -> None:
    """Mark sessions whose server died (alias lock free) as ended."""
    rows = conn.execute("SELECT id, alias FROM sessions WHERE ended_at IS NULL").fetchall()
    dead = [r["id"] for r in rows if not r["alias"] or not is_live(root, r["alias"])]
    if dead:
        with tx(conn):
            conn.executemany(
                "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                [(now(), sid) for sid in dead],
            )


def cleanup_provisional(conn: sqlite3.Connection, root: Path) -> int:
    """Drop offline provisional aliases with nothing open; expire old forwarders."""
    t = now()
    rows = conn.execute(
        "SELECT a.alias, a.renamed_to, a.last_seen_at FROM agents a WHERE a.provisional = 1"
    ).fetchall()
    doomed = []
    for r in rows:
        ttl = FORWARDER_TTL if r["renamed_to"] else PROVISIONAL_TTL
        if t - r["last_seen_at"] < ttl or is_live(root, r["alias"]):
            continue
        has_open = conn.execute(
            "SELECT 1 FROM deliveries WHERE to_alias = ? AND state != 'closed' LIMIT 1",
            (r["alias"],),
        ).fetchone()
        if not has_open:
            doomed.append(r["alias"])
    if doomed:
        with tx(conn):
            conn.executemany("DELETE FROM agents WHERE alias = ?", [(a,) for a in doomed])
        for alias in doomed:
            _lock_path(root, alias).unlink(missing_ok=True)
    return len(doomed)


def list_agents(
    conn: sqlite3.Connection, root: Path, project: str | None = None
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agents WHERE retired_at IS NULL AND renamed_to IS NULL AND alias != ?"
        " ORDER BY alias",
        (OWNER,),
    ).fetchall()
    out = []
    for r in rows:
        if project and r["project"] != project:
            continue
        live = is_live(root, r["alias"])
        if r["provisional"] and not live:
            continue
        out.append(
            {
                "alias": r["alias"],
                "role": r["role"],
                "purpose": r["purpose"],
                "client": r["client"],
                "project": r["project"],
                "provisional": bool(r["provisional"]),
                "live": live,
                "last_seen_at": r["last_seen_at"],
                "open": open_counts(conn, r["alias"]),
            }
        )
    return out


# --- the server's own identity ----------------------------------------------------


@dataclass
class Identity:
    """One MCP server process: its session row and the alias whose lock it holds."""

    conn: sqlite3.Connection
    root: Path
    session_id: str
    client: str
    project: str
    project_root: str
    alias: str = ""
    provisional: bool = True
    role: str | None = None
    purpose: str | None = None
    _fd: int | None = field(default=None, repr=False)

    # lifecycle

    @classmethod
    def start(
        cls,
        conn: sqlite3.Connection,
        root: Path,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> Identity:
        env = dict(os.environ if env is None else env)
        cwd = cwd or os.getcwd()
        reconcile_stale_sessions(conn, root)
        cleanup_provisional(conn, root)
        top = project_root(cwd)
        client = (
            (
                slug(env["AGENT_MAILBOX_CLIENT"], max_len=20)
                if env.get("AGENT_MAILBOX_CLIENT")
                else None
            )
            or procs.nearest_named(CLIENT_PROCESSES)
            or detect_client(env)
        )
        ident = cls(
            conn=conn,
            root=root,
            session_id=uuid7(),
            client=client,
            project=project_slug(top),
            project_root=top,
        )
        t = now()
        with tx(conn):
            conn.execute(
                "INSERT INTO sessions (id, client, server_pid, ancestor_pids, cwd, project_root,"
                " claude_session_id, tmux_pane, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident.session_id,
                    ident.client,
                    os.getpid(),
                    json.dumps(procs.ancestors()),
                    cwd,
                    top,
                    (env.get("CLAUDE_CODE_SESSION_ID") or None)
                    if client == "claude-code"
                    else None,
                    env.get("TMUX_PANE") or None,
                    t,
                ),
            )
        ident._bind_provisional()
        return ident

    def end(self) -> None:
        try:
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE sessions SET ended_at = ? WHERE id = ?", (now(), self.session_id)
                )
                self.conn.execute(
                    "UPDATE agents SET last_seen_at = ? WHERE alias = ?", (now(), self.alias)
                )
        finally:
            release_lock(self._fd)
            self._fd = None

    def _bind_provisional(self) -> list[str]:
        hexes = self.session_id.replace("-", "")
        for width in (4, 6, 8, 12, 16):
            alias = make_alias(self.client, self.project, hexes[-width:])
            if self.conn.execute("SELECT 1 FROM agents WHERE alias = ?", (alias,)).fetchone():
                continue
            fd = acquire_lock(self.root, alias, self.session_id)
            if fd is None:
                continue
            t = now()
            with tx(self.conn):
                self.conn.execute(
                    "INSERT INTO agents (alias, client, project, provisional, created_at,"
                    " last_seen_at) VALUES (?,?,?,1,?,?)",
                    (alias, self.client, self.project, t, t),
                )
                self.conn.execute(
                    "UPDATE sessions SET alias = ? WHERE id = ?", (alias, self.session_id)
                )
            old_fd, self._fd = self._fd, fd
            release_lock(old_fd)
            self.alias, self.provisional, self.role, self.purpose = alias, True, None, None
            return []
        raise MailboxError("could not allocate a provisional alias")

    def confirm_client(self, client_info_name: str | None) -> None:
        """Record the raw clientInfo name the first time a tool call sees it."""
        if not client_info_name:
            return
        with tx(self.conn):
            self.conn.execute(
                "UPDATE sessions SET client_name_raw = ? WHERE id = ? AND client_name_raw IS NULL",
                (client_info_name, self.session_id),
            )

    def touch(self) -> None:
        self.conn.execute("UPDATE agents SET last_seen_at = ? WHERE alias = ?", (now(), self.alias))

    # identity changes

    def apply_pending_role(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT pending_role, pending_purpose FROM sessions WHERE id = ?", (self.session_id,)
        ).fetchone()
        if not row or not row["pending_role"]:
            return None
        try:
            return self.set_role(row["pending_role"], row["pending_purpose"])
        finally:
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE sessions SET pending_role = NULL, pending_purpose = NULL WHERE id = ?",
                    (self.session_id,),
                )

    def set_role(self, role: str, purpose: str | None = None) -> dict[str, Any]:
        role = normalise_role(role)
        if purpose is not None and (not isinstance(purpose, str) or len(purpose) > MAX_PURPOSE):
            raise MailboxError(f"purpose must be a string of at most {MAX_PURPOSE} characters")
        target = make_alias(self.client, self.project, role)
        if target in RESERVED:
            raise MailboxError(f"alias {target!r} is reserved")
        result: dict[str, Any] = {"alias": target, "previous_alias": self.alias, "warnings": []}

        if target == self.alias:
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE agents SET purpose = COALESCE(?, purpose) WHERE alias = ?",
                    (purpose, target),
                )
            self.purpose = purpose or self.purpose
            result["action"] = "unchanged"
            return result

        row = self.conn.execute("SELECT * FROM agents WHERE alias = ?", (target,)).fetchone()
        if row is not None and row["retired_at"]:
            raise MailboxError(f"alias {target!r} was retired; choose another role")
        if row is not None and row["renamed_to"]:
            raise MailboxError(f"alias {target!r} is a forwarding address; choose another role")

        fd = acquire_lock(self.root, target, self.session_id)
        if fd is None:
            holder = live_session_row(self.conn, self.root, target)
            started = time.localtime(holder["started_at"]) if holder else None
            since = f", running since {time.strftime('%Y-%m-%d %H:%M', started)}" if started else ""
            who = holder["id"] if holder else "another process"
            raise MailboxError(
                f"alias {target!r} is in use by live session {who}{since}; choose another role"
            )
        old = self.alias
        try:
            t = now()
            with tx(self.conn):
                inherited = open_counts(self.conn, target)["total"] if row is not None else 0
                if row is None:
                    self.conn.execute(
                        "INSERT INTO agents (alias, client, project, role, purpose, created_at,"
                        " last_seen_at) VALUES (?,?,?,?,?,?,?)",
                        (target, self.client, self.project, role, purpose, t, t),
                    )
                    result["action"] = "created"
                else:
                    self.conn.execute(
                        "UPDATE agents SET role = ?, purpose = COALESCE(?, purpose),"
                        " last_seen_at = ? WHERE alias = ?",
                        (role, purpose, t, target),
                    )
                    result["action"] = "resumed"
                if self.provisional:
                    migrated = self.conn.execute(
                        "UPDATE OR IGNORE deliveries SET to_alias = ? WHERE to_alias = ?",
                        (target, old),
                    ).rowcount
                    self.conn.execute("DELETE FROM deliveries WHERE to_alias = ?", (old,))
                    self.conn.execute(
                        "UPDATE messages SET from_alias = ? WHERE from_alias = ?", (target, old)
                    )
                    self.conn.execute(
                        "UPDATE agents SET renamed_to = ?, last_seen_at = ? WHERE alias = ?",
                        (target, t, old),
                    )
                    result["migrated"] = migrated
                else:
                    left = open_counts(self.conn, old)["total"]
                    if left:
                        result["warnings"].append(
                            f"{left} open item(s) stay with {old!r}; a session that sets that "
                            "role again will pick them up"
                        )
                self.conn.execute(
                    "UPDATE sessions SET alias = ? WHERE id = ?", (target, self.session_id)
                )
                result["inherited_open"] = inherited
        except BaseException:
            release_lock(fd)
            raise
        old_fd, self._fd = self._fd, fd
        release_lock(old_fd)
        self.alias, self.provisional, self.role = target, False, role
        self.purpose = purpose if purpose is not None else (row["purpose"] if row else None)
        return result

    def retire(self) -> dict[str, Any]:
        if self.provisional:
            raise MailboxError("only a durable alias (set with set_role) can be retired")
        old = self.alias
        with tx(self.conn):
            self.conn.execute("UPDATE agents SET retired_at = ? WHERE alias = ?", (now(), old))
        left = open_counts(self.conn, old)["total"]
        self._bind_provisional()
        return {"retired": old, "alias": self.alias, "open_left_on_retired": left}


# --- owner-side operations (CLI) ----------------------------------------------------------


def request_role(
    conn: sqlite3.Connection, root: Path, target: str, role: str, purpose: str | None
) -> dict[str, Any]:
    """Queue a role change for a live session; its server applies it on the next tool call.

    Only the server holds the alias locks, so the CLI cannot rename a session directly.
    """
    role = normalise_role(role)
    row = conn.execute(
        "SELECT * FROM sessions WHERE (id = ? OR alias = ?) AND ended_at IS NULL"
        " ORDER BY started_at DESC LIMIT 1",
        (target, target),
    ).fetchone()
    if row is None or not row["alias"] or not is_live(root, row["alias"]):
        raise MailboxError(f"no live session matches {target!r}")
    with tx(conn):
        conn.execute(
            "UPDATE sessions SET pending_role = ?, pending_purpose = ? WHERE id = ?",
            (role, purpose, row["id"]),
        )
    return {
        "session_id": row["id"],
        "alias": row["alias"],
        "pending_role": role,
        "note": "applied by the session on its next mailbox tool call; its next prompt "
        "will tell the agent to make one",
    }


def retire_offline(conn: sqlite3.Connection, root: Path, alias: str) -> dict[str, Any]:
    validate_alias(alias)
    row = conn.execute("SELECT * FROM agents WHERE alias = ?", (alias,)).fetchone()
    if row is None or alias == OWNER:
        raise MailboxError(f"no agent {alias!r}")
    if row["provisional"]:
        raise MailboxError("provisional aliases are cleaned up automatically")
    if row["retired_at"]:
        return {"retired": alias, "already_retired": True}
    if is_live(root, alias):
        raise MailboxError(f"{alias!r} is live; ask that session to retire itself")
    with tx(conn):
        conn.execute("UPDATE agents SET retired_at = ? WHERE alias = ?", (now(), alias))
    return {"retired": alias, "open_left": open_counts(conn, alias)["total"]}
