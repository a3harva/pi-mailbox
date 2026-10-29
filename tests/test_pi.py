"""Pi support: the `hook pi` adapter, process-based client detection, startup role,
in-process wake hand-off, and `install pi`."""

from __future__ import annotations

import io
import json
import time

import pytest

from agent_mailbox import hooks, wake
from agent_mailbox import install as inst
from agent_mailbox.db import tx
from agent_mailbox.identity import CLIENT_PROCESSES
from agent_mailbox.procs import nearest_named  # the real one; conftest stubs the module's

PI_PID = 4242


def pi_ident(make_ident, conn, pid=PI_PID):
    ident = make_ident(env={"PI_CODING_AGENT": "true"})
    with tx(conn):
        conn.execute(
            "UPDATE sessions SET ancestor_pids = ? WHERE id = ?",
            (json.dumps([pid + 1, pid, 7]), ident.session_id),
        )
    return ident


def pi(event, root, pid=PI_PID, **extra):
    out = hooks.run("pi", event, json.dumps({"client_pid": pid, **extra}), root)
    return json.loads(out) if out else None


def turn_state(conn, ident):
    row = conn.execute("SELECT turn_state FROM sessions WHERE id = ?", (ident.session_id,))
    return row.fetchone()[0]


def test_nearest_named_picks_the_innermost_client():
    # Pi started from a Claude Code shell: the server's nearest client process is Pi.
    t = {500: (400, "python3"), 400: (300, "pi"), 300: (200, "zsh"), 200: (7, "claude")}
    assert nearest_named(CLIENT_PROCESSES, 500, t) == "pi"
    t = {500: (400, "python3"), 400: (200, "uv"), 200: (7, "claude")}
    assert nearest_named(CLIENT_PROCESSES, 500, t) == "claude-code"
    assert nearest_named(CLIENT_PROCESSES, 500, {500: (7, "python3")}) is None


def test_pi_session_never_records_an_inherited_claude_session_id(make_ident, conn, monkeypatch):
    monkeypatch.setattr("agent_mailbox.procs.nearest_named", lambda *a, **k: "pi")
    ident = make_ident(claude_sid="outer-claude")  # env says Claude Code, process says Pi
    row = conn.execute(
        "SELECT client, claude_session_id FROM sessions WHERE id = ?", (ident.session_id,)
    ).fetchone()
    assert (row["client"], row["claude_session_id"]) == ("pi", None)
    assert ident.alias.startswith("pi-my-proj-")


def test_explicit_client_override(make_ident, monkeypatch):
    monkeypatch.setattr("agent_mailbox.procs.nearest_named", lambda *a, **k: "claude-code")
    assert make_ident(env={"AGENT_MAILBOX_CLIENT": "pi"}).alias.startswith("pi-my-proj-")


def test_resolution_by_pi_pid_only(make_ident, conn, root):
    ident = pi_ident(make_ident, conn)
    pi_ident(make_ident, conn, pid=9000)
    pi("session-start", root)
    assert turn_state(conn, ident) == "idle"
    assert pi("turn-start", root, pid=1234) is None  # unknown pid: nothing, fails open
    assert hooks.run("pi", "turn-start", '{"client_pid": "4242"}', root) == ""
    assert hooks.run("pi", "bogus", json.dumps({"client_pid": PI_PID}), root) == ""


def test_first_turn_gets_identity_then_digest_once(make_ident, owner_send, conn, root):
    ident = pi_ident(make_ident, conn)
    owner_send(ident.alias, subject="review please")
    out = pi("turn-start", root, first=True)
    assert ident.alias in out["text"] and "temporary name" in out["text"]
    assert "review please" in out["text"]
    assert turn_state(conn, ident) == "busy"
    later = pi("turn-start", root)
    assert "review please" in later["text"] and "temporary name" not in later["text"]


def test_tool_result_once_low_never_nested_never(make_ident, owner_send, conn, root):
    ident = pi_ident(make_ident, conn)
    owner_send(ident.alias, subject="fyi", priority="low")
    assert pi("tool-result", root) is None
    owner_send(ident.alias, subject="now", priority="urgent")
    assert pi("tool-result", root, nested=True) is None
    out = pi("tool-result", root)
    assert "URGENT" in out["text"] and "now" in out["text"]
    assert pi("tool-result", root) is None  # injected once


def test_settle_gate_continues_then_settled_goes_idle(make_ident, owner_send, conn, root):
    ident = pi_ident(make_ident, conn)
    owner_send(ident.alias, subject="needs an answer")
    first = pi("settle", root)
    assert first["continue"] is True and "before finishing" in first["text"]
    assert pi("settle", root)["continue"] is True
    assert pi("settle", root) is None  # loop guard: two blocks, then overdue
    pi("settled", root)
    assert turn_state(conn, ident) == "idle"


def test_idle_check_wakes_once_per_debounce(make_ident, owner_send, conn, root):
    ident = pi_ident(make_ident, conn)
    pi("session-start", root)
    assert pi("idle-check", root) is None  # nothing new
    owner_send(ident.alias, subject="low one", priority="low")
    assert pi("idle-check", root) is None  # low never wakes
    owner_send(ident.alias, subject="normal one")
    out = pi("idle-check", root)
    assert out["wake"] is True and ident.alias in out["text"]
    assert pi("idle-check", root) is None  # debounced
    pi("turn-start", root)  # busy now
    with tx(conn):
        conn.execute("UPDATE sessions SET last_wake_at = 0")
    assert pi("idle-check", root) is None  # busy sessions are not woken


def test_server_side_wake_leaves_pi_extension_sessions_alone(make_ident, conn, root):
    ident = make_ident(env={"PI_CODING_AGENT": "true"}, pane="%999")
    assert wake.maybe_wake(conn, root, [ident.alias], "urgent") == [
        {"alias": ident.alias, "woke": False, "reason": "tmux pane %999 no longer exists"}
    ]
    hooks._set_turn(conn, ident.session_id, "idle")  # the extension has reported a turn
    (result,) = wake.maybe_wake(conn, root, [ident.alias], "urgent")
    assert result["reason"].startswith("in-process")


def test_hook_latency_pi(make_ident, conn, root):
    pi_ident(make_ident, conn)
    t = time.monotonic()
    for _ in range(5):
        pi("tool-result", root)
    assert (time.monotonic() - t) / 5 < 0.2


# --- install pi -----------------------------------------------------------------------------

CMD = ["/opt/bin/agent-mailbox"]


def paths(tmp_path):
    return {
        "mcp_config": str(tmp_path / "mcp.json"),
        "extension_dir": str(tmp_path / "ext" / "agent-mailbox"),
    }


def test_install_pi_round_trip(tmp_path):
    p = paths(tmp_path)
    other = {"mcpServers": {"playwright": {"command": "npx", "args": ["x"]}}, "keep": 1}
    (tmp_path / "mcp.json").write_text(json.dumps(other))
    out = io.StringIO()
    assert inst.install_pi(command=CMD, yes=True, out=out, **p) == 0
    data = json.loads((tmp_path / "mcp.json").read_text())
    assert data["mcpServers"]["playwright"] == other["mcpServers"]["playwright"]
    assert data["keep"] == 1
    entry = data["mcpServers"]["agent-mailbox"]
    assert entry["command"] == CMD[0] and entry["args"] == ["serve"]
    assert entry["exposure"] == "direct"
    ext = tmp_path / "ext" / "agent-mailbox"
    assert "hook" in (ext / "index.ts").read_text()
    assert json.loads((ext / "config.json").read_text()) == {"protocol": 1, "command": CMD}

    again = io.StringIO()
    inst.install_pi(command=CMD, yes=True, out=again, **p)
    assert again.getvalue().count("no changes") == 2  # idempotent

    assert inst.uninstall_pi(yes=True, out=io.StringIO(), **p) == 0
    assert json.loads((tmp_path / "mcp.json").read_text())["mcpServers"] == other["mcpServers"]
    assert not ext.exists()


def test_install_pi_needs_yes_without_tty(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(inst.InstallError):
        inst.install_pi(command=CMD, out=io.StringIO(), **paths(tmp_path))
    assert not (tmp_path / "mcp.json").exists()


def test_doctor_pi(tmp_path, root):
    p = paths(tmp_path)
    out = io.StringIO()
    assert inst.doctor_pi(out=out, **p) == 1
    inst.install_pi(command=["sh"], yes=True, out=io.StringIO(), **p)
    out = io.StringIO()
    assert inst.doctor_pi(out=out, **p) == 0, out.getvalue()
    (tmp_path / "ext" / "agent-mailbox" / "index.ts").write_text("// old")
    out = io.StringIO()
    assert inst.doctor_pi(out=out, **p) == 1 and "outdated" in out.getvalue()
