import json
import stat
from datetime import timedelta

import pytest

from pi_mailbox import core
from pi_mailbox.core import IdentityInUse, Mailbox, MailboxError


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# FR-2: durable send -----------------------------------------------------------


def test_send_writes_a_complete_private_file(root, agent):
    a, b = agent("pi-main"), agent("pi-2")
    sent = a.send("pi-2", "subject", "body")

    path = root / "pi-2" / "inbox" / f"{sent['id']}.json"
    assert sent["path"] == str(path)
    msg = json.loads(path.read_text())
    assert msg["from"] == "pi-main" and msg["to"] == "pi-2" and msg["body"] == "body"
    for field in (
        "id",
        "from",
        "to",
        "subject",
        "body",
        "created_at",
        "read_at",
        "acked_at",
        "reply_to",
        "thread",
        "ttl_seconds",
    ):
        assert field in msg
    assert mode(path) == 0o600
    for directory in (root, root / "pi-2", root / "pi-2" / "inbox"):
        assert mode(directory) == 0o700
    assert not list((root / "pi-2" / "inbox").glob(".tmp-*"))
    assert (root / "pi-main" / "sent" / f"{sent['id']}.json").is_file()
    assert "warnings" not in sent
    assert b.inbox()["messages"][0]["id"] == sent["id"]


def test_message_to_an_agent_that_never_started_waits_for_it(root, agent):
    a = agent("pi-main")
    sent = a.send("pi-9", "hello", "are you there?")
    assert "never started" in sent["warnings"][0]
    assert "pi-9" not in a.known_agents()

    later = agent("pi-9")  # the agent starts afterwards
    assert [m["id"] for m in later.inbox()["messages"]] == [sent["id"]]
    assert "warnings" not in a.send("pi-9", "again", "now known")


def test_timestamps_are_iso8601_with_timezone(agent):
    sent = agent("a").send("a", "s", "b")
    assert core.datetime.fromisoformat(sent["created_at"]).tzinfo is not None


# FR-3 / FR-4: inbox and read ---------------------------------------------------


def test_inbox_lists_oldest_first_and_hides_read(agent):
    a, b = agent("a"), agent("b")
    first = a.send("b", "one", "1")["id"]
    second = a.send("b", "two", "2")["id"]
    report = b.inbox()
    assert report["agent"] == "b"
    assert [m["id"] for m in report["messages"]] == [first, second]
    assert report["messages"][0]["unread"] is True

    b.read(first)
    assert [m["id"] for m in b.inbox()["messages"]] == [second]
    assert [m["id"] for m in b.inbox(include_read=True)["messages"]] == [first, second]
    assert len(b.inbox(include_read=True, limit=1)["messages"]) == 1


def test_read_marks_read_but_never_deletes(root, agent):
    a, b = agent("a"), agent("b")
    msg_id = a.send("b", "s", "the body")["id"]

    first = b.read(msg_id)
    assert first["body"] == "the body" and first["already_read"] is False
    assert first["status"] == "read"
    assert (root / "b" / "inbox" / f"{msg_id}.json").is_file()

    again = b.read(msg_id)
    assert again["already_read"] is True


def test_read_finds_own_sent_copy_but_not_other_mailboxes(agent):
    a, c = agent("a"), agent("c")
    msg_id = a.send("b", "s", "b")["id"]
    assert a.read(msg_id)["mailbox"] == "sent"
    with pytest.raises(MailboxError, match="no message"):
        c.read(msg_id)


# FR-5: threads ------------------------------------------------------------------


def test_replies_share_the_thread_of_the_first_message(agent):
    a, b = agent("a"), agent("b")
    first = a.send("b", "q", "question")
    reply = b.send("a", "re: q", "answer", reply_to=first["id"])
    third = a.send("b", "re: re: q", "thanks", reply_to=reply["id"])
    assert first["thread"] == first["id"]
    assert reply["thread"] == third["thread"] == first["id"]
    assert b.read(third["id"])["reply_to"] == reply["id"]


def test_reply_to_unknown_message_falls_back_with_a_warning(agent):
    a = agent("a")
    missing = core.new_id()
    sent = a.send("a", "s", "b", reply_to=missing)
    assert sent["thread"] == missing
    assert any("reply_to" in w for w in sent["warnings"])


# FR-6: acknowledge ----------------------------------------------------------------


def test_ack_moves_to_done_and_is_idempotent(root, agent):
    a, b = agent("a"), agent("b")
    msg_id = a.send("b", "s", "b")["id"]

    assert b.ack(msg_id) == {"id": msg_id, "acknowledged": True, "already_acked": False}
    assert b.ack(msg_id) == {"id": msg_id, "acknowledged": True, "already_acked": True}
    assert not (root / "b" / "inbox" / f"{msg_id}.json").exists()
    done = json.loads((root / "b" / "done" / f"{msg_id}.json").read_text())
    assert done["acked_at"]
    assert b.inbox(include_read=True)["messages"] == []
    assert b.read(msg_id)["status"] == "acked"


def test_ack_of_unknown_or_foreign_message_is_an_error(agent):
    a, b = agent("a"), agent("b")
    msg_id = a.send("b", "s", "b")["id"]
    with pytest.raises(MailboxError):
        a.ack(msg_id)  # only in a's sent/, not its inbox
    with pytest.raises(MailboxError):
        b.ack(core.new_id())


def test_crash_between_ack_rename_and_unlink_is_harmless(root, agent):
    a, b = agent("a"), agent("b")
    msg_id = a.send("b", "s", "b")["id"]
    inbox_file = root / "b" / "inbox" / f"{msg_id}.json"
    saved = inbox_file.read_bytes()
    b.ack(msg_id)
    inbox_file.write_bytes(saved)  # simulate the leftover inbox copy

    assert b.inbox(include_read=True)["messages"] == []
    assert b.ack(msg_id)["already_acked"] is True
    assert not inbox_file.exists()


# FR-7: broadcast ---------------------------------------------------------------------


def test_broadcast_delivers_one_independent_copy_per_agent(root, agent):
    a, b, c = agent("a"), agent("b"), agent("c")
    Mailbox("owner", root, allow_owner=True).register()
    sent = a.send("all", "hello all", "body")

    assert sent["to"] == ["b", "c"]
    assert len(sent["path"]) == 2
    assert not (root / "a" / "inbox" / f"{sent['id']}.json").exists()
    assert not (root / "owner" / "inbox" / f"{sent['id']}.json").exists()

    b.ack(sent["id"])
    assert c.inbox()["messages"][0]["id"] == sent["id"]
    assert c.read(sent["id"])["to"] == ["b", "c"]


def test_explicit_recipient_list(agent):
    a = agent("a")
    sent = a.send(["b", "c", "b"], "s", "b")
    assert sent["to"] == ["b", "c"]
    with pytest.raises(MailboxError, match="'all'"):
        a.send(["b", "all"], "s", "b")


def test_broadcast_with_nobody_else_is_an_error(agent):
    with pytest.raises(MailboxError, match="no other known agents"):
        agent("lonely").send("all", "s", "b")


# FR-8: expiry ----------------------------------------------------------------------


def test_expired_messages_are_hidden_kept_and_flagged(root, agent, monkeypatch):
    a, b = agent("a"), agent("b")
    live = a.send("b", "live", "x")["id"]
    short = a.send("b", "short", "x", ttl_seconds=60)["id"]
    assert b.inbox()["messages"][1]["expires_at"]

    later = core._now() + timedelta(seconds=120)
    monkeypatch.setattr(core, "_now", lambda: later)

    assert [m["id"] for m in b.inbox()["messages"]] == [live]
    expired = b.inbox(include_expired=True)["messages"]
    assert [(m["id"], m["status"]) for m in expired] == [(live, "unread"), (short, "expired")]
    msg = b.read(short)
    assert msg["expired"] is True and msg["status"] == "expired"
    assert (root / "b" / "inbox" / f"{short}.json").is_file()  # never deleted


@pytest.mark.parametrize("ttl", [0, -5, 10**9, 1.5, True, "60"])
def test_bad_ttl_is_rejected(agent, ttl):
    with pytest.raises(MailboxError, match="ttl_seconds"):
        agent("a").send("a", "s", "b", ttl_seconds=ttl)


# validation and security ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "../etc", "a/b", "UPPER", "-lead", "x" * 33, "all", "owner"])
def test_bad_agent_names_are_rejected(root, name):
    with pytest.raises(MailboxError):
        Mailbox(name, root)


@pytest.mark.parametrize(
    "bad", ["../../etc/passwd", "msg-1", "", "msg-20261021T172003123456Z-zzzzzzzz"]
)
def test_bad_ids_are_rejected(agent, bad):
    with pytest.raises(MailboxError, match="invalid message id"):
        agent("a").read(bad)


def test_size_limits(agent):
    a = agent("a")
    with pytest.raises(MailboxError, match="body"):
        a.send("a", "s", "x" * (core.MAX_BODY_BYTES + 1))
    with pytest.raises(MailboxError, match="subject"):
        a.send("a", "x" * (core.MAX_SUBJECT_CHARS + 1), "b")
    with pytest.raises(MailboxError, match="subject"):
        a.send("a", "   ", "b")
    a.send("a", "s", "é" * (core.MAX_BODY_BYTES // 2))  # exactly at the byte limit


def test_owner_can_send_from_cli_identity_and_is_never_in_broadcasts(root, agent):
    b = agent("b")
    owner = Mailbox("owner", root, allow_owner=True)
    msg_id = owner.send("b", "note", "from the human")["id"]
    assert b.read(msg_id)["from"] == "owner"
    b.send("owner", "re: note", "got it", reply_to=msg_id)
    assert owner.inbox()["messages"][0]["thread"] == msg_id


def test_ids_match_the_format_and_strictly_increase():
    ids = [core.new_id() for _ in range(1000)]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert all(core.validate_id(i) == i for i in ids)
    # a clock that steps backwards still yields increasing ids
    assert core.new_id(core.datetime(2000, 1, 1, tzinfo=core.UTC)) > ids[-1]


@pytest.mark.parametrize(
    ("seconds", "text"), [(0, "0s"), (59, "59s"), (60, "1m"), (7200, "2h"), (86400 * 3, "3d")]
)
def test_format_age(seconds, text):
    assert core.format_age(seconds) == text


# FR-1: identity lock ---------------------------------------------------------------------


def test_a_second_live_holder_of_a_name_is_refused(root):
    first = Mailbox("pi-2", root).acquire_identity()
    try:
        with pytest.raises(IdentityInUse, match="already in use"):
            Mailbox("pi-2", root).acquire_identity()
        Mailbox("pi-3", root).acquire_identity().release()
    finally:
        first.release()
    Mailbox("pi-2", root).acquire_identity().release()  # free again after release
