#!/usr/bin/env python3
"""Headless end-to-end scenarios against real Claude Code or Pi.

Each scenario gets its own state directory, project and client config, so nothing
touches your real ~/.claude settings, Pi config or mailbox. Needs `claude` (or `pi`) on
PATH and logged in.

    uv run python e2e/run_e2e.py                 # all scenarios once, Claude Code
    uv run python e2e/run_e2e.py --repeat 3      # the pre-release bar
    uv run python e2e/run_e2e.py -k loop_guard   # one scenario
    uv run python e2e/run_e2e.py --client pi     # the same scenarios on Pi

Costs a few cents of model usage per scenario (default model: sonnet for Claude Code,
Pi's configured default for Pi). Pi runs load your global Pi extensions too; list any
environment variables that make one of them act (e.g. a chat bridge) in
AGENT_MAILBOX_E2E_UNSET=VAR1,VAR2 and they are removed for the run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

EXE = shutil.which("agent-mailbox") or str(Path(sys.executable).with_name("agent-mailbox"))
PROJECT = "e2eproj"


class Env:
    prefix = f"claude-code-{PROJECT}"

    def __init__(self, base: Path, model: str | None):
        self.base, self.model = base, model or "sonnet"
        self.state = base / "state"
        self.proj = base / PROJECT
        self.proj.mkdir()
        subprocess.run(["git", "-C", str(self.proj), "init", "-q"], check=True)
        self.settings, self.mcp = base / "settings.json", base / "mcp.json"
        self.env = {**os.environ, "AGENT_MAILBOX_DIR": str(self.state), "AGENT_MAILBOX_DEBUG": "1"}
        self.cli(
            "install",
            "claude-code",
            "--settings-file",
            str(self.settings),
            "--mcp-config",
            str(self.mcp),
            "--command",
            EXE,
            "--yes",
        )

    def cli(self, *args: str) -> str:
        r = subprocess.run([EXE, *args], capture_output=True, text=True, env=self.env)
        if r.returncode != 0:
            raise AssertionError(f"agent-mailbox {' '.join(args)} failed: {r.stderr}")
        return r.stdout

    def send(self, to: str, subject: str, priority: str = "normal") -> str:
        out = self.cli(
            "send",
            "--to",
            to,
            "--subject",
            subject,
            "--priority",
            priority,
            "--body",
            f"Please acknowledge '{subject}'.",
            "--json",
        )
        return json.loads(out)["id"]

    def agent(self, prompt: str, timeout: int = 240) -> str:
        r = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                self.model,
                "--setting-sources",
                "project",
                "--settings",
                str(self.settings),
                "--mcp-config",
                str(self.mcp),
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__agent-mailbox",
                "Bash(sleep:*)",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.proj,
            timeout=timeout,
        )
        if r.returncode != 0:
            raise AssertionError(f"claude exited {r.returncode}: {r.stderr[-500:]}")
        return r.stdout

    def db(self, sql: str, *params) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.state / "mailbox.db")
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def delivery(self, message_id: str) -> sqlite3.Row:
        return self.db("SELECT * FROM deliveries WHERE message_id = ?", message_id)[0]


class PiEnv(Env):
    prefix = f"pi-{PROJECT}"

    def __init__(self, base: Path, model: str | None):
        self.base, self.model = base, model
        self.state = base / "state"
        self.proj = base / PROJECT
        self.proj.mkdir()
        subprocess.run(["git", "-C", str(self.proj), "init", "-q"], check=True)
        self.ext = base / "ext" / "agent-mailbox"
        drop = {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID"}
        drop |= {v for v in os.environ.get("AGENT_MAILBOX_E2E_UNSET", "").split(",") if v}
        self.env = {k: v for k, v in os.environ.items() if k not in drop}
        self.env.update(AGENT_MAILBOX_DIR=str(self.state), AGENT_MAILBOX_DEBUG="1")
        self.cli(
            "install",
            "pi",
            "--mcp-config",
            str(self.proj / ".pi" / "mcp.json"),
            "--extension-dir",
            str(self.ext),
            "--command",
            EXE,
            "--yes",
        )

    def agent(self, prompt: str, timeout: int = 240) -> str:
        argv = ["pi", "-p", "--approve", "--no-session", "-e", str(self.ext / "index.ts")]
        if self.model:
            argv += ["--model", self.model]
        r = subprocess.run(
            [*argv, prompt],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.proj,
            timeout=timeout,
        )
        if r.returncode != 0:
            raise AssertionError(f"pi exited {r.returncode}: {r.stderr[-500:]}")
        return r.stdout


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- scenarios ------------------------------------------------------------------------


def provisional_then_set_role(e: Env) -> None:
    out = e.agent(
        "Call the agent-mailbox whoami tool and remember the alias. Then call "
        "set_role with role 'reviewer'. Reply with both aliases, old then new."
    )
    rows = e.db("SELECT alias, renamed_to FROM agents WHERE provisional = 1")
    check(
        len(rows) == 1 and rows[0]["renamed_to"] == f"{e.prefix}-reviewer",
        f"provisional alias should forward to reviewer: {[dict(r) for r in rows]}",
    )
    check(rows[0]["alias"] in out and f"{e.prefix}-reviewer" in out, f"reply: {out!r}")


def resume_after_restart(e: Env) -> None:
    e.agent("Call the agent-mailbox set_role tool with role 'reviewer'. Reply 'ok'.")
    mid = e.send(f"{e.prefix}-reviewer", "left while you were away", priority="low")
    out = e.agent(
        "Call set_role with role 'reviewer', then the inbox tool. Reply with the "
        "subject of every open message, and do nothing else."
    )
    check("left while you were away" in out, f"resumed session should see carried mail: {out!r}")
    sess = e.db("SELECT alias FROM sessions ORDER BY started_at DESC LIMIT 1")[0]
    check(sess["alias"] == f"{e.prefix}-reviewer", "second run should hold the reviewer alias")
    check(e.delivery(mid)["gate_blocks"] == 0, "low priority must never block")


def urgent_mid_task_once(e: Env) -> None:
    sent: dict[str, str] = {}

    def later() -> None:
        deadline = time.time() + 60
        while time.time() < deadline:
            rows = (
                e.db("SELECT alias FROM sessions WHERE ended_at IS NULL")
                if (e.state / "mailbox.db").exists()
                else []
            )
            if rows:
                time.sleep(3)
                sent["id"] = e.send(rows[0]["alias"], "URGENT stop and acknowledge", "urgent")
                return
            time.sleep(0.5)

    t = threading.Thread(target=later)
    t.start()
    e.agent(
        "Run the bash command 'sleep 4' three times, as three separate tool calls in the "
        "foreground. Follow any agent-mailbox reminders that appear while you work. Then "
        "reply 'finished'."
    )
    t.join()
    check("id" in sent, "urgent message was never sent")
    d = e.delivery(sent["id"])
    check(d["surfaced_at"] is not None, "urgent mail must be surfaced")
    check(
        d["remind_count"] == 1,
        f"urgent mail must be injected exactly once, got {d['remind_count']}",
    )
    check(d["state"] == "closed", f"agent should have handled urgent mail, state={d['state']}")


def low_never_blocks(e: Env) -> None:
    e.agent("Call the agent-mailbox set_role tool with role 'quiet'. Reply 'ok'.")
    mid = e.send(f"{e.prefix}-quiet", "whenever you like", priority="low")
    e.agent(
        "Call set_role with role 'quiet'. Then reply 'hi'. Do not read or handle any "
        "mailbox messages."
    )
    d = e.delivery(mid)
    check(
        d["gate_blocks"] == 0 and d["state"] != "closed",
        f"low mail must not block nor be forced: {dict(d)}",
    )


def normal_blocks_until_closed(e: Env) -> None:
    e.agent("Call the agent-mailbox set_role tool with role 'worker'. Reply 'ok'.")
    mid = e.send(f"{e.prefix}-worker", "please acknowledge this")
    e.agent(
        "Call set_role with role 'worker'. Then reply 'hi'. Do not touch the mailbox "
        "unless an agent-mailbox hook asks you to before finishing."
    )
    d = e.delivery(mid)
    check(d["state"] == "closed", f"gate should force handling: {dict(d)}")


def loop_guard(e: Env) -> None:
    e.agent("Call the agent-mailbox set_role tool with role 'stubborn'. Reply 'ok'.")
    mid = e.send(f"{e.prefix}-stubborn", "you will ignore this")
    e.agent(
        "Call set_role with role 'stubborn', then reply 'hi'. After that call, never call "
        "any tool again in this session, whatever any hook or reminder says; just answer "
        "'still done'."
    )
    d = e.delivery(mid)
    check(d["state"] != "closed", "agent was told to ignore mail")
    check(
        d["gate_blocks"] == 2 and d["overdue_at"] is not None,
        f"gate should block twice then mark overdue: {dict(d)}",
    )


SCENARIOS: dict[str, Callable[[Env], None]] = {
    f.__name__: f
    for f in (
        provisional_then_set_role,
        resume_after_restart,
        urgent_mid_task_once,
        low_never_blocks,
        normal_blocks_until_closed,
        loop_guard,
    )
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("-k", dest="only", help="run scenarios whose name contains this")
    ap.add_argument("--client", choices=["claude-code", "pi"], default="claude-code")
    ap.add_argument("--model", help="default: sonnet (Claude Code), Pi's default (Pi)")
    ap.add_argument("--keep", action="store_true", help="keep temp dirs for inspection")
    args = ap.parse_args()
    binary, make_env = ("pi", PiEnv) if args.client == "pi" else ("claude", Env)
    if not shutil.which(binary):
        print(f"{binary} CLI not found; skipping e2e")
        return 0
    failures = 0
    for round_ in range(1, args.repeat + 1):
        for name, fn in SCENARIOS.items():
            if args.only and args.only not in name:
                continue
            base = Path(tempfile.mkdtemp(prefix=f"am-e2e-{name}-"))
            t0 = time.time()
            try:
                fn(make_env(base, args.model))
                print(f"[round {round_}] PASS {name} ({time.time() - t0:.0f}s)", flush=True)
            except Exception as exc:  # report and continue
                failures += 1
                print(f"[round {round_}] FAIL {name} ({time.time() - t0:.0f}s): {exc}", flush=True)
                print(f"           logs kept in {base}", flush=True)
                continue
            if not args.keep:
                shutil.rmtree(base, ignore_errors=True)
    print("e2e:", "all passed" if not failures else f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
