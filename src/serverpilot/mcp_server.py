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
from typing import Annotated, Any

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from serverpilot import __version__
from serverpilot.client import (
    CONTROL_PLANE_READ_TIMEOUT_SECONDS,
    BrokerClient,
    BrokerClientError,
    control_plane_async_httpx_client,
    control_plane_request_timeout,
    parse_broker_response,
    request_was_never_sent,
)
from serverpilot.daemon import ensure_broker_ready_for_mcp

# The agent surface reports the GPU state code, not the control plane's
# localized label. A caller decides on "can I claim this" and, when it cannot,
# on who is holding it; the desktop app owns how that reads in a human language.
ROUTINE_GPU_STATUS = {
    "CONFLICT": "ownership_conflict",
    "BUSY_UNMANAGED": "busy_unmanaged",
    "ORPHANED_BUSY": "busy_unmanaged",
    "RUNNING_MANAGED": "running",
    "HELD": "held_idle",
    "LEASED_IDLE": "held_idle",
    "MAINTENANCE": "maintenance",
    "DRAINING": "draining",
    "DISABLED": "disabled",
    "UNHEALTHY": "unhealthy",
    "UNKNOWN_STALE": "unreachable",
    "UNKNOWN_RECOVERING": "observing",
}
ROUTINE_GPU_STATUS_AVAILABLE = "available"
ROUTINE_GPU_STATUS_UNKNOWN = "unavailable"
ROUTINE_UNNAMED_TASK = "unnamed task"

_ROUTINE_MCP_INSTANCE_ID = secrets.token_hex(16)

ROUTINE_GPU_COUNT_DESCRIPTION = (
    "exact job parallelism from launch script/config (`devices`, "
    "`--nproc_per_node`, `num_processes`, `--gres`), never server/free capacity"
)

MCP_INSTRUCTIONS = """Five tools cover GPU work: gpu_status; gpu_apply picks the cards itself and keeps one lease on one server (task=the task name, never the client UI title); gpu_release; gpu_add_server registers a host (observation_profile: linux-nvidia, linux-host, server-script-v1, or local plugin ID); gpu_update_server updates safe host metadata.
Assess group workspace/environment/data-weight notes, capacity and limits first; choose server_group_id for grouped hosts; the broker best-fits within that group. server_id is for ungrouped compatibility and must not pin a grouped host.
Connection and working directory are projected once per server: ssh=how to connect; workspace.path (workspace_path)=the cwd to enter; code_location=not_provided means workspace_path is never a code repository. Allocation gpus[] point back with server_id.
cuda_device_order=PCI_BUS_ID; cuda_visible_devices=the whole lease, gpu_cuda_visible_devices=one card. Never put a UUID in CUDA_VISIBLE_DEVICES.
gpu_status gives grouped allocatable capacity (name/vram_mib/total_count/available_count), allocation/limits and busy_gpus(task) with no telemetry; server_id narrows to one server. Delegated clusters sit in their server_group; largest_allocatable_block is one apply's max cards.
Telemetry is only meaningful on cards you hold: gpu_status(lease_id=...) returns leased_gpus with recent_average per card plus a lease summary (min_memory_free_mib, slowest_gpu) for tuning batch size and parallelism. Covers your hold only: null until your work is observed; current reads the card now. Load on a free card is ServerPilot's own hold, stopped before allocation, not evidence it is taken.
no_capacity is an answer, not a failure, and nothing is queued; group_selection_required is the same kind of answer; free cards spread across servers also give no_capacity. On any failure call gpu_release and confirm released. gpu_status lists open_leases: every lease still holding cards on this machine, with the lease_id gpu_release needs and running_gpu_count. Read it before you claim and release any whose running_gpu_count is 0 — a finished lease that still holds cards is what makes the next apply answer no_capacity. Idle reclaim is a backstop, not how a card comes back.
ServerPilot only coordinates GPUs. Do not use SSH, SQLite, inventory or nvidia-smi to work around it. Non-GPU remote work such as syncing a repository needs no lease."""


_http_client: httpx.AsyncClient | None = None


def _broker_url() -> str:
    url = os.environ.get("SERVERPILOT_URL", "http://127.0.0.1:8787")
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
        headers = {"X-ServerPilot-Actor": self.actor}
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
        return await self.get("/api/v1/snapshot", params=params)



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


def _client(actor_name: str | None = None) -> BrokerClient | _AsyncBroker:
    if _http_client is None:
        ensure_broker_ready_for_mcp()
    return _broker(actor_name)


def _routine_client() -> BrokerClient | _AsyncBroker:
    return _broker("agent")


def _routine_no_capacity(
    exc: BrokerClientError,
    *,
    gpu_count: int,
    server_id: str | None,
    server_group_id: str | None = None,
) -> dict[str, Any]:
    """Report a documented outcome as data rather than as a tool failure."""

    return {
        "no_capacity": {
            "reason": "no_single_server_satisfies_the_request",
            "message": str(exc).split(": ", 2)[-1],
            "gpu_count": gpu_count,
            "server_id": server_id,
            "server_group_id": server_group_id,
        }
    }


async def _routine_attach_open_leases(documented: dict[str, Any], client: Any) -> None:
    """Give a no_capacity answer the one thing that makes it actionable."""

    detail = documented.get("no_capacity")
    if not isinstance(detail, dict):
        return
    open_leases = await _routine_open_leases_now(client)
    if open_leases:
        detail["open_leases"] = open_leases


async def _routine_open_leases_now(client: Any) -> list[dict[str, Any]]:
    """Leases the caller could release, fetched only when a claim found nothing.

    ``no_capacity`` is an answer, but on its own it is not one anybody can act
    on. What makes it actionable is the set of leases still holding cards and
    whether any of them has stopped working, so the caller can free the cards
    itself instead of waiting out idle reclaim. Costing one read here and none
    on the path that succeeds is the point.
    """

    try:
        payload = await _client_call(
            client, "snapshot", compact=False, endpoint_id=None, only_available=False
        )
    except Exception:
        return []
    return _routine_gpu_status(payload, lease_id=None).get("open_leases", [])


def _routine_group_selection_required(
    exc: BrokerClientError,
    *,
    gpu_count: int,
    server_id: str | None,
    server_group_id: str | None,
) -> dict[str, Any]:
    """Report a missing group choice as data, like ``no_capacity``."""

    payload: dict[str, Any] = {
        "reason": "direct_grouped_hosts_require_server_group_id",
        "message": str(exc).split(": ", 2)[-1],
        "gpu_count": gpu_count,
        "server_id": server_id,
        "server_group_id": server_group_id,
    }
    for key, value in exc.details.items():
        if key in payload and payload[key] is not None:
            continue
        payload[key] = value
    return {"group_selection_required": payload}


def _routine_documented_claim_outcome(
    exc: BrokerClientError,
    *,
    gpu_count: int,
    server_id: str | None,
    server_group_id: str | None,
) -> dict[str, Any] | None:
    """Map broker business answers onto structured MCP data."""

    if exc.code == "no_capacity":
        return _routine_no_capacity(
            exc,
            gpu_count=gpu_count,
            server_id=server_id,
            server_group_id=server_group_id,
        )
    if exc.code == "group_selection_required":
        return _routine_group_selection_required(
            exc,
            gpu_count=gpu_count,
            server_id=server_id,
            server_group_id=server_group_id,
        )
    return None


def _routine_task(task: str | None) -> str:
    if task is None:
        return ROUTINE_UNNAMED_TASK
    value = task.strip()
    if not value:
        raise ValueError("task must not be empty when it is given")
    if len(value) > 120:
        raise ValueError("task must be at most 120 characters")
    return value


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
            first_observed_at
            if isinstance(first_observed_at, str) and first_observed_at
            else None
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


def _routine_status_server(endpoint: Any, server_id: Any, gpus: list[dict[str, Any]]) -> dict[str, Any]:
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


def _routine_group_projection(record: dict[str, Any], servers: list[dict[str, Any]]) -> dict[str, Any]:
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
    return any(
        isinstance(group, dict) and group.get("servers")
        for group in server_groups
    )


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
        if not gpu_ids:
            continue
        leases.append(
            {
                "lease_id": lease.get("id"),
                "task": lease.get("task_ref") or ROUTINE_UNNAMED_TASK,
                "servers": sorted({item.split(":", 1)[0] for item in gpu_ids}),
                "gpu_count": len(gpu_ids),
                "running_gpu_count": sum(
                    1 for item in gpu_ids if status_by_gpu_id.get(item) == "running"
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
    the rest.  Telemetry belongs to exactly one of them: a card is only
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
        status = (
            ROUTINE_GPU_STATUS_AVAILABLE
            if available
            else ROUTINE_GPU_STATUS.get(state, ROUTINE_GPU_STATUS_UNKNOWN)
        )
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
        result.update(_routine_lease_view(lease_id, leased_gpus, lease_windows, lease_task))
    open_leases = _routine_open_leases(data if isinstance(data, dict) else {}, status_by_gpu_id)
    if open_leases:
        result["open_leases"] = open_leases
    if busy_gpus:
        result["busy_gpus"] = busy_gpus
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
        cpu_only_servers.append(
            {
                "server_id": endpoint.get("id"),
                "resource_kind": "cpu_only",
                "monitor_status": monitor.get("status"),
                "cpu_count": _routine_integer(host_telemetry.get("cpu_count")),
                "memory_available_mib": _routine_integer(
                    host_telemetry.get("memory_available_mib")
                ),
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


def _routine_gpu_allocation(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a lease for routine use without conflating lease and GPU scope.

    Connection, workspace and the lease-wide CUDA selector belong to the server,
    so they are published once per server in ``servers`` rather than copied onto
    every GPU row.  A single-server lease also keeps them at the top level,
    which is the path routine callers read first.  Each ``gpus`` row carries its
    own one-GPU selector and retains the UUID as the physical identity.
    """

    lease = payload.get("lease")
    if not isinstance(lease, dict):
        raise ValueError("ServerPilot returned no GPU lease")
    rows: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    for resource in lease.get("resources", []):
        if not isinstance(resource, dict):
            continue
        endpoint = resource.get("endpoint")
        server_id = endpoint.get("id") if isinstance(endpoint, dict) else None
        workspace_path = endpoint.get("workspace_path") if isinstance(endpoint, dict) else None
        ssh = _routine_ssh(endpoint)
        cuda_visible_devices = resource.get("cuda_visible_devices")
        cuda_device_order = resource.get("cuda_device_order")
        if cuda_device_order != "PCI_BUS_ID" or not isinstance(cuda_visible_devices, str):
            raise ValueError("ServerPilot returned an invalid CUDA selector")
        resource_projection = {
            "server_id": server_id,
            "workspace_path": workspace_path,
            "workspace": _routine_workspace(workspace_path),
            "cuda_visible_devices": cuda_visible_devices,
            "cuda_device_order": cuda_device_order,
        }
        if ssh is not None:
            resource_projection["ssh"] = ssh
        resources.append(resource_projection)
        for gpu in resource.get("gpus", []):
            if isinstance(gpu, dict):
                gpu_index = gpu.get("gpu_index")
                cuda_ordinal = gpu.get("cuda_ordinal")
                if (
                    isinstance(cuda_ordinal, bool)
                    or not isinstance(cuda_ordinal, int)
                    or cuda_ordinal < 0
                ):
                    raise ValueError("ServerPilot returned an invalid GPU CUDA ordinal")
                rows.append(
                    {
                        "server_id": server_id,
                        "gpu_id": gpu.get("gpu_uuid"),
                        "gpu_index": gpu_index,
                        "cuda_ordinal": cuda_ordinal,
                        "gpu_cuda_visible_devices": str(cuda_ordinal),
                    }
                )
    result: dict[str, Any] = {"lease_id": lease.get("id"), "servers": resources, "gpus": rows}
    if len(resources) == 1:
        result.update(resources[0])
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def gpu_status(server_id: str | None = None, lease_id: str | None = None) -> dict[str, Any]:
    """List grouped allocatable GPU capacity, busy_gpus and who holds them, CPU-only servers, and scheduler clusters you can request on demand; pass lease_id for per-card telemetry on cards you hold."""

    if server_id is not None:
        server_id = server_id.strip()
        if not server_id:
            raise ValueError("server_id must not be empty when it is given")
    if lease_id is not None:
        lease_id = lease_id.strip()
        if not lease_id:
            raise ValueError("lease_id must not be empty when it is given")
    # Busy cards are filtered in the projection, not by the broker, so one call
    # can name their tasks and still carry the caller's own lease telemetry.
    payload = await _client_call(
        _routine_client(),
        "snapshot",
        compact=False,
        endpoint_id=server_id,
        only_available=False,
    )
    return _routine_gpu_status(payload, lease_id=lease_id)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def gpu_apply(
    server_group_id: str | None = None,
    server_id: str | None = None,
    gpu_count: Annotated[
        int,
        Field(ge=1, le=1024, description=ROUTINE_GPU_COUNT_DESCRIPTION),
    ] = 1,
    task: str | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Claim GPUs on a single server now; pass server_group_id for grouped hosts. Returns SSH, the remote working directory and a CUDA selector. no_capacity and group_selection_required are answers and nothing is queued."""

    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ValueError("gpu_count must be a positive integer")
    if server_group_id is not None:
        server_group_id = server_group_id.strip()
        if not server_group_id:
            raise ValueError("server_group_id must not be empty when it is given")
    if server_id is not None:
        server_id = server_id.strip()
        if not server_id:
            raise ValueError("server_id must not be empty when it is given")
    # One lease is one machine. Cards split across hosts cannot run a
    # single-node job, and holding them would deny the whole gang to someone
    # who can use it, so an unsatisfiable request fails instead.
    constraints: dict[str, Any] = {
        "gpu_count": gpu_count,
        "placement": "pack",
        "same_host": True,
    }
    if server_group_id is not None:
        constraints["server_group_ids"] = [server_group_id]
    if server_id is not None:
        constraints["endpoint_ids"] = [server_id]
    task_ref = _routine_task(task)
    body = {
        "project_id": "agent",
        "task_ref": task_ref,
        "purpose": task_ref,
        "constraints": constraints,
    }
    replay_key = _routine_request_key(context) or f"mcp-call:{secrets.token_hex(16)}"
    client = _routine_client()
    try:
        payload = await _client_call(
            client,
            "post",
            "/api/v1/routine/claims",
            body,
            idempotency_key=replay_key,
        )
    except BrokerClientError as exc:
        documented = _routine_documented_claim_outcome(
            exc,
            gpu_count=gpu_count,
            server_id=server_id,
            server_group_id=server_group_id,
        )
        if documented is not None:
            await _routine_attach_open_leases(documented, client)
            return documented
        if not exc.unsent:
            raise
        # The connection never reached the control plane, so nothing can have
        # been committed. Retry once with the same key so a broker that did
        # somehow see it cannot allocate a second lease for this invocation.
        # A read timeout is deliberately not retried: the request did arrive,
        # and replaying it only doubles the wait for an answer already coming.
        try:
            payload = await _client_call(
                client,
                "post",
                "/api/v1/routine/claims",
                body,
                idempotency_key=replay_key,
            )
        except BrokerClientError as retried:
            documented = _routine_documented_claim_outcome(
                retried,
                gpu_count=gpu_count,
                server_id=server_id,
                server_group_id=server_group_id,
            )
            if documented is None:
                raise
            await _routine_attach_open_leases(documented, client)
            return documented
    return _routine_gpu_allocation(payload)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
)
async def gpu_release(
    lease_id: str,
) -> dict[str, Any]:
    """Give back GPUs claimed earlier; returns the settled lease_id and its final state so each one can be confirmed."""

    if not isinstance(lease_id, str) or not lease_id.strip():
        raise ValueError("lease_id must not be empty")
    lease_id = lease_id.strip()
    try:
        payload = await _client_call(
            _routine_client(), "post", f"/api/v1/routine/leases/{lease_id}/release"
        )
    except BrokerClientError as exc:
        if exc.code != "lease_already_released":
            raise
        # Settling a lease that is already settled reaches the state the caller
        # asked for, so it answers instead of failing. A caller told to release
        # and confirm would otherwise have to treat its own retry as an error.
        return {"released": True, "lease_id": lease_id, "state": "RELEASED"}
    # Echo the lease the broker actually settled so a caller holding several
    # leases can confirm them one by one instead of assuming one release
    # finished the whole task.
    lease = payload.get("lease") if isinstance(payload, dict) else None
    state = lease.get("state") if isinstance(lease, dict) else None
    result: dict[str, Any] = {"released": True, "lease_id": lease_id}
    if isinstance(state, str) and state:
        result["state"] = state
    return result


OBSERVATION_PROFILE_DESCRIPTION = (
    "Built-in observation profiles: linux-nvidia, linux-host, server-script-v1. "
    "A locally discovered plugin ID is also accepted."
)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def gpu_add_server(
    project_id: str,
    host: str,
    workspace_path: str,
    port: int = 22,
    ssh_user: str = "root",
    server_id: str | None = None,
    ssh_alias: str | None = None,
    observation_profile: Annotated[
        str,
        Field(description=OBSERVATION_PROFILE_DESCRIPTION),
    ] = "server-script-v1",
    labels: list[str] | None = None,
    storage_group: str | None = None,
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Create a shared endpoint after explicit current-task human approval; observation_profile accepts linux-nvidia, linux-host, server-script-v1, or a local plugin ID."""

    if not project_id.strip():
        raise ValueError("project_id must not be empty")
    # The replay key is this call's own, like every other tool's. Asking the
    # caller for one asked it to supply a REST concept the instructions
    # deliberately never teach, so the tool could not be called as documented.
    replay_key = _routine_request_key(context) or f"mcp-call:{secrets.token_hex(16)}"
    client = _client("agent")
    generated_id = "server-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:96]
    generated_id = f"{generated_id}-p{port}"
    return await _client_call(
        client,
        "post",
        "/api/v1/endpoints",
        {
            "id": server_id or generated_id,
            "host": host,
            "port": port,
            "ssh_user": ssh_user,
            "ssh_alias": ssh_alias,
            "workspace_path": workspace_path,
            "observation_profile": observation_profile,
            "labels": labels or [],
            "storage_group": storage_group,
            "expected_gpu_count": expected_gpu_count,
            "expected_gpu_total_vram_mib": expected_gpu_total_vram_mib,
            "owner_project_id": project_id,
        },
        idempotency_key=replay_key,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def gpu_update_server(
    server_id: str,
    ssh_user: str | None = None,
    ssh_alias: str | None = None,
    workspace_path: str | None = None,
    observation_profile: Annotated[
        str | None,
        Field(description=OBSERVATION_PROFILE_DESCRIPTION),
    ] = None,
    labels: list[str] | None = None,
    storage_group: str | None = None,
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
    owner_project_id: str | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Update safe endpoint metadata; endpoint id, host, and port are immutable."""

    body = {
        key: value
        for key, value in {
            "ssh_user": ssh_user,
            "ssh_alias": ssh_alias,
            "workspace_path": workspace_path,
            "observation_profile": observation_profile,
            "labels": labels,
            "storage_group": storage_group,
            "expected_gpu_count": expected_gpu_count,
            "expected_gpu_total_vram_mib": expected_gpu_total_vram_mib,
            "owner_project_id": owner_project_id,
        }.items()
        if value is not None
    }
    if not body:
        raise ValueError("gpu_update_server requires at least one safe metadata field")
    replay_key = _routine_request_key(context) or f"mcp-call:{secrets.token_hex(16)}"
    return await _client_call(
        _client("agent"),
        "patch",
        f"/api/v1/endpoints/{server_id}",
        body,
        idempotency_key=replay_key,
    )


ROUTINE_MCP_TOOL_NAMES = (
    "gpu_status",
    "gpu_apply",
    "gpu_release",
    "gpu_add_server",
    "gpu_update_server",
)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
