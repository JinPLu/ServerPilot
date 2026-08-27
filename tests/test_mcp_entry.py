from __future__ import annotations

from pathlib import Path

import pytest
import typer
from fastapi.testclient import TestClient

from serverpilot import API_CAPABILITIES, cli, mcp_entry
from serverpilot.api import create_app
from serverpilot.config import Settings
from serverpilot.mcp_entry import (
    MCP_ENTRY_UNAVAILABLE_HINT,
    MCP_SERVER_NAME,
    MCPEntryUnavailable,
    mcp_entry_status,
    mcp_server_entry,
    resolve_mcp_command,
)


def test_resolve_mcp_command_uses_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "serverpilot-mcp"
    binary.parent.mkdir()
    binary.write_bytes(b"")
    monkeypatch.setattr(
        "serverpilot.mcp_entry.shutil.which",
        lambda name: str(binary) if name == "serverpilot-mcp" else None,
    )

    assert resolve_mcp_command() == str(binary)


def test_resolve_mcp_command_uses_packaged_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "ServerPilot"
    python.write_bytes(b"")
    sibling = tmp_path / mcp_entry.mcp_executable_name()
    sibling.write_bytes(b"")
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.sys, "executable", str(python))

    assert resolve_mcp_command() == str(sibling.resolve())


def test_resolve_mcp_command_looks_beside_the_unresolved_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A venv's `python` is a symlink to the base interpreter.

    Resolving it before looking for the sibling walks out of the directory that
    holds the console scripts, so the daemon reported no MCP entry at all for
    every `uv tool install`, which is the ordinary macOS layout.
    """

    base = tmp_path / "cpython" / "bin"
    base.mkdir(parents=True)
    interpreter = base / "python3.12"
    interpreter.write_bytes(b"")
    venv = tmp_path / "tools" / "serverpilot" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(interpreter)
    sibling = venv / mcp_entry.mcp_executable_name()
    sibling.write_bytes(b"")
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.sys, "executable", str(venv / "python"))

    assert resolve_mcp_command() == str(sibling)
    assert not (base / mcp_entry.mcp_executable_name()).exists()


def test_mcp_executable_name_uses_exe_suffix_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_entry.os, "name", "nt")
    assert mcp_entry.mcp_executable_name() == "serverpilot-mcp.exe"


def test_resolve_mcp_command_uses_exe_suffix_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "ServerPilot.exe"
    python.write_bytes(b"")
    sibling = tmp_path / "serverpilot-mcp.exe"
    sibling.write_bytes(b"")
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.sys, "executable", str(python))
    monkeypatch.setattr(mcp_entry, "mcp_executable_name", lambda: "serverpilot-mcp.exe")

    assert resolve_mcp_command() == str(sibling.resolve())


def test_resolve_mcp_command_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "python"
    python.write_bytes(b"")
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.sys, "executable", str(python))

    with pytest.raises(MCPEntryUnavailable, match="uv tool install") as exc:
        resolve_mcp_command()
    assert str(exc.value) == MCP_ENTRY_UNAVAILABLE_HINT


def test_mcp_entry_status_reports_not_found_without_inventing_a_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "python"
    python.write_bytes(b"")
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.sys, "executable", str(python))

    payload = mcp_entry_status()
    assert payload == {
        "available": False,
        "command": None,
        "mcpServers": None,
        "hint": MCP_ENTRY_UNAVAILABLE_HINT,
    }


def test_mcp_entry_status_returns_path_and_mcp_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = "/opt/serverpilot/bin/serverpilot-mcp"
    monkeypatch.setattr(
        "serverpilot.mcp_entry.shutil.which",
        lambda name: command if name == "serverpilot-mcp" else None,
    )
    monkeypatch.delenv("SERVERPILOT_URL", raising=False)

    payload = mcp_entry_status()
    assert payload["available"] is True
    assert payload["command"] == command
    assert payload["hint"] is None
    assert payload["mcpServers"] == {
        MCP_SERVER_NAME: mcp_server_entry(command),
    }
    assert payload["mcpServers"]["serverpilot"]["env"]["SERVERPILOT_URL"] == "http://127.0.0.1:8787"


def test_cli_maps_missing_entry_to_the_same_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.Path, "is_file", lambda self: False)

    with pytest.raises(typer.Exit) as exc:
        cli._mcp_command()
    assert exc.value.exit_code == 1
    assert capsys.readouterr().err.strip() == MCP_ENTRY_UNAVAILABLE_HINT


def _client(tmp_path: Path, inventory) -> TestClient:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(inventory.model_dump_json(), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'mcp-entry.sqlite3'}",
            inventory_path=inventory_path,
            project_root=Path(__file__).resolve().parents[1],
        )
    )
    return TestClient(app)


def test_mcp_entry_endpoint_returns_resolved_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inventory
) -> None:
    command = "/opt/serverpilot/bin/serverpilot-mcp"
    monkeypatch.setattr(
        "serverpilot.mcp_entry.shutil.which",
        lambda name: command if name == "serverpilot-mcp" else None,
    )
    monkeypatch.delenv("SERVERPILOT_URL", raising=False)
    headers = {"X-ServerPilot-Actor": "desktop-app"}

    with _client(tmp_path, inventory) as client:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert "mcp_entry" in live.json()["capabilities"]
        assert "mcp_entry" in API_CAPABILITIES

        response = client.get("/api/v1/mcp-entry", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"]["available"] is True
        assert payload["data"]["command"] == command
        assert payload["data"]["mcpServers"] == {
            "serverpilot": {
                "command": command,
                "env": {"SERVERPILOT_URL": "http://127.0.0.1:8787"},
            }
        }
        assert payload["data"]["hint"] is None


def test_mcp_entry_endpoint_returns_not_found_instead_of_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inventory
) -> None:
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.Path, "is_file", lambda self: False)
    headers = {"X-ServerPilot-Actor": "desktop-app"}

    with _client(tmp_path, inventory) as client:
        response = client.get("/api/v1/mcp-entry", headers=headers)
        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["available"] is False
        assert payload["command"] is None
        assert payload["mcpServers"] is None
        assert payload["hint"] == MCP_ENTRY_UNAVAILABLE_HINT


def test_mcp_entry_endpoint_uses_packaged_sibling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inventory
) -> None:
    python = tmp_path / "ServerPilot"
    python.write_bytes(b"")
    sibling = tmp_path / mcp_entry.mcp_executable_name()
    sibling.write_bytes(b"")
    monkeypatch.setattr(mcp_entry.shutil, "which", lambda name: None)
    monkeypatch.setattr(mcp_entry.sys, "executable", str(python))

    with _client(tmp_path, inventory) as client:
        response = client.get("/api/v1/mcp-entry", headers={"X-ServerPilot-Actor": "desktop-app"})
        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["available"] is True
        assert payload["command"] == str(sibling.resolve())
        assert payload["mcpServers"]["serverpilot"]["command"] == str(sibling.resolve())
