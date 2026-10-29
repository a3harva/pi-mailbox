"""Where the mailbox lives on disk, and the permission rules for it."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

DIR_ENV = "AGENT_MAILBOX_DIR"
DIR_MODE = 0o700
FILE_MODE = 0o600


class UnsafeStateDir(RuntimeError):
    """The state directory exists but is not private to this user."""


def state_dir() -> Path:
    configured = os.environ.get(DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "agent-mailbox"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` as 0700, or refuse if it exists and is not ours and private."""
    if not path.exists():
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        # mkdir's mode is filtered by the umask; set it explicitly.
        os.chmod(path, DIR_MODE)
    st = path.stat()
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeStateDir(f"{path} is not a directory")
    if st.st_uid != os.getuid():
        raise UnsafeStateDir(f"{path} is owned by uid {st.st_uid}, not you; refusing to use it")
    if st.st_mode & 0o077:
        raise UnsafeStateDir(
            f"{path} is readable or writable by other users (mode "
            f"{stat.S_IMODE(st.st_mode):o}); fix it with: chmod 700 {path}"
        )
    return path


def open_private_file(path: Path, flags: int = os.O_RDWR | os.O_CREAT) -> int:
    fd = os.open(path, flags, FILE_MODE)
    os.fchmod(fd, FILE_MODE)
    return fd


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: object, *, max_len: int = 64) -> str:
    """Lowercase ``[a-z0-9-]`` with single dashes; raises ValueError when nothing is left."""
    if not isinstance(text, str):
        raise ValueError("expected a string")
    out = _SLUG_RE.sub("-", text.lower()).strip("-")[:max_len].strip("-")
    if not out:
        raise ValueError(f"{text!r} has no usable characters (a-z, 0-9)")
    return out
