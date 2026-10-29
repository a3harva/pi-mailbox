"""Install / uninstall / doctor for Claude Code."""

from __future__ import annotations

import io
import json

import pytest

from agent_mailbox import install as inst

CMD = ["/opt/bin/agent-mailbox"]


def run(fn, **kw):
    out = io.StringIO()
    code = (
        fn(out=out, yes=True, command=CMD, **kw)
        if fn is inst.install
        else fn(out=out, yes=True, **kw)
    )
    return code, out.getvalue()


def ours(data):
    return [
        h
        for groups in data.get("hooks", {}).values()
        for g in groups
        for h in g["hooks"]
        if inst.HOOK_MARK in h["command"]
    ]


@pytest.mark.parametrize(
    "original",
    [
        None,
        {},
        {
            "model": "sonnet",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]},
        },
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "lint.sh"},
                            {
                                "type": "command",
                                "command": "old/agent-mailbox hook claude post-tool-use",
                            },
                        ],
                    }
                ]
            }
        },
    ],
)
def test_install_uninstall_round_trip(tmp_path, original):
    settings = tmp_path / "settings.json"
    mcp = tmp_path / "mcp.json"
    if original is not None:
        settings.write_text(json.dumps(original, indent=2) + "\n")
    before = settings.read_text() if settings.exists() else None
    run(inst.install, settings_file=str(settings), mcp_config=str(mcp))
    data = json.loads(settings.read_text())
    assert len(ours(data)) == 4
    assert {h["command"].split()[-1] for h in ours(data)} == set(inst.HOOK_EVENTS.values())
    assert json.loads(mcp.read_text())["mcpServers"]["agent-mailbox"]["args"] == ["serve"]
    for event, groups in (original or {}).get("hooks", {}).items():  # foreign hooks kept
        for g in groups:
            for h in g["hooks"]:
                if inst.HOOK_MARK not in h["command"]:
                    assert any(h in g2["hooks"] for g2 in data["hooks"][event])
    _, second = run(inst.install, settings_file=str(settings), mcp_config=str(mcp))
    assert second.count("no changes") == 2
    run(inst.uninstall, settings_file=str(settings), mcp_config=str(mcp))
    after = json.loads(settings.read_text())
    assert ours(after) == []
    if original is not None and not any(
        inst.HOOK_MARK in h["command"]
        for gs in original.get("hooks", {}).values()
        for g in gs
        for h in g["hooks"]
    ):
        assert settings.read_text() == before  # byte-identical
    assert "agent-mailbox" not in json.loads(mcp.read_text() or "{}").get("mcpServers", {})


def test_malformed_settings_abort_without_writing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")
    with pytest.raises(inst.InstallError, match="not valid JSON"):
        run(inst.install, settings_file=str(settings), mcp_config=str(tmp_path / "m.json"))
    assert settings.read_text() == "{not json"


def test_refuses_without_tty_or_yes(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(inst.InstallError, match="--yes"):
        inst.install(
            settings_file=str(settings),
            mcp_config=str(tmp_path / "m.json"),
            command=CMD,
            yes=False,
            out=io.StringIO(),
        )
    assert not settings.exists()


def test_backup_written(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"model": "opus"}\n')
    run(inst.install, settings_file=str(settings), mcp_config=str(tmp_path / "m.json"))
    assert (tmp_path / "settings.json.agent-mailbox.bak").read_text() == '{"model": "opus"}\n'


def test_doctor(tmp_path, root):
    settings, mcp = tmp_path / "s.json", tmp_path / "m.json"
    out = io.StringIO()
    assert inst.doctor(settings_file=str(settings), mcp_config=str(mcp), out=out) == 1
    run(inst.install, settings_file=str(settings), mcp_config=str(mcp))
    out = io.StringIO()
    assert inst.doctor(settings_file=str(settings), mcp_config=str(mcp), out=out) == 0, (
        out.getvalue()
    )
