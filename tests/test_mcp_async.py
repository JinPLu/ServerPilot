from __future__ import annotations

import asyncio
import threading

import pytest

from serverpilot import mcp_server
from serverpilot.client import BrokerClientError
from serverpilot.mcp_server import mcp, routine_mcp
from tests.helpers import tools


def _schema_object(schema: dict[str, object], root: dict[str, object]) -> dict[str, object]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        defs = root.get("$defs") or root.get("definitions") or {}
        assert isinstance(defs, dict)
        resolved = defs[name]
        assert isinstance(resolved, dict)
        return resolved
    return schema


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
    tools = asyncio.run(routine_mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}

    status_schema = by_name["gpu_status"].inputSchema
    assert set(status_schema["properties"]) == {"server_id", "lease_id"}
    assert "required" not in status_schema
    assert status_schema["properties"]["lease_id"]["default"] is None

    apply_schema = by_name["gpu_apply"].inputSchema
    assert set(apply_schema["properties"]) == {"server_id", "gpu_count", "task"}
    assert "required" not in apply_schema
    assert apply_schema["properties"]["gpu_count"]["default"] == 1

    release_schema = by_name["gpu_release"].inputSchema
    assert release_schema["required"] == ["lease_id"]
    assert set(release_schema["properties"]) == {"lease_id"}


def test_object_parameters_publish_nested_schemas() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}

    submit_schema = by_name["gpu_scheduler_submit_once"].inputSchema
    assert set(submit_schema["properties"]) >= {"agent_name", "request", "idempotency_key"}
    request_schema = _schema_object(submit_schema["properties"]["request"], submit_schema)
    assert {
        "target_id",
        "project_id",
        "task_ref",
        "purpose",
        "approval_ref",
        "duration_seconds",
        "constraints",
        "scheduler",
        "script_body",
    }.issubset(request_schema["properties"])

    evaluation_schema = _schema_object(
        by_name["resource_evaluate_plan"].inputSchema["properties"]["evaluation"],
        by_name["resource_evaluate_plan"].inputSchema,
    )
    assert {"project_id", "task_ref", "baseline_runtime_seconds", "candidates"}.issubset(
        evaluation_schema["properties"]
    )

    claim_schema = _schema_object(
        by_name["resource_claim"].inputSchema["properties"]["claim"],
        by_name["resource_claim"].inputSchema,
    )
    assert {"project_id", "task_ref", "purpose", "quantities", "forecast"}.issubset(
        claim_schema["properties"]
    )
