from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agent_mailbox import mail
from agent_mailbox.db import connect
from agent_mailbox.identity import Identity


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own state directory; nothing may touch the real one."""
    state = tmp_path / "state"
    monkeypatch.setenv("AGENT_MAILBOX_DIR", str(state))
    monkeypatch.delenv("AGENT_MAILBOX_DEBUG", raising=False)
    monkeypatch.delenv("AGENT_MAILBOX_ROLE", raising=False)
    # Tests run under some real client (often Claude Code); identity must come from the
    # env each test passes, not from the process tree of the test runner.
    monkeypatch.setattr("agent_mailbox.procs.nearest_named", lambda *a, **k: None)
    return state


@pytest.fixture
def conn(root: Path):
    c = connect(root)
    yield c
    c.close()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "my-proj"
    p.mkdir()
    return p


@pytest.fixture
def make_ident(conn, root: Path, project: Path) -> Iterator[Callable[..., Identity]]:
    made: list[Identity] = []

    def factory(
        *, claude_sid: str | None = None, pane: str | None = None, env=None, cwd: Path | None = None
    ) -> Identity:
        e = {"CLAUDECODE": "1"} if env is None else dict(env)
        if claude_sid:
            e["CLAUDE_CODE_SESSION_ID"] = claude_sid
        if pane:
            e["TMUX_PANE"] = pane
        ident = Identity.start(conn, root, env=e, cwd=str(cwd or project))
        made.append(ident)
        return ident

    yield factory
    for ident in made:
        if ident._fd is not None:
            ident.end()


@pytest.fixture
def owner_send(conn, root):
    def go(to, subject="hello", body="body", **kw):
        return mail.send(conn, root, mail.Sender("owner", None, "cli"), to, subject, body, **kw)

    return go


def set_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)
