"""Resolve the MCP entry point this installation gives to agents.

A source install puts ``serverpilot-mcp`` on PATH. The packaged desktop
archive ships it next to the app executable instead, where nothing adds it
to PATH, so an absolute path is the only thing an agent can be handed.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

from serverpilot.config import CONTROL_PLANE_URL_VARIABLE, control_plane_url

MCP_SERVER_NAME = "serverpilot"
MCP_CLIENTS = ("codex", "claude", "cursor")
MCP_ENTRY_UNAVAILABLE_HINT = (
    "cannot find serverpilot-mcp; install this project with "
    "`uv tool install --force .` or use the packaged desktop archive"
)


class MCPEntryUnavailable(Exception):
    """The MCP executable is neither on PATH nor next to this process."""

    def __init__(self, message: str = MCP_ENTRY_UNAVAILABLE_HINT) -> None:
        super().__init__(message)


def mcp_executable_name() -> str:
    return "serverpilot-mcp.exe" if os.name == "nt" else "serverpilot-mcp"


def resolve_mcp_command() -> str:
    found = shutil.which("serverpilot-mcp")
    if found is not None:
        return found
    name = mcp_executable_name()
    # A virtualenv's `python` is a symlink to the base interpreter, so resolving
    # it first walks out of the directory that holds the console scripts. The
    # daemon runs from launchd with no PATH of its own, which makes this the
    # normal case rather than the exotic one.
    for candidate in (
        Path(sys.executable).with_name(name),
        Path(sys.executable).resolve().with_name(name),
    ):
        if candidate.is_file():
            return str(candidate)
    raise MCPEntryUnavailable()


def mcp_server_entry(command: str) -> dict[str, Any]:
    return {
        "command": command,
        "env": {CONTROL_PLANE_URL_VARIABLE: control_plane_url()},
    }


def mcp_servers_config(command: str) -> dict[str, Any]:
    return {MCP_SERVER_NAME: mcp_server_entry(command)}


def mcp_registration(client: str, command: str) -> dict[str, Any]:
    entry = mcp_server_entry(command)
    if client == "cursor":
        return {
            "client": client,
            "target": str(Path.home() / ".cursor" / "mcp.json"),
            "config": {"mcpServers": {MCP_SERVER_NAME: entry}},
        }
    cli = {"codex": "codex", "claude": "claude"}[client]
    argv = [cli, "mcp", "add"]
    if client == "claude":
        argv += ["--scope", "user"]
    for name, value in entry["env"].items():
        argv += ["--env", f"{name}={value}"]
    argv += [MCP_SERVER_NAME, "--", command]
    return {"client": client, "command_line": argv, "config": {MCP_SERVER_NAME: entry}}


def mcp_entry_status() -> dict[str, Any]:
    try:
        command = resolve_mcp_command()
    except MCPEntryUnavailable as exc:
        return {
            "available": False,
            "command": None,
            "mcpServers": None,
            "hint": str(exc),
        }
    return {
        "available": True,
        "command": command,
        "mcpServers": mcp_servers_config(command),
        "hint": None,
    }
