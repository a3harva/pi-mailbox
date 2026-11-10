"""Tmux wake-ups."""

from __future__ import annotations

import shutil
import subprocess
import time
from unittest import mock

import pytest

from agent_mailbox import wake
from agent_mailbox.db import tx


@pytest.mark.parametrize(
    ("turn", "priority", "expected"),
    [
        ("idle", "urgent", True),
        ("idle", "normal", True),
        ("idle", "low", False),
        ("unknown", "urgent", True),
        ("unknown", "normal", False),
        ("unknown", "low", False),
        ("busy", "urgent", False),
        ("busy", "normal", False),
        ("busy", "low", False),
    ],
)
def test_decision_table(turn, priority, expected):
    assert wake.should_wake(turn, priority) is expected


def test_no_pane_spawns_nothing(make_ident, conn, root):
    a = make_ident()
    with mock.patch.object(subprocess, "run") as run:
        assert wake.maybe_wake(conn, root, [a.alias], "urgent") == []
        run.assert_not_called()


def test_busy_session_never_woken(make_ident, conn, root):
    a = make_ident(pane="%1")
    with tx(conn):
        conn.execute("UPDATE sessions SET turn_state='busy' WHERE id=?", (a.session_id,))
    with mock.patch.object(wake, "type_into_pane") as typed:
        assert wake.maybe_wake(conn, root, [a.alias], "urgent") == []
        typed.assert_not_called()


@pytest.mark.tmux
@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_real_tmux_wake_and_debounce(make_ident, conn, root, tmp_path):
    name = f"am-test-{int(time.time() * 1000)}"
    out = tmp_path / "typed.txt"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, f"cat > {out}"], check=True)
    try:
        pane = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        a = make_ident(pane=pane)
        with tx(conn):
            conn.execute("UPDATE sessions SET turn_state='idle' WHERE id=?", (a.session_id,))
        first = wake.maybe_wake(conn, root, [a.alias], "normal")
        assert first[0]["woke"] is True
        second = wake.maybe_wake(conn, root, [a.alias], "normal")
        assert second[0] == {"alias": a.alias, "woke": False, "reason": "debounced"}
        # The pane's shell may not have created the file yet (seen on Linux CI).
        deadline = time.time() + 5
        while time.time() < deadline and not (out.exists() and wake.WAKE_TEXT in out.read_text()):
            time.sleep(0.1)
        assert out.read_text().count(wake.WAKE_TEXT) == 1
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def test_missing_pane_fails_open(make_ident, conn, root):
    a = make_ident(pane="%99999")
    with tx(conn):
        conn.execute("UPDATE sessions SET turn_state='idle' WHERE id=?", (a.session_id,))
    result = wake.maybe_wake(conn, root, [a.alias], "urgent")
    assert result[0]["woke"] is False


def test_idle_again_is_woken_again_inside_the_debounce(make_ident, conn, root):
    # Woken, did the work in a few seconds, went idle: the next message must wake it too.
    a = make_ident(pane="%1")
    with tx(conn):
        conn.execute("UPDATE sessions SET turn_state='idle' WHERE id=?", (a.session_id,))
    with mock.patch.object(wake, "type_into_pane") as typed:
        assert wake.maybe_wake(conn, root, [a.alias], "normal")[0]["woke"] is True
        assert wake.maybe_wake(conn, root, [a.alias], "normal")[0]["reason"] == "debounced"
        with tx(conn):  # the turn that wake-up started has come and gone
            conn.execute(
                "UPDATE sessions SET claude_hooks_seen_at = last_wake_at + 1 WHERE id=?",
                (a.session_id,),
            )
        assert wake.maybe_wake(conn, root, [a.alias], "normal")[0]["woke"] is True
        assert typed.call_count == 2


def test_interrupted_session_is_not_busy_forever(make_ident, conn, root):
    assert wake.should_wake("busy", "urgent", wake.BUSY_STALE_SECONDS + 1) is True
    assert wake.should_wake("busy", "normal", wake.BUSY_STALE_SECONDS + 1) is False
    assert wake.should_wake("busy", "urgent", 10) is False
    a = make_ident(pane="%1")
    with tx(conn):
        conn.execute(
            "UPDATE sessions SET turn_state='busy', claude_hooks_seen_at=? WHERE id=?",
            (time.time() - 3600, a.session_id),
        )
    with mock.patch.object(wake, "type_into_pane"):
        assert wake.maybe_wake(conn, root, [a.alias], "urgent")[0]["woke"] is True
