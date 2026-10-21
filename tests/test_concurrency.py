"""FR-10: many processes sending at the same instant lose and corrupt nothing."""

import json
import multiprocessing as mp
from pathlib import Path

from pi_mailbox.core import Mailbox

SENDERS = 8
PER_SENDER = 25


def _send_many(root: str, sender: str, barrier) -> list[str]:
    box = Mailbox(sender, Path(root))
    barrier.wait()
    return [box.send("target", f"{sender} #{i}", f"body {i}")["id"] for i in range(PER_SENDER)]


def test_parallel_senders_lose_nothing(root: Path):
    Mailbox("target", root).register()
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager, ctx.Pool(SENDERS) as pool:
        barrier = manager.Barrier(SENDERS)
        results = pool.starmap(_send_many, [(str(root), f"s{n}", barrier) for n in range(SENDERS)])

    ids = [i for batch in results for i in batch]
    assert len(ids) == len(set(ids)) == SENDERS * PER_SENDER

    inbox = root / "target" / "inbox"
    files = sorted(inbox.glob("msg-*.json"))
    assert len(files) == SENDERS * PER_SENDER
    assert not list(inbox.glob(".tmp-*"))
    for path in files:
        assert json.loads(path.read_text())["id"] == path.stem

    listed = Mailbox("target", root).inbox(limit=10_000)["messages"]
    assert {m["id"] for m in listed} == set(ids)


def _read_and_ack(root: str, msg_id: str, barrier) -> bool:
    box = Mailbox("target", Path(root))
    barrier.wait()
    box.read(msg_id)
    return box.ack(msg_id)["already_acked"]


def test_concurrent_acks_handle_a_message_exactly_once(root: Path):
    Mailbox("target", root).register()
    msg_id = Mailbox("s", root).send("target", "s", "b")["id"]
    ctx = mp.get_context("spawn")
    with ctx.Manager() as manager, ctx.Pool(6) as pool:
        barrier = manager.Barrier(6)
        outcomes = pool.starmap(_read_and_ack, [(str(root), msg_id, barrier)] * 6)
    assert outcomes.count(False) == 1  # exactly one ack did the work
    assert not (root / "target" / "inbox" / f"{msg_id}.json").exists()
    assert (root / "target" / "done" / f"{msg_id}.json").is_file()
