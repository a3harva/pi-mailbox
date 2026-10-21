"""The acceptance scenario: two real `pi-mailbox serve` processes talking over stdio."""

import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


def server(root: Path, agent: str | None) -> Client:
    env = {"PI_MAILBOX_DIR": str(root)}
    if agent:
        env["PI_MAILBOX_AGENT"] = agent
    return Client(
        StdioServerParameters(
            command=sys.executable, args=["-m", "pi_mailbox.cli", "serve"], env=env
        )
    )


async def call(client: Client, tool: str, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


def test_handoff_and_reply_between_two_sessions(root: Path):
    async def scenario():
        async with server(root, "pi-main") as main, server(root, "pi-2") as pi2:
            assert {t.name for t in (await main.list_tools()).tools} == {
                "send",
                "inbox",
                "read",
                "ack",
            }

            task = await call(
                main,
                "send",
                to="pi-2",
                subject="Workday: backend role",
                body="Answer the three custom questions. Report back with the screenshot path.",
            )
            assert "warnings" not in task  # pi-2 is running, so it is known

            inbox = await call(pi2, "inbox")
            assert inbox["agent"] == "pi-2"
            [item] = inbox["messages"]
            assert item["id"] == task["id"] and item["from"] == "pi-main" and item["unread"]

            msg = await call(pi2, "read", id=task["id"])
            assert msg["body"].startswith("Answer the three") and not msg["already_read"]

            reply = await call(
                pi2,
                "send",
                to="pi-main",
                subject="re: Workday: backend role",
                body="Submitted. Evidence temp/submitted.png.",
                reply_to=task["id"],
            )
            assert (await call(pi2, "ack", id=task["id"]))["already_acked"] is False
            assert (await call(pi2, "ack", id=task["id"]))["already_acked"] is True
            assert (await call(pi2, "inbox", include_read=True))["messages"] == []

            [back] = (await call(main, "inbox"))["messages"]
            assert back["id"] == reply["id"] and back["from"] == "pi-2"
            assert back["thread"] == task["id"]
            answer = await call(main, "read", id=reply["id"])
            assert answer["reply_to"] == task["id"]

    asyncio.run(scenario())


def test_missing_identity_errors_on_every_tool_without_touching_disk(root: Path):
    async def scenario():
        async with server(root, None) as anon:
            result = await anon.call_tool("inbox", {})
            assert result.is_error
            assert "PI_MAILBOX_AGENT is not set" in result.content[0].text

    asyncio.run(scenario())
    assert not root.exists()


def test_duplicate_identity_is_refused_loudly(root: Path):
    async def scenario():
        async with server(root, "pi-2") as first, server(root, "pi-2") as second:
            await call(first, "inbox")
            result = await second.call_tool("inbox", {})
            assert result.is_error
            assert "already in use" in result.content[0].text

    asyncio.run(scenario())
