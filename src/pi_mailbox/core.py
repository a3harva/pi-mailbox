"""The mailbox itself: plain JSON files on disk, shared by the MCP server and the CLI.

Layout under the root (default ``~/.pi-mailbox``)::

    <agent>/inbox/<id>.json   delivered, not yet acknowledged
    <agent>/done/<id>.json    acknowledged
    <agent>/sent/<id>.json    the sender's own copy
    <agent>/.known            present once the agent has acted as itself
    <agent>/.lock             held (flock) by the one live server using this name
    <agent>/.mutex            short-lived flock around read/ack state changes

Every message file is written to a temp file in the target directory, fsynced, and
then linked or renamed into place, so a half-written message is never visible.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

AGENT_ENV = "PI_MAILBOX_AGENT"
DIR_ENV = "PI_MAILBOX_DIR"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ID_RE = re.compile(r"^msg-\d{8}T\d{12}Z-[0-9a-f]{8}$")

BROADCAST = "all"
OWNER = "owner"
RESERVED_NAMES = frozenset({BROADCAST, OWNER})

MAX_SUBJECT_CHARS = 200
MAX_BODY_BYTES = 100 * 1024
MAX_TTL_SECONDS = 365 * 24 * 3600
MAX_RECIPIENTS = 64

INBOX, DONE, SENT = "inbox", "done", "sent"

DIR_MODE = 0o700
FILE_MODE = 0o600


class MailboxError(Exception):
    """A problem the caller can fix: bad input, unknown id, missing identity."""


# --- names, ids, time ---------------------------------------------------------


def validate_name(name: object, *, allow_owner: bool = False) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise MailboxError(
            f"invalid agent name {name!r}: use 1-32 chars of a-z, 0-9, '-' or '_', "
            "starting with a letter or digit"
        )
    if name == BROADCAST or (name == OWNER and not allow_owner):
        raise MailboxError(f"agent name {name!r} is reserved")
    return name


def validate_id(message_id: object) -> str:
    if not isinstance(message_id, str) or not ID_RE.fullmatch(message_id):
        raise MailboxError(f"invalid message id {message_id!r}")
    return message_id


_id_lock = threading.Lock()
_last_id_time = datetime.min.replace(tzinfo=UTC)


def new_id(now: datetime | None = None) -> str:
    """A time-sortable id: UTC to the microsecond plus 32 random bits.

    Within one process ids are strictly increasing, so messages from one sender
    always sort in the order they were sent.
    """
    global _last_id_time
    now = (now or datetime.now(UTC)).astimezone(UTC)
    with _id_lock:
        if now <= _last_id_time:
            now = _last_id_time + timedelta(microseconds=1)
        _last_id_time = now
    return f"msg-{now.strftime('%Y%m%dT%H%M%S%f')}Z-{secrets.token_hex(4)}"


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def default_root() -> Path:
    configured = os.environ.get(DIR_ENV)
    return Path(configured).expanduser() if configured else Path.home() / ".pi-mailbox"


# --- low-level file operations ------------------------------------------------


def _ensure_dir(path: Path) -> None:
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    # mkdir's mode is filtered by the umask; set it explicitly.
    os.chmod(path, DIR_MODE)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(directory: Path, data: dict[str, Any]) -> Path:
    tmp = directory / f".tmp-{secrets.token_hex(8)}"
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return tmp


def _create_exclusive(path: Path, data: dict[str, Any]) -> None:
    """Atomically create ``path``; raise FileExistsError rather than overwrite."""
    tmp = _write_temp(path.parent, data)
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink()
    _fsync_dir(path.parent)


def _replace(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace ``path`` (or create it) with ``data``."""
    tmp = _write_temp(path.parent, data)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _load(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- message views ------------------------------------------------------------


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def is_expired(msg: dict[str, Any], now: datetime | None = None) -> bool:
    expires = _parse_time(msg.get("expires_at"))
    return expires is not None and (now or _now()) >= expires


def status_of(msg: dict[str, Any], now: datetime | None = None) -> str:
    if msg.get("acked_at"):
        return "acked"
    if is_expired(msg, now):
        return "expired"
    return "read" if msg.get("read_at") else "unread"


# --- identity lock ------------------------------------------------------------


class IdentityInUse(MailboxError):
    pass


@dataclass
class IdentityLock:
    """An flock on ``<agent>/.lock`` held for the life of one server process.

    The kernel drops the lock when the process exits, so a crashed server never
    leaves a stale lock behind.
    """

    path: Path
    fd: int

    def release(self) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


# --- the mailbox --------------------------------------------------------------


class Mailbox:
    """One agent's view of the shared mailbox root."""

    def __init__(self, agent: str, root: Path | None = None, *, allow_owner: bool = False):
        self.agent = validate_name(agent, allow_owner=allow_owner)
        self.root = root or default_root()

    # paths

    def _agent_dir(self, name: str) -> Path:
        return self.root / name

    def _box(self, name: str, box: str) -> Path:
        return self._agent_dir(name) / box

    def _register(self, name: str) -> None:
        _ensure_dir(self.root)
        _ensure_dir(self._agent_dir(name))
        for box in (INBOX, DONE, SENT):
            _ensure_dir(self._box(name, box))

    def register(self) -> None:
        """Create this agent's directories and mark it known to other agents.

        Delivering to a recipient creates its directories too, but only an agent
        acting as itself is "known"; that keeps the unknown-recipient warning
        meaningful until the recipient actually starts.
        """
        self._register(self.agent)
        marker = self._agent_dir(self.agent) / ".known"
        if not marker.exists():
            marker.touch(mode=FILE_MODE)

    def known_agents(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name
            for p in self.root.iterdir()
            if NAME_RE.fullmatch(p.name) and (p / ".known").is_file()
        )

    def acquire_identity(self) -> IdentityLock:
        """Claim this agent name for the calling process, or raise IdentityInUse."""
        self.register()
        path = self._agent_dir(self.agent) / ".lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 64).decode(errors="replace").strip() or "unknown"
            os.close(fd)
            raise IdentityInUse(
                f"agent name {self.agent!r} is already in use by another live "
                f"pi-mailbox server (pid {holder}); give this session a different "
                f"{AGENT_ENV}"
            ) from None
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        return IdentityLock(path, fd)

    @contextmanager
    def _mutex(self) -> Iterator[None]:
        """Serialise read/ack state changes on this agent's own mailbox."""
        self.register()
        fd = os.open(self._agent_dir(self.agent) / ".mutex", os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _find(self, message_id: str, boxes: tuple[str, ...]) -> tuple[str, Path] | None:
        for box in boxes:
            path = self._box(self.agent, box) / f"{message_id}.json"
            if path.is_file():
                return box, path
        return None

    # send

    def _resolve_recipients(self, to: str | list[str]) -> list[str]:
        if to == BROADCAST or to == [BROADCAST]:
            names = [n for n in self.known_agents() if n not in (self.agent, OWNER)]
            if not names:
                raise MailboxError("no other known agents to broadcast to")
            return names
        raw = [to] if isinstance(to, str) else list(to)
        if not raw:
            raise MailboxError("'to' must name at least one agent")
        if len(raw) > MAX_RECIPIENTS:
            raise MailboxError(f"too many recipients (max {MAX_RECIPIENTS})")
        names: list[str] = []
        for name in raw:
            if name == BROADCAST:
                raise MailboxError("'all' cannot be combined with other recipients")
            validate_name(name, allow_owner=True)
            if name not in names:
                names.append(name)
        return names

    def _thread_for(self, reply_to: str) -> tuple[str, str | None]:
        found = self._find(reply_to, (INBOX, DONE, SENT))
        if found:
            return _load(found[1]).get("thread") or reply_to, None
        return reply_to, (
            f"reply_to {reply_to!r} is not in {self.agent!r}'s mailbox; using it as the thread id"
        )

    def send(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        *,
        reply_to: str | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(subject, str) or not subject.strip():
            raise MailboxError("subject must be a non-empty string")
        if len(subject) > MAX_SUBJECT_CHARS:
            raise MailboxError(f"subject is longer than {MAX_SUBJECT_CHARS} characters")
        if not isinstance(body, str):
            raise MailboxError("body must be a string")
        if len(body.encode()) > MAX_BODY_BYTES:
            raise MailboxError(f"body is larger than {MAX_BODY_BYTES} bytes")
        if ttl_seconds is not None and (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 0 < ttl_seconds <= MAX_TTL_SECONDS
        ):
            raise MailboxError(f"ttl_seconds must be an integer from 1 to {MAX_TTL_SECONDS}")

        recipients = self._resolve_recipients(to)
        warnings: list[str] = []
        known = set(self.known_agents())
        for name in recipients:
            if name not in known:
                warnings.append(
                    f"agent {name!r} has never started a pi-mailbox server; the message "
                    "is queued and will be waiting when it does (check the name for typos)"
                )

        thread = None
        if reply_to is not None:
            validate_id(reply_to)
            thread, warning = self._thread_for(reply_to)
            if warning:
                warnings.append(warning)

        self.register()
        for name in recipients:
            self._register(name)

        created = _now()
        expires = created + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        to_field: str | list[str] = recipients[0] if len(recipients) == 1 else recipients

        # Recipients first: the id is "taken" once every inbox copy exists. On the
        # (astronomically unlikely) collision, undo and retry with a fresh id.
        for _ in range(5):
            message_id = new_id()
            message = {
                "id": message_id,
                "from": self.agent,
                "to": to_field,
                "subject": subject,
                "body": body,
                "created_at": _iso(created),
                "read_at": None,
                "acked_at": None,
                "reply_to": reply_to,
                "thread": thread or message_id,
                "ttl_seconds": ttl_seconds,
                "expires_at": _iso(expires) if expires else None,
            }
            written: list[Path] = []
            try:
                for name in recipients:
                    path = self._box(name, INBOX) / f"{message_id}.json"
                    _create_exclusive(path, message)
                    written.append(path)
                _create_exclusive(self._box(self.agent, SENT) / f"{message_id}.json", message)
            except FileExistsError:
                for path in written:
                    path.unlink(missing_ok=True)
                continue
            result: dict[str, Any] = {
                "id": message_id,
                "to": to_field,
                "path": str(written[0]) if len(written) == 1 else [str(p) for p in written],
                "created_at": message["created_at"],
                "thread": message["thread"],
            }
            if warnings:
                result["warnings"] = warnings
            return result
        raise MailboxError("could not allocate a unique message id; try again")

    # inbox

    def inbox(
        self, *, include_read: bool = False, include_expired: bool = False, limit: int = 20
    ) -> dict[str, Any]:
        """``{"agent": <this agent>, "messages": [...]}``, oldest first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise MailboxError("limit must be a positive integer")
        items: list[dict[str, Any]] = []
        report = {"agent": self.agent, "messages": items}
        box = self._box(self.agent, INBOX)
        if not box.is_dir():
            return report
        done = self._box(self.agent, DONE)
        now = _now()
        for path in sorted(box.glob("msg-*.json")):
            if (done / path.name).exists():
                continue  # acked; a crash left the inbox copy behind
            try:
                msg = _load(path)
            except OSError, json.JSONDecodeError:
                continue
            status = status_of(msg, now)
            if status == "expired" and not include_expired:
                continue
            if msg.get("read_at") and not include_read:
                continue
            created = _parse_time(msg["created_at"])
            item = {
                "id": msg["id"],
                "from": msg["from"],
                "subject": msg["subject"],
                "created_at": msg["created_at"],
                "age": format_age((now - created).total_seconds()) if created else None,
                "thread": msg.get("thread"),
                "unread": not msg.get("read_at"),
                "status": status,
            }
            if msg.get("expires_at"):
                item["expires_at"] = msg["expires_at"]
            items.append(item)
            if len(items) >= limit:
                break
        return report

    # read

    def read(self, message_id: str) -> dict[str, Any]:
        validate_id(message_id)
        with self._mutex():
            found = self._find(message_id, (DONE, INBOX, SENT))
            if not found:
                raise MailboxError(f"no message {message_id!r} in {self.agent!r}'s mailbox")
            box, path = found
            msg = _load(path)
            already_read = bool(msg.get("read_at"))
            if box == INBOX and not already_read:
                msg["read_at"] = _iso(_now())
                _replace(path, msg)
        result = {
            "id": msg["id"],
            "from": msg["from"],
            "to": msg["to"],
            "subject": msg["subject"],
            "body": msg["body"],
            "created_at": msg["created_at"],
            "reply_to": msg.get("reply_to"),
            "thread": msg.get("thread"),
            "already_read": already_read,
            "status": status_of(msg),
            "expired": is_expired(msg),
            "mailbox": box,
        }
        if msg.get("expires_at"):
            result["expires_at"] = msg["expires_at"]
        return result

    # ack

    def ack(self, message_id: str) -> dict[str, Any]:
        validate_id(message_id)
        with self._mutex():
            done_path = self._box(self.agent, DONE) / f"{message_id}.json"
            inbox_path = self._box(self.agent, INBOX) / f"{message_id}.json"
            if done_path.is_file():
                inbox_path.unlink(missing_ok=True)
                return {"id": message_id, "acknowledged": True, "already_acked": True}
            if not inbox_path.is_file():
                raise MailboxError(f"no message {message_id!r} in {self.agent!r}'s inbox")
            msg = _load(inbox_path)
            msg["acked_at"] = _iso(_now())
            _replace(done_path, msg)
            inbox_path.unlink()
            _fsync_dir(inbox_path.parent)
        return {"id": message_id, "acknowledged": True, "already_acked": False}
