"""Owner CLI and the status dashboard."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

from agent_mailbox import mail
from agent_mailbox.cli import main
from agent_mailbox.db import tx


def cli(*args, stdin=None, monkeypatch=None):
    out, err = io.StringIO(), io.StringIO()
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


def test_send_inbox_read_close_both_modes(make_ident, conn, root, monkeypatch):
    a = make_ident()
    code, out, _ = cli(
        "send",
        "--to",
        a.alias,
        "--subject",
        "s",
        "--body-file",
        "-",
        "--json",
        stdin="from stdin",
        monkeypatch=monkeypatch,
    )
    assert code == 0
    mid = json.loads(out)["id"]
    code, out, _ = cli("send", "--to", a.alias, "--subject", "human", "--body", "b")
    assert code == 0 and out.startswith("sent ")
    code, out, _ = cli("inbox", "--as", a.alias)
    assert "human" in out and "new (2)" in out
    code, out, err = cli("read", mid, "--as", a.alias, "--json")
    assert json.loads(out)["body"].count("from stdin") == 1 and "debugging" in err
    assert cli("defer", mid, "--as", a.alias, "--note", "later")[0] == 0
    assert cli("close", mid, "--as", a.alias, "--resolution", "done")[0] == 0
    code, out, _ = cli("status", mid, "--json")
    assert json.loads(out)["recipients"][0]["resolution"] == "done"


def test_round_trip_with_agent_reply(make_ident, conn, root):
    a = make_ident()
    code, out, _ = cli("send", "--to", a.alias, "--subject", "q", "--body", "?", "--json")
    mid = json.loads(out)["id"]
    mail.send(
        conn, root, mail.Sender(a.alias, a.session_id, "mcp"), "owner", "answer", "42", reply_to=mid
    )
    code, out, _ = cli("inbox", "--json")
    assert json.loads(out)["new"][0]["subject"] == "answer"


def test_errors_exit_1_usage_exit_2(capsys):
    assert cli("read", "nope")[0] == 1
    try:
        main(["send"])
    except SystemExit as exc:
        assert exc.code == 2


def test_agents_and_dashboard(make_ident, conn, root):
    a = make_ident()
    a.set_role("docs", "writes docs")
    m = mail.send(conn, root, mail.Sender("owner", None, "cli"), a.alias, "please", "x")
    mail.defer(conn, a.alias, m["id"], note="busy")
    code, out, _ = cli("agents")
    assert "claude-code-my-proj-docs" in out and "writes docs" in out
    code, out, _ = cli("status")
    assert "deferred: please (busy)" in out and "owner" in out
    with tx(conn):
        conn.execute("UPDATE deliveries SET overdue_at = 1")
    code, out, _ = cli("status", "--overdue-only", "--json")
    rows = json.loads(out)["agents"]
    assert [r["alias"] for r in rows] == [a.alias] and rows[0]["overdue"]
    code, out, _ = cli("status", "--overdue-only")
    assert "OVERDUE" in out


def test_set_role_and_retire_commands(make_ident, root):
    a = make_ident()
    assert cli("set-role", a.alias, "reviewer")[0] == 0
    assert a.apply_pending_role()["alias"].endswith("-reviewer")
    a.end()
    assert cli("retire", a.alias)[0] == 0
