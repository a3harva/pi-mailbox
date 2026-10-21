import io
import json
import shutil
import subprocess
import time
import uuid

import pytest

from pi_mailbox import cli


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


def test_cli_round_trip_as_owner(root, capsys, monkeypatch):
    code, sent, _ = run(
        capsys, "send", "--from", "owner", "--to", "pi-2", "--subject", "note", "--body", "hello"
    )
    assert code == 0 and sent["to"] == "pi-2"

    monkeypatch.setenv("PI_MAILBOX_AGENT", "pi-2")
    code, inbox, _ = run(capsys, "inbox")
    assert inbox["messages"][0]["from"] == "owner"
    _, msg, _ = run(capsys, "read", sent["id"])
    assert msg["body"] == "hello"
    _, acked, _ = run(capsys, "ack", sent["id"])
    assert acked["acknowledged"] is True


def test_cli_body_from_stdin_and_comma_recipients(root, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("piped body"))
    code, sent, _ = run(
        capsys, "send", "--from", "a", "--to", "b,c", "--subject", "s", "--body-file", "-"
    )
    assert code == 0 and sent["to"] == ["b", "c"]


def test_cli_errors_are_json_on_stderr(root, capsys):
    code, out, err = run(capsys, "inbox")
    assert code == 1 and out is None
    assert "no identity" in json.loads(err)["error"]
    code, _, err = run(capsys, "read", "../x", "--as", "a")
    assert code == 1 and "invalid message id" in err


def test_nudge_missing_session_warns_and_succeeds(capsys):
    code, result, err = run(capsys, "nudge", f"no-such-session-{uuid.uuid4().hex}")
    assert code == 0 and result["nudged"] is False
    assert "pi-mailbox:" in err


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_nudge_types_into_a_real_tmux_session(capsys):
    session = f"pi-mailbox-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "cat"], check=True)
    try:
        code, result, _ = run(capsys, "nudge", session, "--text", "check; $(not run)")
        assert code == 0 and result["nudged"] is True
        pane = ""
        for _ in range(50):
            pane = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", session], capture_output=True, text=True
            ).stdout
            if "check; $(not run)" in pane:
                break
            time.sleep(0.05)
        assert "check; $(not run)" in pane
    finally:
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"], check=False)
