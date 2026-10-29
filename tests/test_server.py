"""The real stdio MCP server, driven by the MCP client SDK."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.stdio import StdioServerParameters

EXE = os.path.join(os.path.dirname(sys.executable), "agent-mailbox")
TOOLS = {
    "whoami",
    "agents",
    "set_role",
    "retire",
    "send",
    "inbox",
    "read",
    "defer",
    "close",
    "status",
    "wait",
}


@asynccontextmanager
async def server(root, project, **env):
    params = StdioServerParameters(
        command=EXE,
        args=["serve"],
        cwd=str(project),
        env={"AGENT_MAILBOX_DIR": str(root), "PATH": os.environ["PATH"], **env},
    )
    async with Client(params) as client:
        yield client


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        return {"error": result.content[0].text}
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    return data if data is not None else json.loads(result.content[0].text)


def owner(root, *args):
    return subprocess.run(
        [EXE, *args],
        capture_output=True,
        text=True,
        env={**os.environ, "AGENT_MAILBOX_DIR": str(root)},
    )


def test_tools_and_full_flow(root, project):
    async def go():
        async with server(root, project, CLAUDECODE="1") as a:
            listed = {t.name for t in (await a.list_tools()).tools}
            assert listed >= TOOLS
            me = await call(a, "whoami")
            assert me["provisional"] and me["alias"].startswith("claude-code-my-proj-")
            assert me["mailbox"].startswith("open: ")
            r = await call(a, "set_role", role="refactor-agent", purpose="refactor core")
            assert r["alias"] == "claude-code-my-proj-refactor-agent"
            sent = owner(
                root, "send", "--to", r["alias"], "--subject", "hi", "--body", "yo", "--json"
            )
            assert sent.returncode == 0, sent.stderr
            mid = json.loads(sent.stdout)["id"]
            box = await call(a, "inbox")
            assert [m["id"] for m in box["new"]] == [mid]
            msg = await call(a, "read", id=mid)
            assert msg["trust"] == "peer" and "yo" in msg["body"]
            await call(a, "defer", id=mid, note="mid-task")
            st = json.loads(owner(root, "status", mid, "--json").stdout)
            assert st["recipients"][0]["defer_note"] == "mid-task"
            reply = await call(a, "send", to="owner", subject="re", body="done", reply_to=mid)
            assert reply["closed_original"]
            # every tool result carries the summary line
            for tool, args in [
                ("agents", {}),
                ("status", {"id": reply["id"]}),
                ("close", {"id": mid, "resolution": "done"}),
            ]:
                assert "mailbox" in await call(a, tool, **args)
            err = await call(a, "read", id="nope")
            assert "no message" in err["error"]

    asyncio.run(go())


def test_two_servers_cannot_share_a_role(root, project):
    async def go():
        async with (
            server(root, project, CLAUDECODE="1") as a,
            server(root, project, CLAUDECODE="1") as b,
        ):
            await call(a, "set_role", role="reviewer")
            err = await call(b, "set_role", role="reviewer")
            assert "in use by live session" in err["error"]

    asyncio.run(go())


def test_restart_resumes_role(root, project):
    async def first():
        async with server(root, project, CLAUDECODE="1") as a:
            await call(a, "set_role", role="refactor-agent")

    async def second():
        async with server(root, project, CLAUDECODE="1") as b:
            r = await call(b, "set_role", role="refactor-agent")
            assert r["action"] == "resumed" and r["inherited_open"] == 1

    asyncio.run(first())
    assert (
        owner(
            root,
            "send",
            "--to",
            "claude-code-my-proj-refactor-agent",
            "--subject",
            "x",
            "--body",
            "y",
        ).returncode
        == 0
    )
    asyncio.run(second())


def test_wait_returns_on_new_mail_and_times_out(root, project):
    async def go():
        async with server(root, project, CLAUDECODE="1") as a:
            me = (await call(a, "whoami"))["alias"]
            t0 = time.monotonic()
            r = await call(a, "wait", timeout_seconds=2)
            assert r["timed_out"] and 1.5 < time.monotonic() - t0 < 4
            loop = asyncio.get_running_loop()
            loop.call_later(
                1.5, lambda: owner(root, "send", "--to", me, "--subject", "ping", "--body", "x")
            )
            t0 = time.monotonic()
            r = await call(a, "wait", timeout_seconds=20)
            assert not r["timed_out"] and r["messages"][0]["subject"] == "ping"
            assert time.monotonic() - t0 < 4

    asyncio.run(go())


def test_owner_requested_role_applies_on_next_tool_call(root, project):
    async def go():
        async with server(root, project, CLAUDECODE="1") as a:
            me = (await call(a, "whoami"))["alias"]
            assert owner(root, "set-role", me, "reviewer").returncode == 0
            r = await call(a, "whoami")
            assert r["alias"] == "claude-code-my-proj-reviewer"
            assert r["role_change_requested_by_owner"]["action"] == "created"

    asyncio.run(go())


def test_pi_client_name(root, project):
    async def go():
        # AGENT_MAILBOX_CLIENT: the test runner may itself run under Claude Code.
        async with server(root, project, PI_CODING_AGENT="true", AGENT_MAILBOX_CLIENT="pi") as a:
            assert (await call(a, "whoami"))["alias"].startswith("pi-my-proj-")

    asyncio.run(go())
