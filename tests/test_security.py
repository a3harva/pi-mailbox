"""Fuzzing the injection surfaces, permissions, and no bodies in logs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agent_mailbox import hooks, mail

NASTY = st.lists(
    st.sampled_from(
        list("<>/!\n\r\t\x00\x1b ")
        + ["<<<", ">>>", "system-reminder", "end-peer-message", "é", "😀", "a"]
    ),
    min_size=1,
    max_size=30,
).map("".join)


@settings(
    max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(subject=NASTY, body=NASTY)
def test_no_escape_from_reminders_or_body_wrapper(make_ident, conn, root, subject, body):
    mail.LIMITS.sends_per_minute = 1_000_000
    if not subject.strip():
        subject = "x" + subject
    subject = subject[:200]
    a = make_ident(claude_sid="fuzz")
    m = mail.send(
        conn, root, mail.Sender("owner", None, "cli"), a.alias, subject, body, priority="urgent"
    )
    for event in hooks.EVENTS:
        out = hooks.run("claude", event, json.dumps({"session_id": "fuzz"}), root)
        if out:
            payload = json.loads(out)
            text = payload["hookSpecificOutput"]["additionalContext"]
            assert "<" not in text and ">" not in text
            assert all(len(line) < 400 for line in text.splitlines())
    r = mail.read(conn, a.alias, m["id"])
    inner = r["body"].split(">>>\n", 1)[1]
    assert inner.endswith(mail.DELIM_CLOSE)
    assert mail.DELIM_CLOSE not in inner[: -len(mail.DELIM_CLOSE)]
    assert "<<<" not in inner[: -len(mail.DELIM_CLOSE)]
    mail.close(conn, a.alias, m["id"], "done")
    a.end()
    mail.LIMITS.sends_per_minute = mail.Limits().sends_per_minute


def test_debug_log_never_holds_bodies(make_ident, owner_send, root, monkeypatch):
    monkeypatch.setenv("AGENT_MAILBOX_DEBUG", "1")
    a = make_ident(claude_sid="c1")
    owner_send(a.alias, "subj", body="TOP-SECRET-BODY")
    for event in hooks.EVENTS:
        hooks.run("claude", event, json.dumps({"session_id": "c1"}), root)
    log = (root / "hooks.log").read_text()
    assert "TOP-SECRET-BODY" not in log and "subj" not in log
    assert oct((root / "hooks.log").stat().st_mode & 0o777) == "0o600"


def test_source_never_logs_or_prints_bodies():
    src = Path(__file__).parents[1] / "src" / "agent_mailbox"
    for path in src.glob("*.py"):
        text = path.read_text()
        for match in re.finditer(r"_debug\(.*|print\(.*", text):
            assert "body" not in match.group(0).replace("_body(", ""), (path.name, match.group(0))


def test_hook_paths_do_not_import_mcp():
    src = Path(__file__).parents[1] / "src" / "agent_mailbox"
    for name in ("hooks.py", "mail.py", "identity.py", "db.py", "procs.py", "paths.py"):
        assert "import mcp" not in (src / name).read_text()
        assert "from mcp" not in (src / name).read_text()


def test_state_files_private_under_open_umask(make_ident, owner_send, root, monkeypatch):
    old = os.umask(0)
    try:
        monkeypatch.setenv("AGENT_MAILBOX_DEBUG", "1")
        a = make_ident(claude_sid="c1")
        owner_send(a.alias)
        hooks.run("claude", "stop", '{"session_id": "c1"}', root)
    finally:
        os.umask(old)
    for p in [root, *root.rglob("*")]:
        assert p.stat().st_mode & 0o077 == 0, p
