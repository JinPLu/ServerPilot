from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer

from serverpilot import cli, mcp_entry


@pytest.fixture(autouse=True)
def resolved_command(monkeypatch: pytest.MonkeyPatch) -> str:
    command = "/opt/serverpilot/bin/serverpilot-mcp"
    monkeypatch.setattr(
        "serverpilot.mcp_entry.shutil.which",
        lambda name: command if name == "serverpilot-mcp" else None,
    )
    return command


def test_cursor_registration_targets_the_client_config_file(resolved_command: str) -> None:
    registration = cli._mcp_registration("cursor", resolved_command)

    target = Path(registration["target"])
    assert target.name == "mcp.json"
    assert target.parent.name == ".cursor"
    assert registration["config"]["mcpServers"]["serverpilot"]["command"] == resolved_command


def test_codex_and_claude_register_through_their_own_cli(resolved_command: str) -> None:
    codex = cli._mcp_registration("codex", resolved_command)
    claude = cli._mcp_registration("claude", resolved_command)

    assert codex["command_line"] == [
        "codex",
        "mcp",
        "add",
        "--env",
        "SERVERPILOT_URL=http://127.0.0.1:8787",
        "serverpilot",
        "--",
        resolved_command,
    ]
    assert claude["command_line"] == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "--env",
        "SERVERPILOT_URL=http://127.0.0.1:8787",
        "serverpilot",
        "--",
        resolved_command,
    ]


def test_every_client_receives_the_same_broker_url(resolved_command: str) -> None:
    # A client registered without the broker URL silently falls back to a
    # default that the other clients were told explicitly.
    entry = cli._mcp_server_entry(resolved_command)
    url = entry["env"]["SERVERPILOT_URL"]

    for client in cli.MCP_CLIENTS:
        registration = cli._mcp_registration(client, resolved_command)
        rendered = json.dumps(registration, ensure_ascii=False)
        assert url in rendered, client


def test_config_write_is_atomic_and_leaves_no_debris(tmp_path: Path, resolved_command: str) -> None:
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "/usr/bin/other"}}}), encoding="utf-8")

    cli._write_cursor_config(target, cli._mcp_server_entry(resolved_command))

    assert [path.name for path in tmp_path.iterdir()] == ["mcp.json"]
    assert json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["other"] == {
        "command": "/usr/bin/other"
    }


def test_install_keeps_other_servers_and_unrelated_keys(tmp_path: Path, resolved_command: str) -> None:
    target = tmp_path / "mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "/usr/bin/other"}}, "theme": "dark"}),
        encoding="utf-8",
    )

    cli._write_cursor_config(target, cli._mcp_server_entry(resolved_command))

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["theme"] == "dark"
    assert document["mcpServers"]["other"] == {"command": "/usr/bin/other"}
    assert document["mcpServers"]["serverpilot"]["command"] == resolved_command


def test_install_creates_the_config_when_the_client_has_none(tmp_path: Path, resolved_command: str) -> None:
    target = tmp_path / "nested" / "mcp.json"

    cli._write_cursor_config(target, cli._mcp_server_entry(resolved_command))

    document = json.loads(target.read_text(encoding="utf-8"))
    assert list(document["mcpServers"]) == ["serverpilot"]


def test_install_refuses_to_overwrite_an_unreadable_config(tmp_path: Path, resolved_command: str) -> None:
    target = tmp_path / "mcp.json"
    target.write_text("{not json", encoding="utf-8")

    with pytest.raises(typer.Exit):
        cli._write_cursor_config(target, cli._mcp_server_entry(resolved_command))

    assert target.read_text(encoding="utf-8") == "{not json"


def test_missing_entry_point_reports_how_to_get_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("serverpilot.mcp_entry.shutil.which", lambda name: None)
    monkeypatch.setattr("serverpilot.mcp_entry.Path.is_file", lambda self: False)

    with pytest.raises(typer.Exit):
        cli._mcp_command()


def test_allowing_tools_keeps_everything_else_in_the_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "permissions": {"defaultMode": "auto", "allow": ["Read"]}}))

    assert mcp_entry.allow_in_claude(settings) is True
    written = json.loads(settings.read_text())
    assert written["theme"] == "dark"
    assert written["permissions"]["defaultMode"] == "auto"
    assert written["permissions"]["allow"] == ["Read", "mcp__serverpilot"]
    assert mcp_entry.allow_in_claude(settings) is False
    assert json.loads(settings.read_text())["permissions"]["allow"].count("mcp__serverpilot") == 1


def test_allowing_tools_creates_a_missing_settings_file(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    assert mcp_entry.allow_in_claude(settings) is True
    assert json.loads(settings.read_text()) == {"permissions": {"allow": ["mcp__serverpilot"]}}


@pytest.mark.parametrize("content", ["{broken", "[]", '{"permissions": []}', '{"permissions": {"allow": "x"}}'])
def test_unreadable_settings_are_refused_rather_than_replaced(tmp_path: Path, content: str) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(content)
    with pytest.raises(ValueError):
        mcp_entry.allow_in_claude(settings)
    assert settings.read_text() == content


def test_install_changes_no_permission_unless_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(mcp_entry, "claude_settings_path", lambda: settings)
    monkeypatch.setattr(cli, "claude_settings_path", lambda: settings)
    monkeypatch.setattr(cli, "allow_in_claude", lambda: mcp_entry.allow_in_claude(settings))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    cli._offer_claude_allow(None)
    cli._offer_claude_allow(False)
    assert not settings.exists()
    cli._offer_claude_allow(True)
    assert json.loads(settings.read_text())["permissions"]["allow"] == ["mcp__serverpilot"]


def test_reinstalling_still_reaches_the_permission_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone already registered must be able to allow the tools afterwards."""

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(cli, "allow_in_claude", lambda: mcp_entry.allow_in_claude(settings))
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args, 1, stdout="", stderr="MCP server serverpilot already exists in user config",
    ))
    cli.mcp_install(client="claude", allow_tools=True)
    assert json.loads(settings.read_text())["permissions"]["allow"] == ["mcp__serverpilot"]
