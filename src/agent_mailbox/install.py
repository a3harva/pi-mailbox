"""`install` / `uninstall` / `doctor` for Claude Code and Pi.

Claude Code: our hook entries are recognised by their command containing
``agent-mailbox hook claude``; nothing else in the settings file is ever touched.
Pi: one `agent-mailbox` entry in ``~/.pi/agent/mcp.json`` and the extension in
``~/.pi/agent/extensions/agent-mailbox/``.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HOOK_MARK = "agent-mailbox hook claude"
SERVER_NAME = "agent-mailbox"
HOOK_EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt-submit",
    "PostToolUse": "post-tool-use",
    "Stop": "stop",
}
HOOK_TIMEOUT = 5


class InstallError(RuntimeError):
    pass


def default_command() -> list[str]:
    exe = shutil.which("agent-mailbox")
    if exe:
        return [exe]
    return ["uvx", "agent-mailbox"]


def settings_path(project: bool, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if project:
        return Path.cwd() / ".claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def _load(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}, text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{path} is not valid JSON ({exc}); not touching it") from None
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object; not touching it")
    return data, text


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _is_ours(hook: Any) -> bool:
    return isinstance(hook, dict) and HOOK_MARK in str(hook.get("command", ""))


def _strip_ours(data: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(json.dumps(data))  # deep copy
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                remaining = [h for h in group["hooks"] if not _is_ours(h)]
                if not remaining and len(remaining) != len(group["hooks"]):
                    continue  # the group only held our hook
                group["hooks"] = remaining
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del data["hooks"]
    return data


def with_hooks(data: dict[str, Any], command: list[str]) -> dict[str, Any]:
    data = _strip_ours(data)
    hooks = data.setdefault("hooks", {})
    base = " ".join(shlex.quote(c) for c in command)
    for event, slug in HOOK_EVENTS.items():
        hooks.setdefault(event, []).append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{base} hook claude {slug}",
                        "timeout": HOOK_TIMEOUT,
                    }
                ]
            }
        )
    return data


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise InstallError("not a terminal; re-run with --yes to apply the changes shown")
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def _apply(path: Path, old_text: str, new_data: dict[str, Any], yes: bool, out) -> bool:
    new_text = _dump(new_data)
    if old_text == new_text or (not old_text.strip() and not new_data):
        print(f"{path}: no changes", file=out)
        return False
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"{path} (current)",
        tofile=f"{path} (new)",
    )
    print("".join(diff), file=out)
    if not _confirm(f"Write these changes to {path}?", yes):
        print("aborted; nothing written", file=out)
        return False
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".agent-mailbox.bak"))
    _write_atomic(path, new_text)
    print(f"{path}: updated (backup: {path.name}.agent-mailbox.bak)", file=out)
    return True


# --- MCP registration -------------------------------------------------------------------


def _mcp_entry(command: list[str]) -> dict[str, Any]:
    return {"command": command[0], "args": [*command[1:], "serve"]}


def register_mcp(command: list[str], mcp_config: str | None, yes: bool, out) -> None:
    if mcp_config:
        path = Path(mcp_config).expanduser()
        data, text = _load(path)
        servers = data.setdefault("mcpServers", {})
        servers[SERVER_NAME] = _mcp_entry(command)
        _apply(path, text, data, yes, out)
        return
    claude = shutil.which("claude")
    if not claude:
        print(
            "claude CLI not found; add this MCP server to Claude Code yourself:\n"
            + json.dumps({"mcpServers": {SERVER_NAME: _mcp_entry(command)}}, indent=2),
            file=out,
        )
        return
    if subprocess.run([claude, "mcp", "get", SERVER_NAME], capture_output=True).returncode == 0:
        print(f"MCP server {SERVER_NAME!r}: already registered", file=out)
        return
    argv = [claude, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *command, "serve"]
    print("will run: " + " ".join(shlex.quote(a) for a in argv), file=out)
    if _confirm("Register the MCP server?", yes):
        subprocess.run(argv, check=True)


def unregister_mcp(mcp_config: str | None, yes: bool, out) -> None:
    if mcp_config:
        path = Path(mcp_config).expanduser()
        data, text = _load(path)
        servers = data.get("mcpServers", {})
        if SERVER_NAME not in servers:
            print(f"{path}: no changes", file=out)
            return
        del servers[SERVER_NAME]
        if not servers:
            del data["mcpServers"]
        _apply(path, text, data, yes, out)
        return
    claude = shutil.which("claude")
    if not claude:
        return
    if subprocess.run([claude, "mcp", "get", SERVER_NAME], capture_output=True).returncode != 0:
        print(f"MCP server {SERVER_NAME!r}: not registered", file=out)
        return
    argv = [claude, "mcp", "remove", "--scope", "user", SERVER_NAME]
    if _confirm("Remove the MCP server registration?", yes):
        subprocess.run(argv, check=True)


# --- commands ---------------------------------------------------------------------------


def install(
    *,
    project: bool = False,
    settings_file: str | None = None,
    mcp_config: str | None = None,
    command: list[str] | None = None,
    yes: bool = False,
    out=sys.stdout,
) -> int:
    command = command or default_command()
    path = settings_path(project, settings_file)
    data, text = _load(path)
    _apply(path, text, with_hooks(data, command), yes, out)
    register_mcp(command, mcp_config, yes, out)
    return 0


def uninstall(
    *,
    project: bool = False,
    settings_file: str | None = None,
    mcp_config: str | None = None,
    yes: bool = False,
    out=sys.stdout,
) -> int:
    path = settings_path(project, settings_file)
    data, text = _load(path)
    if path.exists():
        _apply(path, text, _strip_ours(data), yes, out)
    unregister_mcp(mcp_config, yes, out)
    return 0


def doctor(
    *,
    project: bool = False,
    settings_file: str | None = None,
    mcp_config: str | None = None,
    out=sys.stdout,
) -> int:
    from .db import MIGRATIONS, connect, schema_version
    from .hooks import run as run_hook
    from .paths import state_dir

    checks: list[tuple[str, bool, str]] = []
    root = state_dir()
    try:
        conn = connect(root)
        version = schema_version(conn)
        conn.close()
        checks.append(("state directory", True, f"{root} (private)"))
        checks.append(("database schema", version == len(MIGRATIONS), f"version {version}"))
    except Exception as exc:  # report, don't crash
        checks.append(("state directory", False, str(exc)))
    path = settings_path(project, settings_file)
    try:
        data, _ = _load(path)
        hooks = data.get("hooks", {})
        missing = [
            e
            for e in HOOK_EVENTS
            if not any(
                _is_ours(h)
                for g in hooks.get(e, [])
                if isinstance(g, dict)
                for h in g.get("hooks", [])
            )
        ]
        checks.append(
            ("claude hooks", not missing, f"{path}" + (f" missing {missing}" if missing else ""))
        )
    except InstallError as exc:
        checks.append(("claude hooks", False, str(exc)))
    if mcp_config:
        data, _ = _load(Path(mcp_config).expanduser())
        ok = SERVER_NAME in data.get("mcpServers", {})
        checks.append(("mcp server", ok, mcp_config))
    elif shutil.which("claude"):
        ok = (
            subprocess.run(["claude", "mcp", "get", SERVER_NAME], capture_output=True).returncode
            == 0
        )
        checks.append(("mcp server", ok, "claude mcp get agent-mailbox"))
    out_text = run_hook("claude", "user-prompt-submit", '{"session_id": "doctor"}', root)
    checks.append(("hook dry run", out_text == "", "no session -> empty output, exit 0"))
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}", file=out)
    return 0 if all(ok for _, ok, _ in checks) else 1


# --- Pi -----------------------------------------------------------------------------------

PI_EXTENSION_FILES = ("index.ts",)
PI_PROTOCOL = 1
PI_DESCRIPTION = "agent-mailbox: durable local mailbox shared with other coding agents."


def pi_mcp_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser() if explicit else Path.home() / ".pi/agent/mcp.json"


def pi_extension_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return Path.home() / ".pi/agent/extensions" / SERVER_NAME


def _pi_extension_contents(command: list[str]) -> dict[str, str]:
    from importlib.resources import files

    source = files("agent_mailbox") / "pi_extension"
    contents = {name: (source / name).read_text(encoding="utf-8") for name in PI_EXTENSION_FILES}
    contents["config.json"] = _dump({"protocol": PI_PROTOCOL, "command": command})
    return contents


def _pi_mcp_entry(command: list[str]) -> dict[str, Any]:
    return {**_mcp_entry(command), "exposure": "direct", "description": PI_DESCRIPTION}


def _write_extension(directory: Path, contents: dict[str, str], yes: bool, out) -> bool:
    changed = [
        name
        for name, text in contents.items()
        if not (directory / name).exists() or (directory / name).read_text(encoding="utf-8") != text
    ]
    if not changed:
        print(f"{directory}: no changes", file=out)
        return False
    print(f"will write {', '.join(changed)} in {directory}", file=out)
    if "config.json" in changed:
        print(contents["config.json"], end="", file=out)
    if not _confirm("Install the Pi extension?", yes):
        print("aborted; nothing written", file=out)
        return False
    for name in changed:
        _write_atomic(directory / name, contents[name])
    print(f"{directory}: installed", file=out)
    return True


def install_pi(
    *,
    mcp_config: str | None = None,
    extension_dir: str | None = None,
    command: list[str] | None = None,
    yes: bool = False,
    out=sys.stdout,
) -> int:
    command = command or default_command()
    path = pi_mcp_path(mcp_config)
    data, text = _load(path)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _pi_mcp_entry(command)
    _apply(path, text, data, yes, out)
    _write_extension(pi_extension_dir(extension_dir), _pi_extension_contents(command), yes, out)
    print("restart Pi sessions (or run /reload) to load it", file=out)
    return 0


def uninstall_pi(
    *,
    mcp_config: str | None = None,
    extension_dir: str | None = None,
    yes: bool = False,
    out=sys.stdout,
) -> int:
    path = pi_mcp_path(mcp_config)
    data, text = _load(path)
    servers = data.get("mcpServers", {})
    if SERVER_NAME in servers:
        del servers[SERVER_NAME]
        _apply(path, text, data, yes, out)
    else:
        print(f"{path}: no changes", file=out)
    directory = pi_extension_dir(extension_dir)
    ours = [directory / n for n in (*PI_EXTENSION_FILES, "config.json") if (directory / n).exists()]
    if not ours:
        print(f"{directory}: not installed", file=out)
        return 0
    print("will remove " + ", ".join(str(p) for p in ours), file=out)
    if _confirm("Remove the Pi extension?", yes):
        for p in ours:
            p.unlink()
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
        print(f"{directory}: removed", file=out)
    return 0


def doctor_pi(
    *, mcp_config: str | None = None, extension_dir: str | None = None, out=sys.stdout
) -> int:
    from .db import MIGRATIONS, connect, schema_version
    from .hooks import run as run_hook
    from .paths import state_dir

    checks: list[tuple[str, bool, str]] = []
    root = state_dir()
    try:
        conn = connect(root)
        version = schema_version(conn)
        conn.close()
        checks.append(("state directory", True, f"{root} (private)"))
        checks.append(("database schema", version == len(MIGRATIONS), f"version {version}"))
    except Exception as exc:  # report, don't crash
        checks.append(("state directory", False, str(exc)))
    path = pi_mcp_path(mcp_config)
    try:
        entry = _load(path)[0].get("mcpServers", {}).get(SERVER_NAME)
        checks.append(("mcp server", isinstance(entry, dict), str(path)))
    except InstallError as exc:
        checks.append(("mcp server", False, str(exc)))
    directory = pi_extension_dir(extension_dir)
    try:
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        command = config["command"]
        packaged = _pi_extension_contents(command)
        stale = [
            n
            for n in PI_EXTENSION_FILES
            if not (directory / n).exists()
            or (directory / n).read_text(encoding="utf-8") != packaged[n]
        ]
        checks.append(
            (
                "pi extension",
                not stale and config.get("protocol") == PI_PROTOCOL,
                str(directory) + (f" outdated {stale}; re-run install pi" if stale else ""),
            )
        )
        runnable = shutil.which(command[0]) is not None
        checks.append(("hook command", runnable, " ".join(command)))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        checks.append(("pi extension", False, f"{directory}: {exc}"))
    out_text = run_hook("pi", "turn-start", '{"client_pid": 2}', root)
    checks.append(("hook dry run", out_text == "", "no session -> empty output, exit 0"))
    for name, ok, detail in checks:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}", file=out)
    return 0 if all(ok for _, ok, _ in checks) else 1
