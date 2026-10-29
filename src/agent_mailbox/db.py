"""SQLite storage: connection setup, schema migrations and write transactions.

All timestamps are unix seconds (REAL). Aliases are plain text: a delivery may be
addressed to an alias nobody has claimed yet, so deliveries do not reference ``agents``.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .paths import ensure_private_dir, open_private_file, state_dir

DB_NAME = "mailbox.db"
OWNER = "owner"
BROADCAST = "all"
RESERVED = frozenset({OWNER, BROADCAST})

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE agents (
        alias        TEXT PRIMARY KEY,
        client       TEXT NOT NULL,
        project      TEXT NOT NULL,
        role         TEXT,
        purpose      TEXT,
        provisional  INTEGER NOT NULL DEFAULT 0 CHECK (provisional IN (0, 1)),
        renamed_to   TEXT,
        created_at   REAL NOT NULL,
        last_seen_at REAL NOT NULL,
        retired_at   REAL
    );

    CREATE TABLE sessions (
        id                   TEXT PRIMARY KEY,
        alias                TEXT,
        client               TEXT NOT NULL,
        client_name_raw      TEXT,
        server_pid           INTEGER NOT NULL,
        ancestor_pids        TEXT NOT NULL DEFAULT '[]',
        cwd                  TEXT NOT NULL,
        project_root         TEXT NOT NULL,
        claude_session_id    TEXT,
        tmux_pane            TEXT,
        claude_hooks_seen_at REAL,
        turn_state           TEXT NOT NULL DEFAULT 'unknown'
                             CHECK (turn_state IN ('idle', 'busy', 'unknown')),
        pending_role         TEXT,
        pending_purpose      TEXT,
        last_wake_at         REAL,
        started_at           REAL NOT NULL,
        ended_at             REAL
    );
    CREATE INDEX sessions_live ON sessions (alias) WHERE ended_at IS NULL;
    CREATE INDEX sessions_claude ON sessions (claude_session_id) WHERE ended_at IS NULL;

    CREATE TABLE messages (
        id            TEXT PRIMARY KEY,
        thread_id     TEXT NOT NULL,
        reply_to      TEXT,
        from_alias    TEXT NOT NULL,
        from_session  TEXT,
        via           TEXT NOT NULL CHECK (via IN ('mcp', 'cli')),
        priority      TEXT NOT NULL CHECK (priority IN ('urgent', 'normal', 'low')),
        expects_reply INTEGER NOT NULL DEFAULT 0 CHECK (expects_reply IN (0, 1)),
        subject       TEXT NOT NULL,
        body          TEXT NOT NULL,
        created_at    REAL NOT NULL,
        expires_at    REAL
    );
    CREATE INDEX messages_thread ON messages (thread_id);
    CREATE INDEX messages_from ON messages (from_alias, created_at);

    CREATE TABLE deliveries (
        message_id       TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
        to_alias         TEXT NOT NULL,
        state            TEXT NOT NULL DEFAULT 'new'
                         CHECK (state IN ('new', 'surfaced', 'read', 'deferred', 'closed')),
        surfaced_at      REAL,
        read_at          REAL,
        defer_kind       TEXT CHECK (defer_kind IN ('task_end', 'time')),
        deferred_until   REAL,
        defer_note       TEXT,
        defer_count      INTEGER NOT NULL DEFAULT 0,
        gate_blocks      INTEGER NOT NULL DEFAULT 0,
        remind_count     INTEGER NOT NULL DEFAULT 0,
        last_reminded_at REAL,
        overdue_at       REAL,
        closed_at        REAL,
        resolution       TEXT CHECK (resolution IN
                         ('done', 'replied', 'declined', 'no_action', 'expired')),
        resolution_note  TEXT,
        PRIMARY KEY (message_id, to_alias)
    );
    CREATE INDEX deliveries_open ON deliveries (to_alias, state);
    """,
]


def now() -> float:
    return time.time()


def db_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / DB_NAME


def connect(root: Path | None = None) -> sqlite3.Connection:
    root = ensure_private_dir(root or state_dir())
    ensure_private_dir(root / "locks")
    path = root / DB_NAME
    if not path.exists():
        os.close(open_private_file(path))
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    if schema_version(conn) >= len(MIGRATIONS):
        return
    with tx(conn):
        current = schema_version(conn)  # re-read under the write lock
        for number, sql in enumerate(MIGRATIONS[current:], start=current + 1):
            for statement in sql.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
        if current == 0:
            t = now()
            conn.execute(
                "INSERT INTO agents (alias, client, project, role, created_at, last_seen_at)"
                " VALUES (?, 'cli', '-', 'owner', ?, ?)",
                (OWNER, t, t),
            )


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that takes the database write lock up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
