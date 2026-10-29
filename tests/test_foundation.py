"""Paths, permissions, ids, database."""

from __future__ import annotations

import multiprocessing as mp
import os
import sqlite3
import stat
import uuid
from pathlib import Path
from unittest import mock

import pytest

from agent_mailbox.db import MIGRATIONS, connect, migrate, schema_version, tx
from agent_mailbox.ids import uuid7
from agent_mailbox.paths import UnsafeStateDir, ensure_private_dir, slug, state_dir


def test_uuid7_is_strictly_increasing_and_valid():
    ids = [uuid7() for _ in range(10_000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    parsed = uuid.UUID(ids[0])
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122


def test_state_dir_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MAILBOX_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert state_dir() == tmp_path / "xdg" / "agent-mailbox"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state_dir() == Path.home() / ".local" / "state" / "agent-mailbox"
    monkeypatch.setenv("AGENT_MAILBOX_DIR", str(tmp_path / "x"))
    assert state_dir() == tmp_path / "x"


def test_loose_directory_is_refused_with_fix(tmp_path):
    d = tmp_path / "loose"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(UnsafeStateDir, match="chmod 700"):
        ensure_private_dir(d)


def test_foreign_owner_is_refused(tmp_path):
    d = tmp_path / "theirs"
    d.mkdir()
    os.chmod(d, 0o700)
    real = d.stat()
    fake = os.stat_result((real.st_mode, *real[1:4], real.st_uid + 1, *real[5:]))
    with (
        mock.patch.object(Path, "stat", return_value=fake),
        pytest.raises(UnsafeStateDir, match="owned by uid"),
    ):
        ensure_private_dir(d)


def test_everything_created_is_private_even_with_open_umask(tmp_path):
    old = os.umask(0)
    try:
        root = tmp_path / "state"
        conn = connect(root)
        with tx(conn):
            conn.execute(
                "INSERT INTO agents (alias, client, project, created_at, last_seen_at)"
                " VALUES ('a', 'c', 'p', 0, 0)"
            )
        conn.close()
    finally:
        os.umask(old)
    for p in [root, *root.rglob("*")]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o077 == 0, f"{p} has mode {mode:o}"


def test_slug():
    assert slug("Claude Code!!") == "claude-code"
    assert len(slug("x" * 200)) == 64
    with pytest.raises(ValueError):
        slug("!!!")


def test_migrations_idempotent(root):
    conn = connect(root)
    assert schema_version(conn) == len(MIGRATIONS)
    migrate(conn)
    assert schema_version(conn) == len(MIGRATIONS)
    assert conn.execute("SELECT COUNT(*) FROM agents WHERE alias='owner'").fetchone()[0] == 1
    conn.close()


def test_check_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO messages (id, thread_id, from_alias, via, priority, subject, body,"
            " created_at) VALUES ('m', 'm', 'a', 'mcp', 'bogus', 's', 'b', 0)"
        )


def _writer(root: str, n: int, tag: int) -> None:
    conn = connect(Path(root))
    for i in range(n):
        with tx(conn):
            conn.execute(
                "INSERT INTO agents (alias, client, project, created_at, last_seen_at)"
                " VALUES (?, 'c', 'p', 0, 0)",
                (f"w{tag}-{i}",),
            )
    conn.close()


def test_concurrent_writers(root):
    connect(root).close()
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(root), 200, t)) for t in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    conn = connect(root)
    assert conn.execute("SELECT COUNT(*) FROM agents WHERE alias LIKE 'w%'").fetchone()[0] == 1600
    conn.close()
