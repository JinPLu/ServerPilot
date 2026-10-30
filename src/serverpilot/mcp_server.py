"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from serverpilot import __version__
from serverpilot.agent_contract import CARD_STATUS_BY_STATE, render_mcp_instructions
from serverpilot.agent_identity import normalize_client, resolve_project
from serverpilot.client import (
    CONTROL_PLANE_READ_TIMEOUT_SECONDS,
    BrokerClient,
    BrokerClientError,
    control_plane_async_httpx_client,
    control_plane_request_timeout,
    parse_broker_response,
    request_was_never_sent,
)
from serverpilot.config import control_plane_url
from serverpilot.daemon import ensure_broker_ready_for_mcp

ROUTINE_UNNAMED_TASK = "unnamed task"

_ROUTINE_MCP_INSTANCE_ID = secrets.token_hex(16)

ROUTINE_GPU_COUNT_DESCRIPTION = (
    "exact job parallelism from launch script/config (`devices`, "
    "`--nproc_per_node`, `num_processes`, `--gres`), never server/free capacity"
)

MCP_INSTRUCTIONS = render_mcp_instructions()


_http_client: httpx.AsyncClient | None = None


def _broker_url() -> str:
    url = control_plane_url()
    if not url.startswith(("http://", "https://")):
        raise BrokerClientError("SERVERPILOT_URL must start with http:// or https://")
    return url.rstrip("/")


class _AsyncBroker:
    """One AsyncClient for the MCP process; headers still vary by actor."""

    def __init__(self, http: httpx.AsyncClient, *, url: str, actor: str) -> None:
        self._http = http
        self.url = url.rstrip("/")
        self.actor = actor or "agent"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {
            "X-ServerPilot-Actor": self.actor,
            "X-ServerPilot-Session": _SESSION_ID,
            "X-ServerPilot-MCP-Version": __version__,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await self._http.request(
                method,
                f"{self.url}{path}",
                headers=headers,
                json=json_body,
                params=params,
                timeout=control_plane_request_timeout(path, json_body, timeout=timeout),
            )
        except httpx.HTTPError as exc:
            raise BrokerClientError(
                f"broker request failed: {type(exc).__name__}",
                unsent=request_was_never_sent(exc),
            ) from exc
        return parse_broker_response(response)

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            path,
            json_body=body,
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    async def patch(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self.request("PATCH", path, json_body=body, idempotency_key=idempotency_key)

    async def snapshot(
        self,
        *,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "compact": compact,
            "only_available": only_available,
        }
        if endpoint_id:
            params["endpoint_id"] = endpoint_id
        if state:
            params["state"] = state
        return await self.get("/api/v2/snapshot", params=params)


def _broker(actor: str | None) -> BrokerClient | _AsyncBroker:
    if _http_client is not None:
        return _AsyncBroker(_http_client, url=_broker_url(), actor=actor or "agent")
    return BrokerClient.from_env(actor=actor)


async def _client_call(target: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(target, method)(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


@asynccontextmanager
async def _mcp_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    global _http_client
    # Starts the macOS LaunchAgent when needed, then refuses a control plane
    # from another release on every platform — including Windows, where
    # ensure_broker_ready_for_mcp does not start a daemon.
    await anyio.to_thread.run_sync(ensure_broker_ready_for_mcp)
    async with control_plane_async_httpx_client(
        timeout=CONTROL_PLANE_READ_TIMEOUT_SECONDS
    ) as client:
        _http_client = client
        try:
            yield {}
        finally:
            _http_client = None


def _build_server(instructions: str) -> FastMCP:
    server = FastMCP("serverpilot", instructions=instructions, lifespan=_mcp_lifespan)
    # FastMCP has no version argument, so the low-level server would report the
    # MCP SDK's version in initialize and a client could not tell which
    # ServerPilot it is talking to.
    server._mcp_server.version = __version__
    return server


mcp = _build_server(MCP_INSTRUCTIONS)


def _routine_request_key(context: Context | None) -> str | None:
    """Map one MCP invocation to a private, stable REST replay key."""

    if context is None:
        return None
    request_id = f"{_ROUTINE_MCP_INSTANCE_ID}:{context.request_id}".encode()
    return f"mcp-request:{hashlib.sha256(request_id).hexdigest()}"


def _routine_ssh(endpoint: Any) -> dict[str, Any] | None:
    """Project one registered endpoint into shell-neutral SSH connection data."""

    if not isinstance(endpoint, dict):
        return None
    host = endpoint.get("host")
    port = endpoint.get("port")
    user = endpoint.get("ssh_user")
    if (
        not isinstance(host, str)
        or not host
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65_535
        or not isinstance(user, str)
        or not user
    ):
        return None
    return {"host": host, "port": port, "user": user}


def _routine_workspace(path: Any) -> dict[str, Any]:
    """Describe the endpoint cwd without implying that it is a code checkout."""

    return {
        "path": path if isinstance(path, str) and path else None,
        "kind": "working_directory",
        "use_as_cwd": True,
        "code_location": "not_provided",
    }


def _routine_integer(value: Any) -> int | None:
    """Keep an optional non-negative integer from a broker telemetry payload."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _routine_number(value: Any) -> int | float | None:
    """Keep an optional finite non-negative telemetry measurement."""

    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return value if value >= 0 else None


def _routine_telemetry_window(value: Any) -> dict[str, Any] | None:
    """Project the rolling-average window descriptor of one GPU observation.

    Every GPU observed in the same collector cycle shares this descriptor, so
    the status projection publishes it once per server instead of once per GPU.
    """

    if not isinstance(value, dict):
        return None
    window_seconds = _routine_integer(value.get("window_seconds"))
    sample_count = _routine_integer(value.get("sample_count"))
    first_observed_at = value.get("first_observed_at")
    last_observed_at = value.get("last_observed_at")
    window = {
        "window_seconds": window_seconds if window_seconds and window_seconds > 0 else None,
        "sample_count": sample_count if sample_count and sample_count > 0 else None,
        "first_observed_at": (
            first_observed_at if isinstance(first_observed_at, str) and first_observed_at else None
        ),
        "last_observed_at": (
            last_observed_at if isinstance(last_observed_at, str) and last_observed_at else None
        ),
    }
    return window if any(item is not None for item in window.values()) else None


def _routine_shared_telemetry_window(
    windows: list[dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Return the window shared by one server's GPUs, or None when they disagree.

    Disagreement is not smoothed over.  A partially failing collector cycle must
    not be published as one shared window, so the caller keeps each GPU's own
    descriptor instead.
    """

    observed = [window for window in windows if window is not None]
    if not observed:
        return None
    first = observed[0]
    return first if all(window == first for window in observed[1:]) else None


def _routine_recent_telemetry_average(value: Any) -> dict[str, Any] | None:
    """Project one GPU's rolling-average measurements.

    The window descriptor lives in ``servers[].telemetry_window``; a GPU whose
    window disagrees with its server keeps its own ``telemetry.window_override``.
    """

    if not isinstance(value, dict):
        return None
    return {
        "memory_used_mib": _routine_number(value.get("memory_used_mib")),
        "memory_free_mib": _routine_number(value.get("memory_free_mib")),
        "memory_used_pct": _routine_number(value.get("memory_used_pct")),
        "gpu_utilization_pct": _routine_number(value.get("gpu_utilization_pct")),
        "memory_utilization_pct": _routine_number(value.get("memory_utilization_pct")),
        "temperature_c": _routine_number(value.get("temperature_c")),
    }


def _routine_gpu_telemetry(gpu: dict[str, Any], *, vram_mib: Any) -> dict[str, Any] | None:
    """Project the latest sample for one GPU the calling lease holds.

    Preserve the observation timestamp so a caller can distinguish a missing or
    old sample from a lightly loaded device.  One sample is the supporting
    reading only: a tuning decision follows the rolling average next to it.
    """

    source = gpu.get("telemetry")
    if not isinstance(source, dict):
        return None
    total_vram_mib = _routine_integer(vram_mib)
    memory_used_mib = _routine_integer(source.get("memory_used_mib"))
    memory_used_pct = (
        round(memory_used_mib * 100 / total_vram_mib, 2)
        if memory_used_mib is not None and total_vram_mib and total_vram_mib > 0
        else None
    )
    observed_at = source.get("observed_at")
    return {
        "observed_at": observed_at if isinstance(observed_at, str) and observed_at else None,
        "memory_used_mib": memory_used_mib,
        "memory_free_mib": _routine_integer(source.get("memory_free_mib")),
        "memory_used_pct": memory_used_pct,
        "gpu_utilization_pct": _routine_integer(source.get("gpu_utilization_pct")),
        "memory_utilization_pct": _routine_integer(source.get("memory_utilization_pct")),
        "temperature_c": _routine_integer(source.get("temperature_c")),
    }


def _routine_lease_gpu_telemetry(gpu: dict[str, Any], *, vram_mib: Any) -> dict[str, Any] | None:
    """Project telemetry for one GPU held by the calling lease.

    The rolling average leads because a tuning decision follows the sustained
    picture: a low sustained utilization points at the input pipeline, and the
    sustained free memory is what bounds a larger batch.  A single sample moves
    with whatever step the job happens to be in, so it stays alongside as
    ``current`` rather than being the number a caller reads first.
    """

    source = gpu.get("telemetry")
    if not isinstance(source, dict):
        return None
    return {
        "recent_average": _routine_recent_telemetry_average(source.get("lease_recent_average")),
        "current": _routine_gpu_telemetry(gpu, vram_mib=vram_mib),
    }


def _routine_lease_utilization(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize how well one lease is using the GPUs it holds.

    ``min_memory_free_mib`` and the laggard card are the two numbers a tuning
    decision turns on: the first bounds a larger batch across the whole lease,
    the second exposes one GPU holding a multi-GPU job back.  Averaging those
    two away would hide exactly the case worth acting on.
    """

    observed = [
        (row, average) for row in rows if isinstance(average := row.get("recent_average"), dict)
    ]
    if not observed:
        return {"telemetry_gpu_count": 0}

    def collect(field: str) -> list[int | float]:
        return [
            value
            for _row, average in observed
            if (value := _routine_number(average.get(field))) is not None
        ]

    def mean(values: list[int | float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    utilization = collect("gpu_utilization_pct")
    memory_free = collect("memory_free_mib")
    summary: dict[str, Any] = {
        "telemetry_gpu_count": len(observed),
        "recent_average": {
            "gpu_utilization_pct": mean(utilization),
            "memory_used_pct": mean(collect("memory_used_pct")),
            "memory_utilization_pct": mean(collect("memory_utilization_pct")),
            "min_memory_free_mib": min(memory_free) if memory_free else None,
        },
    }
    if len(utilization) > 1:
        summary["gpu_utilization_spread_pct"] = round(max(utilization) - min(utilization), 2)
        slowest_row, slowest_value = min(
            (
                (row, value)
                for row, average in observed
                if (value := _routine_number(average.get("gpu_utilization_pct"))) is not None
            ),
            key=lambda item: item[1],
        )
        summary["slowest_gpu"] = {
            "gpu_id": slowest_row.get("gpu_id"),
            "index": slowest_row.get("index"),
            "gpu_utilization_pct": slowest_value,
        }
    return summary


def _routine_group_catalog(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep top-level server-group records in snapshot order."""

    raw = data.get("server_groups")
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]


def _routine_sku_capacity(gpus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate one server's GPUs by name and VRAM so 4+4 cannot look like 8."""

    buckets: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[tuple[Any, Any]] = []
    for gpu in gpus:
        key = (gpu.get("name"), gpu.get("total_vram_mib"))
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "name": gpu.get("name"),
                "vram_mib": gpu.get("total_vram_mib"),
                "total_count": 0,
                "available_count": 0,
            }
            buckets[key] = bucket
            order.append(key)
        bucket["total_count"] += 1
        if gpu.get("publicly_available") is True:
            bucket["available_count"] += 1
    return [buckets[key] for key in order]


def _routine_status_server(
    endpoint: Any, server_id: Any, gpus: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project one GPU server: connection once, capacity as SKU counts."""

    workspace_path = endpoint.get("workspace_path") if isinstance(endpoint, dict) else None
    server: dict[str, Any] = {
        "server_id": server_id,
        "workspace_path": workspace_path,
        "workspace": _routine_workspace(workspace_path),
        "gpus": _routine_sku_capacity(gpus),
    }
    ssh = _routine_ssh(endpoint)
    if ssh is not None:
        server["ssh"] = ssh
    return server


def _routine_group_projection(
    record: dict[str, Any], servers: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project group metadata plus the nested per-server capacity summary."""

    payload = {
        "id": record.get("id"),
        "display_name": record.get("display_name"),
        "workspace_path": record.get("workspace_path"),
        "environment_notes": record.get("environment_notes"),
        "description": record.get("description"),
        "servers": servers,
    }
    if "allocation" in record:
        payload["allocation"] = record["allocation"]
    if "limits" in record:
        payload["limits"] = record["limits"]
    if "largest_allocatable_block" in record:
        payload["largest_allocatable_block"] = record["largest_allocatable_block"]
    return payload


def _routine_scheduler_capacity(endpoint: Any) -> dict[str, Any] | None:
    if not isinstance(endpoint, dict):
        return None
    capacity = endpoint.get("scheduler_capacity")
    if (
        not isinstance(capacity, dict)
        or not isinstance(capacity.get("free_gpu_count"), int)
        or not isinstance(capacity.get("gpu_name"), str)
        or not capacity["gpu_name"]
    ):
        return None
    return capacity


def _routine_delegated_server(endpoint: dict[str, Any], capacity: dict[str, Any]) -> dict[str, Any]:
    """Project a delegated cluster login as one server with a scheduler SKU."""

    sku: dict[str, Any] = {
        "name": capacity["gpu_name"],
        "available_count": capacity["free_gpu_count"],
    }
    vram = capacity.get("vram_mib")
    if type(vram) is int and not isinstance(vram, bool) and vram >= 1:
        sku["vram_mib"] = vram
    workspace_path = endpoint.get("workspace_path")
    server: dict[str, Any] = {
        "server_id": endpoint.get("id"),
        "workspace_path": workspace_path,
        "workspace": _routine_workspace(workspace_path),
        "gpus": [sku],
    }
    ssh = _routine_ssh(endpoint)
    if ssh is not None:
        server["ssh"] = ssh
    return server


def _routine_has_gpu_servers(
    server_groups: list[dict[str, Any]], ungrouped_servers: list[dict[str, Any]]
) -> bool:
    if ungrouped_servers:
        return True
    return any(isinstance(group, dict) and group.get("servers") for group in server_groups)


def _routine_has_available_capacity(
    server_groups: list[dict[str, Any]], ungrouped_servers: list[dict[str, Any]]
) -> bool:
    servers = [
        server
        for group in server_groups
        for server in group.get("servers", [])
        if isinstance(server, dict)
    ]
    servers.extend(ungrouped_servers)
    return any(
        isinstance(sku.get("available_count"), int) and sku["available_count"] > 0
        for server in servers
        for sku in server.get("gpus", [])
        if isinstance(sku, dict)
    )


def _routine_open_leases(
    data: dict[str, Any], status_by_gpu_id: dict[str, str]
) -> list[dict[str, Any]]:
    """Name the leases that are still holding cards, so one can be released.

    ``gpu_apply`` returns a lease id exactly once. A caller that comes back in
    a later turn, or a second caller on the same machine, can see from
    ``busy_gpus`` that cards are held and even whose task holds them, but has
    no way to name the lease, and ``gpu_release`` takes nothing else. Releasing
    a finished lease was therefore impossible and idle reclaim was the only way
    a card ever came back. ``running_gpu_count`` is what separates a lease
    still doing work from one that is only holding.
    """

    leases: list[dict[str, Any]] = []
    for lease in data.get("leases", []) or []:
        if not isinstance(lease, dict) or lease.get("kind") != "workload":
            continue
        gpu_ids = [item for item in lease.get("gpu_ids", []) or [] if isinstance(item, str)]
        allocations = lease.get("allocations", [])
        delegated = [item for item in allocations if item.get("release_granularity") == "allocation"]
        if delegated:
            fresh = all(item.get("workload_fresh") and item.get("fresh") for item in delegated)
            locations = [location for item in delegated for location in item.get("locations", [])]
            known_devices = [device for location in locations for device in location.get("devices", [])]
            count = sum(location["gpu_count"] for location in locations)
            running_known = fresh and len(known_devices) == count and all(
                device.get("running_process_count") is not None for device in known_devices)
            leases.append({"lease_id": lease.get("id"), "task": lease.get("task_ref"),
                           "servers": sorted({item["endpoint_id"] for item in delegated}), "gpu_count": count,
                           "running_gpu_count": sum(device["running_process_count"] > 0 for device in known_devices) if running_known else None,
                           "held_since": lease.get("issued_at"), "allocations": delegated})
            continue
        if not gpu_ids:
            continue
        leases.append(
            {
                "lease_id": lease.get("id"),
                "task": lease.get("task_ref") or ROUTINE_UNNAMED_TASK,
                "servers": sorted({item.split(":", 1)[0] for item in gpu_ids}),
                "gpu_count": len(gpu_ids),
                "running_gpu_count": sum(
                    1 for item in gpu_ids if status_by_gpu_id.get(item) == "in_use"
                ),
                "held_since": lease.get("issued_at"),
            }
        )
    return leases


def _routine_gpu_status(payload: dict[str, Any], *, lease_id: str | None) -> dict[str, Any]:
    """Project the routine status view as grouped capacity plus occupancy.

    Allocatable capacity is a per-server SKU summary under ``server_groups`` or
    ``ungrouped_servers``, never one free row per card.  ``leased_gpus`` says
    how the caller's own workload is running, and ``busy_gpus`` says who holds
    the rest and, in ``status``, whether a compute process is on the card.  Telemetry belongs to exactly one of them: a card is only
    readable where its occupancy provably belongs to the reader.  On an
    unclaimed card the observable load is ServerPilot's own keepalive hold,
    which is stopped before allocation, so publishing it there would read as
    somebody else's work and turn a free card into a card that looks full.
    Capacity is what an unclaimed card can answer.
    """

    data = payload.get("data")
    values = data.get("gpus", []) if isinstance(data, dict) else []
    endpoints = data.get("endpoints", []) if isinstance(data, dict) else []
    endpoint_by_id = {
        endpoint.get("id"): endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict) and endpoint.get("id")
    }
    catalog = _routine_group_catalog(data if isinstance(data, dict) else {})
    catalog_by_id = {item["id"]: item for item in catalog}
    gpus_by_server: dict[Any, list[dict[str, Any]]] = {}
    leased_gpus: list[dict[str, Any]] = []
    busy_gpus: list[dict[str, Any]] = []
    lease_windows: list[dict[str, Any] | None] = []
    lease_task: str | None = None
    status_by_gpu_id: dict[str, str] = {}

    for gpu in values:
        if not isinstance(gpu, dict):
            continue
        available = gpu.get("publicly_available")
        state = gpu.get("state")
        if not isinstance(available, bool) or not isinstance(state, str) or not state:
            raise ValueError("ServerPilot returned an invalid GPU state")
        status = "free" if available else CARD_STATUS_BY_STATE.get(state, "unknown")
        gpu_row_id = gpu.get("id")
        if isinstance(gpu_row_id, str):
            status_by_gpu_id[gpu_row_id] = status
        lease = gpu.get("lease")
        lease = lease if isinstance(lease, dict) else None
        server_id = gpu.get("endpoint_id")
        gpus_by_server.setdefault(server_id, []).append(gpu)
        task = (lease.get("task_ref") or ROUTINE_UNNAMED_TASK) if lease is not None else None
        identity = {
            "server_id": server_id,
            "gpu_id": gpu.get("gpu_uuid"),
            "index": gpu.get("gpu_index"),
        }
        if lease_id is not None and lease is not None and lease.get("id") == lease_id:
            # The caller's own cards: every process on them is this lease's
            # workload, so telemetry here answers "is my job using the card
            # well" and nothing else.
            row = dict(identity)
            row["name"] = gpu.get("name")
            row["vram_mib"] = gpu.get("total_vram_mib")
            telemetry = _routine_lease_gpu_telemetry(gpu, vram_mib=row["vram_mib"])
            if telemetry is not None:
                row.update(telemetry)
            source = gpu.get("telemetry")
            lease_windows.append(
                _routine_telemetry_window(source.get("lease_recent_average"))
                if isinstance(source, dict)
                else None
            )
            if lease_task is None and task is not None:
                lease_task = task
            leased_gpus.append(row)
            continue
        if not available:
            # Answer "who holds the busy cards" inside the same response so a
            # placement decision does not need a second call.  Whose task it is
            # is actionable; how hard their job is working the card is not.
            busy_gpus.append({**identity, "status": status, "task": task})

    grouped_servers: dict[str, list[dict[str, Any]]] = {}
    ungrouped_servers: list[dict[str, Any]] = []
    delegated_ids: set[Any] = set()

    def place_server(row: dict[str, Any], group_id: Any) -> None:
        if isinstance(group_id, str) and group_id:
            grouped_servers.setdefault(group_id, []).append(row)
        else:
            ungrouped_servers.append(row)

    for server_id, server_gpus in gpus_by_server.items():
        endpoint = endpoint_by_id.get(server_id)
        if _routine_scheduler_capacity(endpoint) is not None:
            continue
        row = _routine_status_server(endpoint, server_id, server_gpus)
        group_id = endpoint.get("server_group_id") if isinstance(endpoint, dict) else None
        place_server(row, group_id)

    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        capacity = _routine_scheduler_capacity(endpoint)
        if capacity is None:
            continue
        delegated_ids.add(endpoint.get("id"))
        place_server(
            _routine_delegated_server(endpoint, capacity),
            endpoint.get("server_group_id"),
        )

    server_groups: list[dict[str, Any]] = []
    emitted_group_ids: set[str] = set()
    for record in catalog:
        group_id = record["id"]
        servers = grouped_servers.get(group_id, [])
        if not servers:
            continue
        server_groups.append(_routine_group_projection(record, servers))
        emitted_group_ids.add(group_id)
    for group_id, servers in grouped_servers.items():
        if group_id in emitted_group_ids or not servers:
            continue
        record = catalog_by_id.get(group_id, {"id": group_id})
        server_groups.append(_routine_group_projection(record, servers))

    result: dict[str, Any] = {}
    if server_groups:
        result["server_groups"] = server_groups
    if ungrouped_servers:
        result["ungrouped_servers"] = ungrouped_servers
    if lease_id is not None:
        held = next((item for item in data.get("leases", []) if item.get("id") == lease_id), None) if isinstance(data, dict) else None
        allocations = held.get("allocations", []) if held else []
        delegated = [item for item in allocations if item.get("release_granularity") == "allocation"]
        if delegated:
            rows = []
            for allocation in delegated:
                for location in allocation["locations"]:
                    for device in location["devices"]:
                        row = {"server_id": allocation["endpoint_id"], "allocation_id": allocation["id"],
                               "node": location["node"], "gpu_id": device["gpu_uuid"],
                               "index": device.get("physical_index"), "device_selector": device.get("device_selector"),
                               "name": device.get("name"), "vram_mib": device.get("total_vram_mib")}
                        if allocation["fresh"] and allocation["workload_fresh"]:
                            row.update({"memory_used_mib": device.get("memory_used_mib"),
                                        "gpu_utilization_pct": device.get("gpu_utilization_pct"),
                                        "running_process_count": device.get("running_process_count")})
                        rows.append(row)
            result.update({"lease": {"lease_id": lease_id, "task": held.get("task_ref"), "allocations": delegated,
                                     "gpu_count": sum(location["gpu_count"] for item in delegated for location in item["locations"])},
                           "leased_gpus": rows})
        else:
            result.update(_routine_lease_view(lease_id, leased_gpus, lease_windows, lease_task))
    open_leases = _routine_open_leases(data if isinstance(data, dict) else {}, status_by_gpu_id)
    if open_leases:
        result["open_leases"] = open_leases
    if busy_gpus:
        result["busy_gpus"] = busy_gpus
    if isinstance(data, dict) and data.get("pending_allocations"):
        result["pending_allocations"] = data["pending_allocations"]
    connections = [{"server_id": item["id"], **item["connection"]} for item in endpoints
                   if isinstance(item, dict) and item.get("connection", {}).get("supported")]
    if connections:
        result["connections"] = connections
    observation_gpus = {gpu["id"]: gpu for gpu in data.get("gpus", []) if isinstance(gpu, dict) and "id" in gpu} if isinstance(data, dict) else {}
    resource_nodes = [{"server_id": endpoint["id"], **node,
        "gpus": [{**{key: observation_gpus[gpu_id].get(key) for key in (
            "id", "gpu_uuid", "gpu_index", "name", "total_vram_mib", "telemetry", "processes")},
            "status": CARD_STATUS_BY_STATE.get(observation_gpus[gpu_id].get("state"), "unknown")}
            for gpu_id in node["gpu_ids"] if gpu_id in observation_gpus]}
        for endpoint in endpoints if isinstance(endpoint, dict) for node in endpoint.get("resource_nodes", [])]
    if resource_nodes:
        result["resource_nodes"] = resource_nodes
    external = [{"server_id": item["id"], **allocation} for item in endpoints if isinstance(item, dict)
                for allocation in item.get("external_allocations", [])]
    if external:
        result["external_allocations"] = external
    cpu_only_servers: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        if endpoint.get("id") in delegated_ids:
            continue
        if endpoint.get("resource_kind") != "cpu_only":
            continue
        monitor = endpoint.get("monitor")
        monitor = monitor if isinstance(monitor, dict) else {}
        host_telemetry = endpoint.get("host_telemetry")
        host_telemetry = host_telemetry if isinstance(host_telemetry, dict) else {}
        capacity = host_telemetry.get("capacity")
        capacity = capacity if isinstance(capacity, dict) else {}
        cpu_only_servers.append(
            {
                "server_id": endpoint.get("id"),
                "resource_kind": "cpu_only",
                "monitor_status": monitor.get("status"),
                "cpu_cores": _routine_number(capacity.get("cpu_cores")),
                "cpu_available_cores": _routine_number(capacity.get("cpu_available_cores")),
                "memory_total_mib": _routine_integer(capacity.get("memory_total_mib")),
                "memory_available_mib": _routine_integer(capacity.get("memory_available_mib")),
            }
        )
    if cpu_only_servers:
        result["cpu_only_servers"] = cpu_only_servers
    summary = data.get("summary") if isinstance(data, dict) else None
    if not _routine_has_available_capacity(server_groups, ungrouped_servers):
        if (
            isinstance(summary, dict)
            and summary.get("total_gpus") == 0
            and not _routine_has_gpu_servers(server_groups, ungrouped_servers)
        ):
            result["message"] = "no GPUs are registered"
        else:
            total_gpus = summary.get("total_gpus") if isinstance(summary, dict) else None
            result["no_capacity"] = {
                "reason": "all_gpus_busy_or_unavailable",
                "message": "no GPU is allocatable right now; busy_gpus lists the tasks holding them.",
                "total_gpus": total_gpus if isinstance(total_gpus, int) else None,
            }
    return result


def _routine_lease_view(
    lease_id: str,
    rows: list[dict[str, Any]],
    windows: list[dict[str, Any] | None],
    task: str | None,
) -> dict[str, Any]:
    """Project the caller's own lease: its GPUs, their telemetry and one summary."""

    if not rows:
        return {
            "no_leased_gpus": {
                "lease_id": lease_id,
                "reason": "lease_holds_no_visible_gpu",
                "message": (
                    "This lease has no visible GPU: it may have been released, reclaimed as "
                    "idle, or fall outside the server_id this call narrowed to."
                ),
            }
        }
    lease: dict[str, Any] = {"lease_id": lease_id, "gpu_count": len(rows)}
    if task is not None:
        lease["task"] = task
    shared_window = _routine_shared_telemetry_window(windows)
    if shared_window is not None:
        lease["telemetry_window"] = shared_window
    else:
        # A partially failing collector cycle is not smoothed into one window.
        for row, window in zip(rows, windows, strict=True):
            if window is not None:
                row["window_override"] = window
    lease.update(_routine_lease_utilization(rows))
    return {"lease": lease, "leased_gpus": rows}


# ---- task sessions --------------------------------------------------------

_SESSION_ID = secrets.token_hex(16)
_session_state: dict[str, Any] = {}

#: Refusals an agent is expected to act on. Everything else is a fault.
DOCUMENTED_REFUSALS = frozenset({
    "no_capacity", "group_selection_required", "task_already_held", "resize_unsupported",
    "epoch_conflict", "card_in_use", "card_not_held", "use_release", "nothing_to_change",
    "lease_not_found", "lease_forbidden", "lease_taken_over", "lease_ended",
    "takeover_project_mismatch", "project_required", "allocation_unconfirmed",
    "entry_unusable", "restart_client", "server_required", "server_group_not_found",
    "endpoint_not_found",
})
RETRYABLE_REFUSALS = frozenset({"no_capacity", "allocation_unconfirmed"})


def _next_step(code: str, details: dict[str, Any]) -> dict[str, Any] | None:
    if code == "task_already_held":
        tool = "gpu_lease" if details.get("held_by_you") else "gpu_take_over"
        return {"tool": tool, "args": {"lease_id": details.get("lease_id")}}
    if code == "resize_unsupported":
        return {"tool": "gpu_apply", "args": {"task": "<a new task name>"}}
    if code == "allocation_unconfirmed":
        return {"tool": "gpu_apply", "args": "repeat the same call"}
    if code == "group_selection_required":
        return {"tool": "gpu_apply", "args": {"server_group_id": "<one of details.groups>"}}
    if code == "lease_taken_over":
        return {"tool": "gpu_status", "args": {}}
    return None


def _refusal(exc: BrokerClientError) -> dict[str, Any]:
    if exc.code not in DOCUMENTED_REFUSALS:
        raise exc
    details = dict(exc.details or {})
    error: dict[str, Any] = {
        "code": exc.code,
        "message": str(exc).split(": ", 2)[-1],
        "retryable": exc.code in RETRYABLE_REFUSALS,
    }
    retry_after = details.pop("retry_after_seconds", None)
    if retry_after is not None:
        error["retry_after_seconds"] = retry_after
    following = _next_step(exc.code, details)
    if following is not None:
        error["next"] = following
    if details:
        error["details"] = details
    return {"ok": False, "error": error}


async def _session_roots(context: Context | None) -> list[str]:
    if context is None:
        return []
    try:
        result = await context.session.list_roots()
    except Exception:
        # A client without roots answers with an error. Not having them is
        # what the fallbacks are for.
        return []
    return [str(root.uri) for root in result.roots]


def _client_info(context: Context | None) -> Any:
    try:
        params = context.session.client_params if context is not None else None
    except ValueError:
        # Outside a request there is no session to ask.
        return None
    return params.clientInfo if params is not None else None


async def _identity(context: Context | None, explicit: str | None = None) -> tuple[Any, str]:
    """The client for this process and its project, registering on first use."""

    info = _client_info(context)
    actor = normalize_client(info.name if info is not None else None)
    project = _session_state.get("project") if not explicit else None
    if project is None:
        project = resolve_project(
            explicit=explicit,
            roots=await _session_roots(context),
            environment=os.environ,
            cwd=Path.cwd(),
            home=Path.home(),
        )
    if project is None:
        raise BrokerClientError(
            "broker HTTP 400: project_required: pass project; this client gives no project root",
            code="project_required",
            status_code=400,
        )
    client = _session_client(actor)
    if _session_state.get("registered") != project:
        await _client_call(client, "post", "/api/v2/sessions", {
            "session_id": _SESSION_ID,
            "client": actor,
            "client_version": info.version if info is not None else None,
            "project": project,
        })
        _session_state.update(registered=project, project=project)
    return client, project


def _session_client(actor: str) -> Any:
    return _broker(actor)


def _cards_by_server(lease: dict[str, Any]) -> list[dict[str, Any]]:
    """Each server of a lease as something an agent can use without assembling it."""

    servers: list[dict[str, Any]] = []
    for resource in lease.get("resources", []):
        endpoint = resource.get("endpoint") or {}
        server: dict[str, Any] = {"server_id": endpoint.get("id")}
        ssh = _routine_ssh(endpoint)
        if ssh is not None:
            server["ssh"] = ssh
        if endpoint.get("workspace_path"):
            server["cwd"] = endpoint["workspace_path"]
        entry = resource.get("entry")
        if isinstance(entry, dict) and entry.get("kind") == "allocation":
            server["entry"] = entry
            server["allocation_id"] = resource.get("allocation_id")
            server["cards"] = [
                {"node": location["node"], "gpu_id": device["gpu_uuid"],
                 "selector": device.get("device_selector")}
                for location in resource.get("locations", [])
                for device in location["devices"]
            ]
        else:
            server["env"] = {
                "CUDA_DEVICE_ORDER": resource.get("cuda_device_order"),
                "CUDA_VISIBLE_DEVICES": resource.get("cuda_visible_devices"),
            }
            server["cards"] = [
                {"gpu_id": gpu.get("gpu_uuid"), "index": gpu.get("gpu_index"),
                 "selector": gpu.get("cuda_ordinal")}
                for gpu in resource.get("gpus", [])
            ]
        servers.append(server)
    return servers


async def _post(client: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _client_call(client, "post", path, body)
    except BrokerClientError as exc:
        if not exc.unsent:
            raise
    # Nothing reached the control plane, and repeating a task returns its lease.
    return await _client_call(client, "post", path, body)


def _node_view(node: dict[str, Any]) -> dict[str, Any]:
    """One node as facts to reason from, without every timestamp behind them."""

    capacity = (node.get("host_telemetry") or {}).get("capacity") or {}
    cards = []
    for gpu in node.get("gpus", []):
        telemetry = gpu.get("telemetry") or {}
        cards.append({
            "gpu_id": gpu.get("gpu_uuid"),
            "index": gpu.get("gpu_index"),
            "name": gpu.get("name"),
            "status": gpu.get("status"),
            "memory_used_mib": telemetry.get("memory_used_mib"),
            "memory_free_mib": telemetry.get("memory_free_mib"),
            "utilization_pct": telemetry.get("gpu_utilization_pct"),
            "processes": [
                {"pid": item.get("pid"), "user": item.get("username"), "memory_mib": item.get("used_memory_mib")}
                for item in gpu.get("processes") or []
            ],
        })
    return {
        "server_id": node.get("server_id"),
        "node": node.get("node_id"),
        "fresh": node.get("fresh"),
        "observed_at": node.get("observed_at"),
        "error_code": node.get("error_code"),
        "usage": node.get("usage"),
        "cpu_available_cores": capacity.get("cpu_available_cores"),
        "memory_available_mib": capacity.get("memory_available_mib"),
        "cards": cards,
    }


def _capacity_digest(groups: list[dict[str, Any]]) -> str:
    facts = sorted((str(group.get("server_group_id") or group.get("server_id")), group["free"],
                    group["largest_block"], group["unknown"]) for group in groups)
    return hashlib.sha256(repr(facts).encode()).hexdigest()[:12]


def _clean(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in group.items() if value not in (None, 0) or key == "free"}
            for group in groups]


LEASE_SUMMARY_KEYS = ("recent_average", "slowest_gpu")

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
_HEARTBEAT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True)
_ACTS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_RETURNS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


@mcp.tool(annotations=_READ_ONLY)
async def gpu_status(
    view: Annotated[
        Literal["summary", "capacity", "servers", "nodes"],
        Field(description="summary=capacity plus your leases; capacity=free cards only; "
                          "servers=per-server detail; nodes=one server's nodes, needs server_id"),
    ] = "summary",
    server_group_id: str | None = None,
    server_id: str | None = None,
    if_changed: Annotated[
        str | None, Field(description="digest from an earlier reply; unchanged capacity answers briefly")
    ] = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Read capacity and your own leases. Does not count as a heartbeat."""

    try:
        client, project = await _identity(context)
        if view == "nodes" and server_id is None:
            raise BrokerClientError(
                "broker HTTP 400: server_required: view=nodes needs server_id",
                code="server_required", status_code=400,
            )
        if view in {"servers", "nodes"}:
            payload = await _client_call(client, "snapshot", endpoint_id=server_id)
            detail = _routine_gpu_status(payload, lease_id=None)
            if view == "nodes":
                return {"ok": True, "nodes": [_node_view(node) for node in detail.get("resource_nodes", [])]}
            detail.pop("resource_nodes", None)
            return {"ok": True, **detail}
        groups = (await _client_call(client, "get", "/api/v2/capacity"))["groups"]
        if server_group_id is not None:
            groups = [group for group in groups if group.get("server_group_id") == server_group_id]
        digest = _capacity_digest(groups)
        if if_changed == digest:
            return {"ok": True, "unchanged": True, "digest": digest}
        answer: dict[str, Any] = {"ok": True, "digest": digest, "groups": _clean(groups)}
        if view == "summary":
            answer["project"] = project
            answer.update(await _client_call(client, "get", "/api/v2/leases/mine", params={"project": project}))
        return answer
    except BrokerClientError as exc:
        return _refusal(exc)


@mcp.tool(annotations=_ACTS)
async def gpu_apply(
    task: Annotated[str, Field(min_length=1, max_length=255, description="What this work is, stable across restarts")],
    duration_seconds: Annotated[int, Field(ge=60, le=2592000, description="Expected duration; an estimate for others, never a deadline for you")],
    gpu_count: Annotated[int, Field(ge=1, le=1024, description=ROUTINE_GPU_COUNT_DESCRIPTION)] = 1,
    server_group_id: str | None = None,
    server_id: str | None = None,
    project: str | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Get GPUs now, or a reason you can act on. Repeating a task returns its lease; nothing queues."""

    try:
        client, resolved = await _identity(context, project)
        payload = await _post(client, "/api/v2/leases", {
            "project": resolved, "task": task, "duration_seconds": duration_seconds,
            "gpu_count": gpu_count, "server_group_id": server_group_id, "server_id": server_id,
        })
    except BrokerClientError as exc:
        return _refusal(exc)
    lease = payload["lease"]
    return {
        "ok": True,
        "lease_id": lease["id"],
        "existing": bool(payload.get("existing")),
        "resource_epoch": lease.get("resource_epoch", 0),
        "heartbeat_within_seconds": payload.get("heartbeat_within_seconds"),
        "servers": _cards_by_server(lease),
    }


@mcp.tool(annotations=_HEARTBEAT)
async def gpu_lease(
    lease_id: str | None = None,
    task: str | None = None,
    keep_idle: Annotated[bool, Field(description="Postpone reclaiming idle cards, up to three windows")] = False,
    include: Annotated[
        list[Literal["entry", "telemetry"]] | None,
        Field(description="entry=ssh, cwd, env; telemetry=per-card use of your cards"),
    ] = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Heartbeat and read one of your leases: its state, cards at risk, and what changed since you last asked."""

    wanted = set(include or [])
    try:
        client, project = await _identity(context)
        view = await _client_call(client, "post", "/api/v2/leases/heartbeat", {
            "lease_id": lease_id, "task": task, "project": project,
            "keep_idle": keep_idle, "include_entry": "entry" in wanted,
        })
        lease = view.pop("lease", None)
        if lease is not None:
            view["servers"] = _cards_by_server(lease)
        if "telemetry" in wanted and "ended" not in view:
            payload = await _client_call(client, "snapshot")
            detail = _routine_gpu_status(payload, lease_id=view["lease_id"])
            view["telemetry"] = {
                "cards": [
                    {key: value for key, value in row.items() if key not in {"name", "server_id"}}
                    for row in detail.get("leased_gpus", [])
                ],
                "summary": {key: (detail.get("lease") or {}).get(key) for key in LEASE_SUMMARY_KEYS},
            }
        return {"ok": True, **view}
    except BrokerClientError as exc:
        return _refusal(exc)


@mcp.tool(annotations=_ACTS)
async def gpu_resize(
    lease_id: str | None = None,
    task: str | None = None,
    add_count: Annotated[int, Field(ge=0, le=1024)] = 0,
    server_id: Annotated[str | None, Field(description="Add on this server; default is where the lease already is")] = None,
    server_group_id: str | None = None,
    remove_gpu_ids: list[str] | None = None,
    expected_epoch: Annotated[int | None, Field(description="Refuse if the cards changed since you read this")] = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Add or return cards on your lease, all or nothing. A running process cannot see new cards until restarted."""

    try:
        client, project = await _identity(context)
        target = lease_id or (await _client_call(client, "post", "/api/v2/leases/heartbeat", {
            "task": task, "project": project,
        }))["lease_id"]
        payload = await _post(client, f"/api/v2/leases/{target}/resize", {
            "add_count": add_count, "server_id": server_id, "server_group_id": server_group_id,
            "remove_gpu_ids": remove_gpu_ids or [], "expected_epoch": expected_epoch,
        })
    except BrokerClientError as exc:
        return _refusal(exc)
    return {
        "ok": True,
        "lease_id": target,
        "resource_epoch": payload["resource_epoch"],
        "added": payload["added_gpu_ids"],
        "removed": payload["removed_gpu_ids"],
        "restart_required": True,
        "servers": _cards_by_server(payload["lease"]),
    }


@mcp.tool(annotations=_RETURNS)
async def gpu_release(
    lease_id: str | None = None,
    task: str | None = None,
    outcome: Literal["completed", "failed", "abandoned"] = "completed",
    context: Context | None = None,
) -> dict[str, Any]:
    """Return a whole lease when its task ends or fails."""

    try:
        client, project = await _identity(context)
        target = lease_id or (await _client_call(client, "post", "/api/v2/leases/heartbeat", {
            "task": task, "project": project,
        }))["lease_id"]
        await _client_call(client, "post", f"/api/v2/leases/{target}/release", {"outcome": outcome})
    except BrokerClientError as exc:
        if exc.code == "lease_already_released":
            return {"ok": True, "lease_id": lease_id, "ended": True}
        return _refusal(exc)
    return {"ok": True, "lease_id": target, "ended": True}


@mcp.tool(annotations=_ACTS)
async def gpu_take_over(
    reason: Annotated[str, Field(min_length=1, max_length=300)],
    lease_id: str | None = None,
    task: str | None = None,
    project: str | None = None,
    force: Annotated[bool, Field(description="Allow a lease from another project")] = False,
    context: Context | None = None,
) -> dict[str, Any]:
    """Make a lease yours after a restart, or one left by a crashed session. Audited."""

    try:
        client, resolved = await _identity(context, project)
        payload = await _client_call(client, "post", "/api/v2/leases/take-over", {
            "lease_id": lease_id, "task": task, "project": resolved, "reason": reason, "force": force,
        })
    except BrokerClientError as exc:
        return _refusal(exc)
    lease = payload["lease"]
    return {
        "ok": True,
        "lease_id": lease["id"],
        "resource_epoch": lease.get("resource_epoch", 0),
        "servers": _cards_by_server(lease),
    }


OBSERVATION_PROFILE_DESCRIPTION = (
    "linux for an ordinary host; otherwise a plugin id from `serverpilot plugin list`"
)
SERVER_GROUP_DESCRIPTION = "Group sharing workspace and data; a grouped host is reached only through it"


@mcp.tool(annotations=_ACTS)
async def gpu_server(
    action: Literal["add", "update"],
    server_id: str | None = None,
    host: str | None = None,
    port: int = 22,
    ssh_user: str | None = None,
    ssh_alias: str | None = None,
    workspace_path: str | None = None,
    server_group_id: Annotated[str | None, Field(description=SERVER_GROUP_DESCRIPTION)] = None,
    observation_profile: Annotated[str | None, Field(description=OBSERVATION_PROFILE_DESCRIPTION)] = None,
    labels: list[str] | None = None,
    project: str | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Add a host after explicit human approval, or update safe metadata. Host and port never change."""

    replay_key = _routine_request_key(context) or f"mcp-call:{secrets.token_hex(16)}"
    try:
        client, resolved = await _identity(context, project)
        if action == "add":
            if not host or not workspace_path:
                raise ValueError("adding a server needs host and workspace_path")
            generated = "server-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:96] + f"-p{port}"
            return {"ok": True, **await _client_call(client, "post", "/api/v2/endpoints", {
                "id": server_id or generated, "host": host, "port": port,
                "ssh_user": ssh_user or "root", "ssh_alias": ssh_alias,
                "workspace_path": workspace_path, "server_group_id": server_group_id,
                "observation_profile": observation_profile or "linux", "labels": labels or [],
                "storage_group": None, "expected_gpu_count": None,
                "expected_gpu_total_vram_mib": None, "owner_project_id": resolved,
            }, idempotency_key=replay_key)}
        if server_id is None:
            raise ValueError("updating a server needs server_id")
        body = {key: value for key, value in {
            "ssh_user": ssh_user, "ssh_alias": ssh_alias, "workspace_path": workspace_path,
            "server_group_id": server_group_id, "observation_profile": observation_profile,
            "labels": labels,
        }.items() if value is not None}
        if not body:
            raise ValueError("updating a server needs at least one field")
        return {"ok": True, **await _client_call(
            client, "patch", f"/api/v2/endpoints/{server_id}", body, idempotency_key=replay_key
        )}
    except BrokerClientError as exc:
        return _refusal(exc)


ROUTINE_MCP_TOOL_NAMES = (
    "gpu_status",
    "gpu_apply",
    "gpu_lease",
    "gpu_resize",
    "gpu_release",
    "gpu_take_over",
    "gpu_server",
)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
