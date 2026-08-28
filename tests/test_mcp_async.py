from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from serverpilot import API_CAPABILITIES, __version__, daemon, mcp_server
from serverpilot.client import (
    CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS,
    CONTROL_PLANE_READ_TIMEOUT_SECONDS,
    BrokerClientError,
)
from serverpilot.daemon import DaemonError, ensure_broker_ready_for_mcp
from serverpilot.mcp_server import mcp
from tests.helpers import tools


def test_mcp_rejects_a_control_plane_from_another_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(
        daemon,
        "_probe_json",
        lambda *_args, **_kwargs: {
            "status": "live",
            "schema_version": "v1",
            "version": "1.9.0",
            "capabilities": list(API_CAPABILITIES),
        },
    )

    with pytest.raises(DaemonError, match="Restart the ServerPilot control plane") as captured:
        ensure_broker_ready_for_mcp()
    assert "this MCP is ServerPilot" in str(captured.value)
    assert __version__ in str(captured.value)
    assert "1.9.0" in str(captured.value)


def test_mcp_rejects_a_control_plane_missing_declared_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(
        daemon,
        "_probe_json",
        lambda *_args, **_kwargs: {
            "status": "live",
            "schema_version": "v1",
            "version": __version__,
            "capabilities": [item for item in API_CAPABILITIES if item != "server_group_crud"],
        },
    )

    with pytest.raises(DaemonError, match="server_group_crud") as captured:
        ensure_broker_ready_for_mcp()
    assert "Restart the ServerPilot control plane" in str(captured.value)


def test_mcp_accepts_a_control_plane_from_this_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(
        daemon,
        "_probe_json",
        lambda *_args, **_kwargs: {
            "status": "live",
            "schema_version": "v1",
            "version": __version__,
            "capabilities": list(API_CAPABILITIES),
        },
    )

    ensure_broker_ready_for_mcp()


def test_lifespan_ensures_the_daemon_off_the_event_loop_and_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_threads: list[int] = []
    monkeypatch.setattr(
        mcp_server,
        "ensure_broker_ready_for_mcp",
        lambda: ensure_threads.append(threading.get_ident()),
    )

    async def run() -> None:
        loop_thread = threading.get_ident()
        async with mcp_server._mcp_lifespan(mcp):
            client = mcp_server._http_client
            assert client is not None
            assert client.trust_env is False
            first = mcp_server._broker("agent")
            second = mcp_server._broker("other")
            assert first._http is client
            assert second._http is client
        assert mcp_server._http_client is None
        assert client.is_closed
        assert ensure_threads == [ensure_threads[0]]
        assert ensure_threads[0] != loop_thread

    asyncio.run(run())


def test_routine_tools_do_not_ensure_the_daemon_on_the_call_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "ensure_broker_ready_for_mcp",
        lambda: (_ for _ in ()).throw(AssertionError("ensure must not run per tool call")),
    )

    class Fake:
        def snapshot(self, **_kwargs: object) -> dict[str, object]:
            return {"data": {"summary": {"total_gpus": 0}, "gpus": [], "endpoints": []}}

        def post(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise BrokerClientError("already settled", code="lease_already_released")

    monkeypatch.setattr(mcp_server, "_routine_client", lambda: Fake())

    assert tools.gpu_status()["message"] == "no GPUs are registered"
    assert tools.gpu_release("lease-a") == {
        "released": True,
        "lease_id": "lease-a",
        "state": "RELEASED",
    }


def test_in_flight_tool_calls_overlap_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = 0
    gate = asyncio.Event()
    overlapped = asyncio.Event()

    class Slow:
        async def snapshot(self, **_kwargs: object) -> dict[str, object]:
            nonlocal started
            started += 1
            if started >= 2:
                overlapped.set()
            await gate.wait()
            return {"data": {"summary": {"total_gpus": 0}, "gpus": [], "endpoints": []}}

    monkeypatch.setattr(mcp_server, "_routine_client", lambda: Slow())

    async def run() -> None:
        first = asyncio.create_task(mcp.call_tool("gpu_status", {}))
        second = asyncio.create_task(mcp.call_tool("gpu_status", {}))
        await asyncio.wait_for(overlapped.wait(), timeout=2)
        assert started == 2
        gate.set()
        await first
        await second

    asyncio.run(run())


def test_routine_tool_parameter_names_are_unchanged() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}

    status_schema = by_name["gpu_status"].inputSchema
    assert set(status_schema["properties"]) == {"server_id", "lease_id"}
    assert "required" not in status_schema
    assert status_schema["properties"]["lease_id"]["default"] is None

    apply_schema = by_name["gpu_apply"].inputSchema
    assert set(apply_schema["properties"]) == {
        "server_group_id",
        "server_id",
        "gpu_count",
        "task",
    }
    assert "required" not in apply_schema
    assert apply_schema["properties"]["gpu_count"]["default"] == 1

    release_schema = by_name["gpu_release"].inputSchema
    assert release_schema["required"] == ["lease_id"]
    assert set(release_schema["properties"]) == {"lease_id"}


def test_async_broker_claim_waits_out_the_server_budget() -> None:
    recorded: list[float | None] = []

    class FakeHttp:
        async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
            del method, url
            timeout = kwargs.get("timeout")
            recorded.append(timeout if isinstance(timeout, int | float) else None)
            return httpx.Response(200, json={"schema_version": "v1", "data": {}})

    async def run() -> None:
        broker = mcp_server._AsyncBroker(
            FakeHttp(),  # type: ignore[arg-type]
            url="http://127.0.0.1:8787",
            actor="agent",
        )
        await broker.get("/api/v1/snapshot")
        await broker.post(
            "/api/v1/routine/claims",
            {"constraints": {"gpu_count": 8}},
        )

    asyncio.run(run())
    assert recorded == [
        CONTROL_PLANE_READ_TIMEOUT_SECONDS,
        CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS,
    ]


def test_object_parameters_publish_nested_schemas() -> None:
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "gpu_scheduler_submit_once" not in names
    assert "resource_evaluate_plan" not in names
    assert "resource_claim" not in names
