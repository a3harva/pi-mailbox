from pathlib import Path

import pytest

from pi_mailbox.core import Mailbox


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "mailbox"
    monkeypatch.setenv("PI_MAILBOX_DIR", str(path))
    monkeypatch.delenv("PI_MAILBOX_AGENT", raising=False)
    return path


@pytest.fixture
def agent(root: Path):
    """Factory for a registered (known) agent's mailbox."""

    def make(name: str) -> Mailbox:
        box = Mailbox(name, root)
        box.register()
        return box

    return make
