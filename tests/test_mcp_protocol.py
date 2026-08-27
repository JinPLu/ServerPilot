from __future__ import annotations

import asyncio
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, InitializeResult, ListToolsResult

from serverpilot import __version__
from serverpilot.config import InventoryConfig

ROOT = Path(__file__).resolve().parents[1]
ROUTINE_TOOLS = (
    "gpu_status",
    "gpu_apply",
    "gpu_release",
    "gpu_add_server",
    "gpu_update_server",
)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def isolated_broker(tmp_path: Path, build_app) -> tuple[str, Path]:
    port = _unused_loopback_port()
    app = build_app(
        "broker",
        inventory_config=InventoryConfig(schema_version=1, projects=[], endpoints=[]),
        project_root=ROOT,
        bind_host="127.0.0.1",
        bind_port=port,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    url = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{url}/health/live", timeout=0.2, trust_env=False)
            if response.status_code == 200:
                break
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError(f"isolated broker did not become live: {last_error}")
    try:
        yield url, tmp_path
    finally:
        server.should_exit = True
        thread.join(timeout=8)


@dataclass(frozen=True, slots=True)
class ProtocolProbe:
    initialize: InitializeResult
    tools: ListToolsResult
    gpu_status: CallToolResult
    stdout_errors: tuple[Exception, ...]
    isolated_home: Path


async def _probe_stdio(broker_url: str, isolated_home: Path) -> ProtocolProbe:
    # Run the package under test, not whichever console script happens to be on
    # PATH, and hand the child the same import roots as this interpreter so it
    # does not depend on the checkout being installed.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "serverpilot.mcp_server"],
        env={
            "HOME": str(isolated_home),
            "USERPROFILE": str(isolated_home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "SERVERPILOT_URL": broker_url,
            "SERVERPILOT_AUTOSTART": "0",
            "PYTHONUNBUFFERED": "1",
        },
        cwd=str(isolated_home),
    )
    stdout_errors: list[Exception] = []

    async def on_message(message: object) -> None:
        if isinstance(message, Exception):
            stdout_errors.append(message)

    errlog_path = isolated_home / "mcp-stderr.log"
    try:
        with errlog_path.open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read_stream, write_stream):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=20),
                    message_handler=on_message,
                ) as session:
                    initialize = await session.initialize()
                    tools = await session.list_tools()
                    gpu_status = await session.call_tool("gpu_status", {})
    except Exception as exc:
        stderr = errlog_path.read_text(encoding="utf-8") if errlog_path.is_file() else ""
        raise RuntimeError(f"MCP stdio probe failed: {exc}\nstderr:\n{stderr}") from exc
    return ProtocolProbe(
        initialize=initialize,
        tools=tools,
        gpu_status=gpu_status,
        stdout_errors=tuple(stdout_errors),
        isolated_home=isolated_home,
    )


@pytest.fixture
def protocol_probe(isolated_broker: tuple[str, Path]) -> ProtocolProbe:
    broker_url, isolated_home = isolated_broker
    return asyncio.run(_probe_stdio(broker_url, isolated_home))


def test_stdio_initialize_exposes_the_routine_surface(protocol_probe: ProtocolProbe) -> None:
    assert protocol_probe.initialize.serverInfo.name == "serverpilot"
    assert tuple(tool.name for tool in protocol_probe.tools.tools) == ROUTINE_TOOLS
    assert protocol_probe.gpu_status.isError is False
    assert protocol_probe.stdout_errors == ()
    assert not list(protocol_probe.isolated_home.rglob("*.plist"))
    assert not list(protocol_probe.isolated_home.rglob("LaunchAgents"))


def test_every_routine_tool_declares_its_effect(protocol_probe: ProtocolProbe) -> None:
    # Without annotations a client cannot tell a read from a lease mutation and
    # has to gate all three the same way.
    by_name = {tool.name: tool.annotations for tool in protocol_probe.tools.tools}

    assert by_name["gpu_status"].readOnlyHint is True
    assert by_name["gpu_apply"].readOnlyHint is False
    assert by_name["gpu_apply"].idempotentHint is False
    assert by_name["gpu_release"].idempotentHint is True
    for name in ROUTINE_TOOLS:
        assert by_name[name].openWorldHint is True, name


def test_stdio_initialize_reports_serverpilot_version(protocol_probe: ProtocolProbe) -> None:
    # FastMCP takes no version argument, so without an explicit assignment the
    # low-level server reports the MCP SDK's version and a client cannot tell
    # which ServerPilot it reached.
    assert protocol_probe.initialize.serverInfo.version == __version__
