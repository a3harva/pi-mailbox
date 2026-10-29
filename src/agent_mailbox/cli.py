"""Command line: the MCP server, the hook entry point, and owner commands."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import Any

from . import __version__
from .db import OWNER, connect, now
from .errors import MailboxError
from .hooks import EVENTS, PI_EVENTS
from .paths import UnsafeStateDir, state_dir


def _emit(data: Any, as_json: bool, text: str | None = None) -> None:
    if as_json or text is None:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(text)


def _inbox_text(result: dict[str, Any]) -> str:
    lines = [f"inbox for {result['alias']}"]
    for group in ("new", "deferred", "read_open", "closed"):
        items = result.get(group) or []
        if not items:
            continue
        lines.append(f"\n{group} ({len(items)})")
        for m in items:
            extra = f"  deferred: {m['defer_note']}" if group == "deferred" else ""
            lines.append(
                f"  {m['priority']:<6} {m['age']:>4}  {m['from']:<40} {m['subject']}\n"
                f"         id {m['id']}{extra}"
            )
    if len(lines) == 1:
        lines.append("  (nothing open)")
    return "\n".join(lines)


def _agents_text(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no agents)"
    lines = [f"{'alias':<48} {'live':<5} {'open':>4}  purpose"]
    for a in rows:
        lines.append(
            f"{a['alias']:<48} {'yes' if a['live'] else 'no':<5} {a['open']['total']:>4}  "
            f"{a['purpose'] or ''}"
        )
    return "\n".join(lines)


def dashboard(conn, root, overdue_only: bool = False) -> dict[str, Any]:
    from .identity import list_agents, live_session_row
    from .mail import age, iso, open_deliveries

    t = now()
    rows = list_agents(conn, root)
    owner = {"alias": OWNER, "live": False, "role": "owner", "purpose": "you (CLI)"}
    out = []
    for a in [owner, *rows]:
        opened = open_deliveries(conn, a["alias"])
        overdue = [r for r in opened if r["overdue_at"]]
        if overdue_only and not overdue:
            continue
        sess = live_session_row(conn, root, a["alias"]) if a["alias"] != OWNER else None
        out.append(
            {
                "alias": a["alias"],
                "live": a["live"],
                "turn_state": sess["turn_state"] if sess else None,
                "open": {
                    p: sum(1 for r in opened if r["priority"] == p)
                    for p in ("urgent", "normal", "low")
                },
                "oldest_open": age(t - min(r["created_at"] for r in opened)) if opened else None,
                "deferred": [
                    {"id": r["message_id"], "subject": r["subject"], "note": r["defer_note"]}
                    for r in opened
                    if r["state"] == "deferred"
                ],
                "overdue": [
                    {
                        "id": r["message_id"],
                        "subject": r["subject"],
                        "from": r["from_alias"],
                        "overdue_since": iso(r["overdue_at"]),
                    }
                    for r in overdue
                ],
            }
        )
    return {"agents": out}


def _dashboard_text(data: dict[str, Any]) -> str:
    lines = []
    for a in data["agents"]:
        o = a["open"]
        state = ("live/" + (a["turn_state"] or "?")) if a["live"] else "offline"
        mark = "  <- you" if a["alias"] == OWNER else ""
        lines.append(
            f"{a['alias']:<48} {state:<13} urgent {o['urgent']} normal {o['normal']} "
            f"low {o['low']}  oldest {a['oldest_open'] or '-'}{mark}"
        )
        for d in a["deferred"]:
            lines.append(f"    deferred: {d['subject']} ({d['note']})  id {d['id']}")
        for d in a["overdue"]:
            lines.append(f"    OVERDUE:  {d['subject']} from {d['from']}  id {d['id']}")
    return "\n".join(lines) or "(nothing)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-mailbox", description="Durable local mailbox between coding agents."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the stdio MCP server (one per client session)")

    p = sub.add_parser("hook", help="client hook entry point (reads JSON on stdin)")
    p.add_argument("client", choices=["claude", "pi"])
    p.add_argument("event", choices=sorted(set(EVENTS) | set(PI_EVENTS)))

    def owner_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--as", dest="as_alias", help="act as another alias (debugging only)")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    p = owner_cmd("send", "send a message (as owner)")
    p.add_argument("--to", action="append", required=True, help="alias, comma list, or 'all'")
    p.add_argument("--subject", required=True)
    body = p.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", help="read the body from a file, or '-' for stdin")
    p.add_argument("--priority", choices=["urgent", "normal", "low"], default="normal")
    p.add_argument("--expects-reply", action="store_true")
    p.add_argument("--reply-to")
    p.add_argument("--keep-open", action="store_true")
    p.add_argument("--ttl", type=int, dest="ttl_seconds")

    p = owner_cmd("inbox", "list open messages")
    p.add_argument("--closed", action="store_true", help="also list recently closed")
    p.add_argument("--limit", type=int, default=20)

    p = owner_cmd("read", "show a message and mark it read")
    p.add_argument("id")

    p = owner_cmd("defer", "defer a message")
    p.add_argument("id")
    p.add_argument("--note", required=True)
    p.add_argument("--until", default="task_end")

    p = owner_cmd("close", "close (acknowledge) a message")
    p.add_argument("id")
    p.add_argument(
        "--resolution", default="done", choices=["done", "replied", "declined", "no_action"]
    )
    p.add_argument("--note")

    p = owner_cmd("status", "dashboard of every agent, or one sent message's status")
    p.add_argument("id", nargs="?")
    p.add_argument("--overdue-only", action="store_true")

    p = owner_cmd("agents", "list agents")
    p.add_argument("--project")

    p = owner_cmd("set-role", "ask a live session to take a role")
    p.add_argument("target", help="session id or current alias of a live session")
    p.add_argument("role")
    p.add_argument("--purpose")

    p = owner_cmd("retire", "retire an offline durable alias")
    p.add_argument("alias")

    for name in ("install", "uninstall", "doctor"):
        p = sub.add_parser(name, help=f"{name} the Claude Code or Pi integration")
        p.add_argument("client", choices=["claude-code", "pi"])
        p.add_argument("--project", action="store_true", help="use ./.claude/settings.json")
        p.add_argument("--settings-file", help="explicit Claude Code settings file")
        p.add_argument(
            "--mcp-config",
            help="MCP config file (Claude Code: instead of `claude mcp add`; "
            "Pi: default ~/.pi/agent/mcp.json)",
        )
        p.add_argument(
            "--extension-dir", help="Pi extension directory (default: ~/.pi/agent/extensions/…)"
        )
        if name == "install":
            p.add_argument(
                "--command", dest="install_command", help="how to run agent-mailbox (default: auto)"
            )
        if name != "doctor":
            p.add_argument("--yes", action="store_true", help="apply without asking")
    return parser


def _alias(args: argparse.Namespace) -> str:
    if args.as_alias:
        print(f"agent-mailbox: acting as {args.as_alias!r} (debugging)", file=sys.stderr)
        return args.as_alias
    return OWNER


def _body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file == "-":
        return sys.stdin.read()
    with open(args.body_file, encoding="utf-8") as fh:
        return fh.read()


def _owner(conn, root, args: argparse.Namespace) -> tuple[Any, str | None]:
    from . import mail, wake
    from .identity import list_agents, request_role, retire_offline

    me = _alias(args)
    match args.command:
        case "send":
            to = [n.strip() for v in args.to for n in v.split(",") if n.strip()]
            result = mail.send(
                conn,
                root,
                mail.Sender(me, None, "cli"),
                to[0] if len(to) == 1 else to,
                args.subject,
                _body(args),
                priority=args.priority,
                expects_reply=args.expects_reply,
                reply_to=args.reply_to,
                ttl_seconds=args.ttl_seconds,
                keep_open=args.keep_open,
            )
            woke = wake.maybe_wake(conn, root, result["to"], args.priority)
            if woke:
                result["wake"] = woke
            return result, f"sent {result['id']} to {', '.join(result['to'])}" + "".join(
                f"\nwarning: {w}" for w in result.get("warnings", [])
            )
        case "inbox":
            result = mail.inbox(conn, me, include_closed=args.closed, limit=args.limit)
            return result, _inbox_text(result)
        case "read":
            result = mail.read(conn, me, args.id)
            return result, (
                f"from {result['from']} ({result['via']}) to {', '.join(result['to'])}\n"
                f"subject: {result['subject']}\npriority: {result['priority']}\n\n"
                f"{result['body']}"
            )
        case "defer":
            return mail.defer(conn, me, args.id, args.until, args.note), None
        case "close":
            return mail.close(conn, me, args.id, args.resolution, args.note), None
        case "status":
            if args.id:
                return mail.status(conn, me, args.id), None
            data = dashboard(conn, root, args.overdue_only)
            return data, _dashboard_text(data)
        case "agents":
            rows = list_agents(conn, root, args.project)
            return {"agents": rows}, _agents_text(rows)
        case "set-role":
            return request_role(conn, root, args.target, args.role, args.purpose), None
        case "retire":
            return retire_offline(conn, root, args.alias), None
    raise MailboxError(f"unknown command {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "serve":
        from .server import serve

        return serve()

    if args.command == "hook":
        from .hooks import run

        out = run(args.client, args.event, sys.stdin.read())
        if out:
            print(out)
        return 0

    if args.command in ("install", "uninstall", "doctor"):
        from . import install as inst

        try:
            if args.client == "pi":
                pi_kwargs = dict(mcp_config=args.mcp_config, extension_dir=args.extension_dir)
                if args.command == "install":
                    command = shlex.split(args.install_command) if args.install_command else None
                    return inst.install_pi(command=command, yes=args.yes, **pi_kwargs)
                if args.command == "uninstall":
                    return inst.uninstall_pi(yes=args.yes, **pi_kwargs)
                return inst.doctor_pi(**pi_kwargs)
            kwargs = dict(
                project=args.project, settings_file=args.settings_file, mcp_config=args.mcp_config
            )
            if args.command == "install":
                command = shlex.split(args.install_command) if args.install_command else None
                return inst.install(command=command, yes=args.yes, **kwargs)
            if args.command == "uninstall":
                return inst.uninstall(yes=args.yes, **kwargs)
            return inst.doctor(**kwargs)
        except inst.InstallError as exc:
            print(f"agent-mailbox: {exc}", file=sys.stderr)
            return 1

    root = state_dir()
    try:
        conn = connect(root)
        try:
            result, text = _owner(conn, root, args)
        finally:
            conn.close()
    except (MailboxError, UnsafeStateDir, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    _emit(result, args.json, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
