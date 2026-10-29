"""Sessions, provisional aliases, set_role, locks, retire, cleanup."""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

import pytest

from agent_mailbox import mail
from agent_mailbox.db import connect, now, tx
from agent_mailbox.errors import MailboxError
from agent_mailbox.identity import (
    Identity,
    acquire_lock,
    cleanup_provisional,
    detect_client,
    is_live,
    list_agents,
    make_alias,
    request_role,
    retire_offline,
)


def test_detect_client():
    assert detect_client({"CLAUDECODE": "1"}) == "claude-code"
    assert detect_client({"PI_CODING_AGENT": "true"}) == "pi"
    assert detect_client({}) == "mcp"
    assert detect_client({}, "claude-code") == "claude-code"
    assert detect_client({}, "Some Client") == "some-client"


def test_alias_length_cap():
    alias = make_alias("claude-code", "p" * 60, "refactor-agent")
    assert len(alias) <= 64
    assert alias.startswith("claude-code-p") and alias.endswith("-refactor-agent")


def test_start_creates_session_and_provisional_alias(make_ident, conn):
    ident = make_ident(claude_sid="c1")
    assert ident.provisional
    assert ident.alias.startswith("claude-code-my-proj-") and len(ident.alias.split("-")[-1]) == 4
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (ident.session_id,)).fetchone()
    assert row["claude_session_id"] == "c1"
    assert row["tmux_pane"] is None
    assert row["turn_state"] == "unknown"
    assert make_ident(pane="%9").alias != ident.alias


def test_pane_recorded(make_ident, conn):
    ident = make_ident(pane="%3")
    row = conn.execute("SELECT tmux_pane FROM sessions WHERE id=?", (ident.session_id,)).fetchone()
    assert row["tmux_pane"] == "%3"


def test_git_toplevel_is_the_project(make_ident, project):
    os.system(f"git -C {project} init -q")
    sub = project / "src" / "deep"
    sub.mkdir(parents=True)
    assert make_ident(cwd=sub).project == "my-proj"


def test_end_marks_session_and_releases_lock(make_ident, conn, root):
    ident = make_ident()
    assert is_live(root, ident.alias)
    ident.end()
    assert not is_live(root, ident.alias)
    row = conn.execute("SELECT ended_at FROM sessions WHERE id=?", (ident.session_id,)).fetchone()
    assert row["ended_at"] is not None


def _hold(root: str, alias: str, ready) -> None:
    acquire_lock(Path(root), alias, "child")
    ready.set()
    time.sleep(60)


def test_lock_conflict_and_crash_release(root, conn):
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    p = ctx.Process(target=_hold, args=(str(root), "x-y-z", ready))
    p.start()
    assert ready.wait(20)
    assert is_live(root, "x-y-z")
    assert acquire_lock(root, "x-y-z", "me") is None
    os.kill(p.pid, signal.SIGKILL)
    p.join(10)
    fd = acquire_lock(root, "x-y-z", "me")
    assert fd is not None
    os.close(fd)
    assert not is_live(root, "never-bound")


def test_provisional_to_new_migrates_and_forwards(make_ident, owner_send, conn, root):
    ident = make_ident()
    old = ident.alias
    owner_send(old, "one")
    owner_send(old, "two")
    r = ident.set_role("Refactor Agent", "refactor core storage")
    assert r["action"] == "created" and r["migrated"] == 2
    assert ident.alias == "claude-code-my-proj-refactor-agent" and not ident.provisional
    assert not is_live(root, old) and is_live(root, ident.alias)
    assert len(mail.inbox(conn, ident.alias)["new"]) == 2
    sent = owner_send(old, "three")  # forwarder
    assert sent["to"] == [ident.alias] and "renamed" in sent["warnings"][0]


def test_resume_offline_alias_inherits(make_ident, owner_send, conn):
    a = make_ident()
    a.set_role("refactor-agent")
    owner_send(a.alias, "left behind")
    a.end()
    b = make_ident()
    owner_send(b.alias, "mine")
    r = b.set_role("refactor-agent")
    assert r["action"] == "resumed" and r["inherited_open"] == 1
    assert len(mail.inbox(conn, b.alias)["new"]) == 2


def test_live_alias_cannot_be_taken(make_ident):
    a = make_ident()
    a.set_role("reviewer")
    b = make_ident()
    with pytest.raises(MailboxError, match=a.session_id):
        b.set_role("reviewer")
    assert b.provisional


def test_durable_to_durable_releases_without_migrating(make_ident, owner_send, conn, root):
    s = make_ident()
    s.set_role("a-role")
    first = s.alias
    owner_send(first, "stays")
    r = s.set_role("b-role")
    assert r["warnings"] and "stay with" in r["warnings"][0]
    assert not is_live(root, first)
    assert mail.inbox(conn, first)["new"] and not mail.inbox(conn, s.alias)["new"]


def test_same_role_updates_purpose(make_ident):
    s = make_ident()
    s.set_role("x")
    assert s.set_role("x", "new purpose")["action"] == "unchanged"
    assert s.purpose == "new purpose"


def test_role_validation(make_ident):
    s = make_ident()
    with pytest.raises(MailboxError):
        s.set_role("one two three four five")
    with pytest.raises(MailboxError):
        s.set_role("!!!")


def test_kill_mid_set_role_loses_nothing(make_ident, owner_send, conn, root):
    s = make_ident()
    owner_send(s.alias, "precious")
    real_tx = tx

    class Boom(Exception):
        pass

    def exploding_tx(c):
        class Ctx:
            def __enter__(self):
                self.cm = real_tx(c)
                return self.cm.__enter__()

            def __exit__(self, *exc):
                self.cm.__exit__(Boom, Boom(), None)
                raise Boom

        return Ctx()

    import agent_mailbox.identity as identity_mod

    old = s.alias
    identity_mod.tx = exploding_tx
    try:
        with pytest.raises(Boom):
            s.set_role("crashy")
    finally:
        identity_mod.tx = real_tx
    assert s.alias == old and is_live(root, old)
    assert not is_live(root, "claude-code-my-proj-crashy")
    assert len(mail.inbox(conn, old)["new"]) == 1


def test_whoami_agents_and_retire(make_ident, owner_send, conn, root):
    a = make_ident()
    a.set_role("docs", "writes docs")
    b = make_ident()
    b.set_role("refactor")
    owner_send(b.alias, "x")
    b.end()
    listed = {x["alias"]: x for x in list_agents(conn, root)}
    assert listed[a.alias]["live"] and listed[a.alias]["purpose"] == "writes docs"
    assert not listed[b.alias]["live"] and listed[b.alias]["open"]["total"] == 1
    r = a.retire()
    assert a.provisional and r["retired"] == "claude-code-my-proj-docs"
    with pytest.raises(MailboxError, match="retired"):
        owner_send("claude-code-my-proj-docs")
    with pytest.raises(MailboxError, match="retired"):
        a.set_role("docs")


def test_owner_retire_offline_only(make_ident, conn, root):
    a = make_ident()
    a.set_role("live-one")
    with pytest.raises(MailboxError, match="is live"):
        retire_offline(conn, root, a.alias)
    a.end()
    assert retire_offline(conn, root, a.alias)["retired"] == a.alias


def test_cleanup_provisional(make_ident, owner_send, conn, root):
    a = make_ident()
    b = make_ident()
    owner_send(b.alias, "keep me")
    a.end()
    b.end()
    with tx(conn):
        conn.execute("UPDATE agents SET last_seen_at = ?", (now() - 2 * 86400,))
    assert cleanup_provisional(conn, root) == 1
    aliases = {r[0] for r in conn.execute("SELECT alias FROM agents")}
    assert a.alias not in aliases and b.alias in aliases


def test_owner_requested_role_applied_on_next_call(make_ident, conn, root):
    s = make_ident()
    request_role(conn, root, s.alias, "reviewer", "reviews PRs")
    applied = s.apply_pending_role()
    assert applied["alias"] == "claude-code-my-proj-reviewer"
    assert s.apply_pending_role() is None
    with pytest.raises(MailboxError, match="no live session"):
        request_role(conn, root, "nope", "x", None)


def test_stale_sessions_reconciled(make_ident, conn, root):
    s = make_ident()
    sid = s.session_id
    os.close(s._fd)  # simulate a crash: the lock goes away, the row stays open
    s._fd = None
    c2 = connect(root)
    Identity.start(c2, root, env={}, cwd=str(root)).end()
    row = conn.execute("SELECT ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row["ended_at"] is not None
    c2.close()
