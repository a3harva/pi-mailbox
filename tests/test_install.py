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
        if inst._is_ours(h)
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
    assert len(ours(data)) == len(inst.HOOK_EVENTS)
    assert {h["command"].split()[-1] for h in ours(data)} == set(inst.HOOK_EVENTS.values())
    assert json.loads(mcp.read_text())["mcpServers"]["agent-mailbox"]["args"] == ["serve"]
    for event, groups in (original or {}).get("hooks", {}).items():  # foreign hooks kept
        for g in groups:
            for h in g["hooks"]:
                if not inst._is_ours(h):
                    assert any(h in g2["hooks"] for g2 in data["hooks"][event])
    _, second = run(inst.install, settings_file=str(settings), mcp_config=str(mcp))
    assert second.count("no changes") == 2
    run(inst.uninstall, settings_file=str(settings), mcp_config=str(mcp))
    after = json.loads(settings.read_text())
    assert ours(after) == []
    if original is not None and not any(
        inst._is_ours(h)
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


def test_quoted_command_is_still_recognised(tmp_path):
    settings = tmp_path / "settings.json"
    kwargs = dict(settings_file=str(settings), mcp_config=str(tmp_path / "m.json"))
    command = ["/Users/a b/bin/agent-mailbox"]
    for _ in range(2):  # a second install must replace, not duplicate
        inst.install(command=command, yes=True, out=io.StringIO(), **kwargs)
    assert len(ours(json.loads(settings.read_text()))) == len(inst.HOOK_EVENTS)
    inst.uninstall(yes=True, out=io.StringIO(), **kwargs)
    assert ours(json.loads(settings.read_text())) == []


def test_symlinked_settings_stay_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}\n")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    run(inst.install, settings_file=str(link), mcp_config=str(tmp_path / "m.json"))
    assert link.is_symlink() and "StopFailure" in json.loads(real.read_text())["hooks"]
