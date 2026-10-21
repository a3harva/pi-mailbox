"""Command-line interface: `serve` plus parity subcommands that print JSON."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

from . import __version__
from .core import AGENT_ENV, Mailbox, MailboxError

DEFAULT_NUDGE = "check your mailbox"


def _emit(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _mailbox(name: str | None) -> Mailbox:
    name = name or os.environ.get(AGENT_ENV, "").strip()
    if not name:
        raise MailboxError(f"no identity: pass --as/--from NAME or set {AGENT_ENV}")
    return Mailbox(name, allow_owner=True)


def _body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file == "-":
        return sys.stdin.read()
    with open(args.body_file, encoding="utf-8") as fh:
        return fh.read()


def _to(values: list[str]) -> str | list[str]:
    names = [n.strip() for v in values for n in v.split(",") if n.strip()]
    return names[0] if len(names) == 1 else names


def nudge(session: str, text: str = DEFAULT_NUDGE) -> dict[str, Any]:
    """Type `text` + Enter into a tmux session's input. Never fails hard."""
    tmux = shutil.which("tmux")
    if not tmux:
        return {"nudged": False, "session": session, "warning": "tmux is not installed"}
    target = f"={session}"
    exists = subprocess.run([tmux, "has-session", "-t", target], capture_output=True)
    if exists.returncode != 0:
        return {"nudged": False, "session": session, "warning": "no such tmux session"}
    pane = f"{target}:"
    subprocess.run([tmux, "send-keys", "-t", pane, "-l", text], check=True)
    subprocess.run([tmux, "send-keys", "-t", pane, "Enter"], check=True)
    return {"nudged": True, "session": session, "text": text}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-mailbox", description="Durable local mailbox between Pi sessions."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help=f"run the stdio MCP server (identity from {AGENT_ENV})")

    p = sub.add_parser("send", help="send a message")
    p.add_argument(
        "--from", dest="sender", help=f"sender name (default ${AGENT_ENV}); may be 'owner'"
    )
    p.add_argument("--to", action="append", required=True, help="recipient, comma list, or 'all'")
    p.add_argument("--subject", required=True)
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", help="read the body from a file, or '-' for stdin")
    p.add_argument("--reply-to")
    p.add_argument("--ttl", type=int, dest="ttl_seconds", help="time to live in seconds")

    p = sub.add_parser("inbox", help="list messages")
    p.add_argument("--as", dest="agent", help=f"whose inbox (default ${AGENT_ENV})")
    p.add_argument("--all", action="store_true", dest="include_read", help="include read messages")
    p.add_argument("--include-expired", action="store_true")
    p.add_argument("--limit", type=int, default=20)

    for command, help_text in (
        ("read", "show a message and mark it read"),
        ("ack", "acknowledge a message"),
    ):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("id")
        p.add_argument("--as", dest="agent", help=f"whose mailbox (default ${AGENT_ENV})")

    p = sub.add_parser("nudge", help="type a wake-up line into a tmux session")
    p.add_argument("session", help="tmux session name")
    p.add_argument("--text", default=DEFAULT_NUDGE)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        from .server import serve

        serve()
        return 0

    try:
        match args.command:
            case "send":
                result = _mailbox(args.sender).send(
                    _to(args.to),
                    args.subject,
                    _body(args),
                    reply_to=args.reply_to,
                    ttl_seconds=args.ttl_seconds,
                )
            case "inbox":
                result = _mailbox(args.agent).inbox(
                    include_read=args.include_read,
                    include_expired=args.include_expired,
                    limit=args.limit,
                )
            case "read":
                result = _mailbox(args.agent).read(args.id)
            case "ack":
                result = _mailbox(args.agent).ack(args.id)
            case "nudge":
                result = nudge(args.session, args.text)
                if not result["nudged"]:
                    print(f"pi-mailbox: {result['warning']}", file=sys.stderr)
    except (MailboxError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
