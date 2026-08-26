"""MCP adapter: tools wrap the broker REST API and never touch SSH/SQLite directly."""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from serverpilot import __version__
from serverpilot.client import BrokerClient, BrokerClientError
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

PLACED_LEASE_STATES = {"HELD", "ACTIVE"}
TERMINAL_REQUEST_STATES = {"CANCELLED", "EXPIRED", "REJECTED", "RELEASED"}
TERMINAL_LEASE_STATES = {"RELEASED", "EXPIRED_EMPTY"}
RESOURCE_MARGINAL_MIN_SAVED_RATIO = 0.10
RESOURCE_MARGINAL_MIN_SAVED_SECONDS = 120
_ROUTINE_MCP_INSTANCE_ID = secrets.token_hex(16)

MCP_INSTRUCTIONS = """Three tools cover routine GPU work: gpu_status; gpu_apply picks the cards itself and keeps one lease on one server (task=the task name, never the client UI title); gpu_release.
Connection and working directory are projected once per server in servers[], not per card: ssh=how to connect; workspace.path (workspace_path)=the cwd to enter; code_location=not_provided means workspace_path is never a code repository; gpus[] point back with server_id.
cuda_device_order=PCI_BUS_ID; cuda_visible_devices=the whole lease, gpu_cuda_visible_devices=one card. Never put a UUID in CUDA_VISIBLE_DEVICES.
gpu_status gives allocatable capacity (name/vram_mib/status) and busy_gpus(task) with no telemetry; server_id narrows to one server. Unaccounted scheduler headroom appears in scheduler_servers, which you can gpu_apply against by server_id.
Telemetry is only meaningful on cards you hold: gpu_status(lease_id=...) returns leased_gpus with recent_average per card plus a lease summary (min_memory_free_mib, slowest_gpu) for tuning batch size and parallelism. Load on a free card is ServerPilot's own hold, stopped before allocation, and is not evidence the card is taken.
no_capacity is an answer, not a failure, and nothing is queued; free cards spread across servers also give no_capacity. On any failure call gpu_release and confirm released. Claim only cards you will use: an idle card is reclaimed on its own.
ServerPilot only coordinates GPUs. Do not use SSH, SQLite, inventory or nvidia-smi to work around it. Non-GPU remote work such as syncing a repository needs no lease."""


class _McpToolModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class SchedulerSubmitOnceRequest(_McpToolModel):
    target_id: str
    project_id: str
    task_ref: str
    purpose: str
    approval_ref: str
    duration_seconds: float
    constraints: dict[str, Any]
    scheduler: dict[str, Any]
    script_body: str


class ResourcePlanCandidate(_McpToolModel):
    candidate_key: str
    quantities: dict[str, Any]
    predicted_runtime_seconds: float
    predicted_saved_seconds: float
    predicted_saved_ratio: float
    satisfies_marginal_threshold: bool


class ResourcePlanEvaluation(_McpToolModel):
    project_id: str
    task_ref: str
    baseline_runtime_seconds: float
    candidates: list[ResourcePlanCandidate]
    selected_candidate_key: str | None = None
    marginal_min_saved_ratio: float = Field(default=RESOURCE_MARGINAL_MIN_SAVED_RATIO)
    marginal_min_saved_seconds: float = Field(default=RESOURCE_MARGINAL_MIN_SAVED_SECONDS)


class ResourceClaimForecast(_McpToolModel):
    quantities: dict[str, Any]
    predicted_runtime_seconds: float


class ResourceClaimBody(_McpToolModel):
    project_id: str
    task_ref: str
    purpose: str
    quantities: dict[str, Any]
    forecast: ResourceClaimForecast


class ResourceRunActual(_McpToolModel):
    project_id: str
    task_ref: str
    quantities: dict[str, Any]
    outcome: str


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
        self._last_state_revision: int | None = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        params: dict[str, Any] | None = None,
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
            )
        except httpx.HTTPError as exc:
            raise BrokerClientError(f"broker request failed: {type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            raise BrokerClientError(
                f"broker returned non-JSON HTTP {response.status_code} ({content_type})"
            ) from exc
        if response.is_error:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            code = error.get("code")
            message = error.get("message", "request failed")
            raise BrokerClientError(
                f"broker HTTP {response.status_code}: {code or 'unknown'}: {message}",
                code=code if isinstance(code, str) else None,
                status_code=response.status_code,
            )
        if not isinstance(payload, dict):
            raise BrokerClientError("broker returned an invalid JSON envelope")
        return payload

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self.request("POST", path, json_body=body, idempotency_key=idempotency_key)

    async def patch(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self.request("PATCH", path, json_body=body, idempotency_key=idempotency_key)

    async def control_plane_state(
        self,
        *,
        minimum_snapshot_revision: int | None = None,
        timeout_seconds: float = 0,
        poll_interval_seconds: float = 0.25,
    ) -> dict[str, Any]:
        if minimum_snapshot_revision is not None and (
            isinstance(minimum_snapshot_revision, bool)
            or not isinstance(minimum_snapshot_revision, int)
            or minimum_snapshot_revision < 0
        ):
            raise BrokerClientError("minimum_snapshot_revision must be a non-negative integer")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
            raise BrokerClientError("timeout_seconds must be a number")
        if isinstance(poll_interval_seconds, bool) or not isinstance(
            poll_interval_seconds, int | float
        ):
            raise BrokerClientError("poll_interval_seconds must be a number")
        timeout_seconds = float(timeout_seconds)
        poll_interval_seconds = float(poll_interval_seconds)
        if not 0 <= timeout_seconds <= 300:
            raise BrokerClientError("timeout_seconds must be between 0 and 300")
        if not 0.05 <= poll_interval_seconds <= 10:
            raise BrokerClientError("poll_interval_seconds must be between 0.05 and 10")

        deadline = time.monotonic() + timeout_seconds
        previous_revision = self._last_state_revision
        while True:
            payload = await self.get("/api/v1/state")
            revision = payload.get("snapshot_revision")
            if isinstance(revision, bool) or not isinstance(revision, int):
                raise BrokerClientError("broker state returned an invalid snapshot_revision")
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("current"), dict):
                raise BrokerClientError("broker state returned an invalid current state")
            if previous_revision is not None and revision < previous_revision:
                raise BrokerClientError(
                    f"broker state revision rolled back from {previous_revision} to {revision}"
                )
            previous_revision = revision
            self._last_state_revision = revision
            if minimum_snapshot_revision is None or revision >= minimum_snapshot_revision:
                return payload
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0:
                raise BrokerClientError(
                    f"broker state revision {revision} is below required {minimum_snapshot_revision}"
                )
            await anyio.sleep(min(poll_interval_seconds, remaining_seconds))

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

    async def gpus(
        self,
        *,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "compact": compact,
            "only_available": only_available,
        }
        if state:
            params["state"] = state
        if endpoint_id:
            params["endpoint_id"] = endpoint_id
        return await self.get("/api/v1/gpus", params=params)

    async def leases(self, *, project_id: str | None = None) -> dict[str, Any]:
        payload = await self.get("/api/v1/leases")
        leases = payload.get("data")
        if not isinstance(leases, list):
            raise BrokerClientError("broker leases response is invalid")
        if project_id:
            leases = [lease for lease in leases if lease.get("project_id") == project_id]
        return {**payload, "data": leases}

    async def workload_profiles(self, *, project_id: str | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return await self.get("/api/v1/workload-profiles", params=params)

    async def scheduler_targets(self) -> dict[str, Any]:
        return await self.get("/api/v1/scheduler-targets")

    async def scheduler_jobs(self, *, project_id: str | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return await self.get("/api/v1/scheduler-jobs", params=params)

    async def coordination(self) -> dict[str, Any]:
        return await self.get("/api/v1/coordination")

    async def resource_providers(
        self,
        *,
        provider_type: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if provider_type is not None:
            params["provider_type"] = provider_type
        if enabled is not None:
            params["enabled"] = enabled
        return await self.get("/api/v1/resource-providers", params=params or None)

    async def resource_monitor(self, *, project_id: str | None = None) -> dict[str, Any]:
        params = {"project_id": project_id} if project_id else None
        return await self.get("/api/v1/resource-monitor", params=params)

    async def resource_claims(
        self,
        *,
        project_id: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if project_id is not None:
            params["project_id"] = project_id
        if state is not None:
            params["state"] = state
        return await self.get("/api/v1/resource-claims", params=params or None)

    async def evaluate_resource_plan(
        self,
        evaluation: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self.post(
            "/api/v1/resource-plan-evaluations",
            evaluation,
            idempotency_key=idempotency_key,
        )

    async def claim_resource(
        self,
        claim: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self.post(
            "/api/v1/resource-claims",
            claim,
            idempotency_key=idempotency_key,
        )

    async def release_resource_claim(
        self,
        claim_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self.post(
            f"/api/v1/resource-claims/{claim_id}/release",
            {"reason": reason},
            idempotency_key=idempotency_key,
        )

    async def record_resource_run_actual(
        self,
        actual: dict[str, Any],
        *,
        claim_id: str | None = None,
        evaluation_id: str | None = None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if claim_id:
            params["claim_id"] = claim_id
        if evaluation_id:
            params["evaluation_id"] = evaluation_id
        return await self.request(
            "POST",
            "/api/v1/resource-run-actuals",
            json_body=actual,
            params=params or None,
            idempotency_key=idempotency_key,
        )


def _broker(actor: str | None) -> BrokerClient | _AsyncBroker:
    if _http_client is not None:
        return _AsyncBroker(_http_client, url=_broker_url(), actor=actor or "agent")
    return BrokerClient.from_env(actor=actor)


async def _client_call(target: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    result = getattr(target, method)(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    raise ValueError("expected an object")


@asynccontextmanager
async def _mcp_lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    global _http_client
    await anyio.to_thread.run_sync(ensure_broker_ready_for_mcp)
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
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
    exc: BrokerClientError, *, gpu_count: int, server_id: str | None
) -> dict[str, Any]:
    """Report a documented outcome as data rather than as a tool failure."""

    return {
        "no_capacity": {
            "reason": "no_single_server_satisfies_the_request",
            "message": str(exc).split(": ", 2)[-1],
            "gpu_count": gpu_count,
            "server_id": server_id,
        }
    }


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
        "recent_average": _routine_recent_telemetry_average(source.get("recent_average")),
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
    current = _routine_gpu_telemetry(gpu, vram_mib=vram_mib)
    if isinstance(current, dict):
        current.pop("recent_average", None)
    return {
        "recent_average": _routine_recent_telemetry_average(source.get("recent_average")),
        "current": current,
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


def _require_request_fields(request: dict[str, Any]) -> None:
    missing = [field for field in ("project_id", "task_ref", "purpose") if not request.get(field)]
    if missing:
        raise ValueError(f"gpu_request requires {', '.join(missing)}")


def _has_resource_quantity(quantities: dict[str, Any]) -> bool:
    return any(
        float(quantities.get(field) or 0) > 0
        for field in ("gpu_count", "cpu_cores", "memory_mib", "nodes", "scheduler_units")
    )


def _require_resource_claim_fields(claim: dict[str, Any]) -> None:
    missing = [
        field
        for field in ("project_id", "task_ref", "purpose", "quantities", "forecast")
        if not claim.get(field)
    ]
    if missing:
        raise ValueError("resource_claim requires " + ", ".join(missing))
    quantities = claim["quantities"]
    if not isinstance(quantities, dict) or not _has_resource_quantity(quantities):
        raise ValueError(
            "resource_claim quantities must request CPU, memory, GPU, nodes, or scheduler units"
        )
    forecast = claim["forecast"]
    if not isinstance(forecast, dict):
        raise ValueError("resource_claim forecast must be a mapping")
    if not isinstance(forecast.get("quantities"), dict) or not forecast.get(
        "predicted_runtime_seconds"
    ):
        raise ValueError(
            "resource_claim forecast requires quantities and predicted_runtime_seconds"
        )


def _require_resource_plan_fields(evaluation: dict[str, Any]) -> None:
    missing = [
        field
        for field in ("project_id", "task_ref", "baseline_runtime_seconds", "candidates")
        if not evaluation.get(field)
    ]
    if missing:
        raise ValueError("resource_evaluate_plan requires " + ", ".join(missing))
    if (
        evaluation.get("marginal_min_saved_ratio", RESOURCE_MARGINAL_MIN_SAVED_RATIO)
        != RESOURCE_MARGINAL_MIN_SAVED_RATIO
    ):
        raise ValueError("resource_evaluate_plan marginal_min_saved_ratio must be 0.10")
    if (
        evaluation.get("marginal_min_saved_seconds", RESOURCE_MARGINAL_MIN_SAVED_SECONDS)
        != RESOURCE_MARGINAL_MIN_SAVED_SECONDS
    ):
        raise ValueError("resource_evaluate_plan marginal_min_saved_seconds must be 120")
    candidates = evaluation["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("resource_evaluate_plan candidates must be a non-empty list")
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"resource_evaluate_plan candidate {index} must be a mapping")
        required = (
            "candidate_key",
            "quantities",
            "predicted_runtime_seconds",
            "predicted_saved_seconds",
            "predicted_saved_ratio",
            "satisfies_marginal_threshold",
        )
        missing_candidate = [field for field in required if field not in candidate]
        if missing_candidate:
            raise ValueError(
                f"resource_evaluate_plan candidate {index} requires " + ", ".join(missing_candidate)
            )
        if not isinstance(candidate["quantities"], dict) or not _has_resource_quantity(
            candidate["quantities"]
        ):
            raise ValueError(
                f"resource_evaluate_plan candidate {index} quantities must include a resource"
            )


def _require_endpoint_admin_contract(approval_ref: str, idempotency_key: str) -> None:
    if not isinstance(approval_ref, str) or not approval_ref.strip():
        raise ValueError("server administration needs explicit authorisation for this task and a non-empty approval_ref")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("server administration needs a stable, non-empty idempotency_key")


def _bounded_seconds(value: float, *, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _matching_request(payload: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    return next((item for item in payload.get("data", []) if item.get("id") == request_id), None)


def _matching_lease(payload: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in payload.get("data", []) if item.get("request_id") == request_id), None
    )


def _compact_gpu_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep the placement facts needed by an Agent and drop unrelated state.

    ``/api/v1/snapshot`` is intentionally the desktop's full revision-consistent
    read model.  MCP status calls should not echo its scheduler, generic-resource,
    history, and profile collections into the model context.
    """

    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    if not data:
        return payload
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    gpus = data.get("gpus")
    if not isinstance(gpus, list):
        gpus = []
    host_capacity = data.get("host_capacity")
    if not isinstance(host_capacity, list):
        host_capacity = []
    capacity_by_endpoint = {
        item.get("endpoint", {}).get("id"): item
        for item in host_capacity
        if isinstance(item, dict) and isinstance(item.get("endpoint"), dict)
    }
    gpu_counts: dict[str, dict[str, int]] = {}
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        endpoint_id = gpu.get("endpoint_id")
        if not endpoint_id:
            continue
        counts = gpu_counts.setdefault(
            endpoint_id,
            {
                "total": 0,
                "available": 0,
                "workload_leased": 0,
                "keepalive_owned": 0,
                "verified_keepalive": 0,
            },
        )
        counts["total"] += 1
        keepalive = gpu.get("keepalive")
        if gpu.get("state") in {"AVAILABLE", "KEEPALIVE"}:
            counts["available"] += 1
        lease = gpu.get("lease")
        if isinstance(lease, dict):
            counts["workload_leased"] += 1
        keepalive = gpu.get("keepalive")
        if isinstance(keepalive, dict) and keepalive.get("lease_id"):
            counts["keepalive_owned"] += 1
        if gpu.get("state") == "KEEPALIVE":
            counts["verified_keepalive"] += 1

    compact_gpus = []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        telemetry = gpu.get("telemetry")
        if isinstance(telemetry, dict):
            telemetry = {
                key: telemetry.get(key)
                for key in (
                    "observed_at",
                    "memory_used_mib",
                    "memory_free_mib",
                    "gpu_utilization_pct",
                    "temperature_c",
                )
            }
        lease = gpu.get("lease")
        lease = lease if isinstance(lease, dict) else {}
        keepalive = gpu.get("keepalive")
        keepalive = keepalive if isinstance(keepalive, dict) else {}
        compact_gpus.append(
            {
                "id": gpu.get("id"),
                "endpoint_id": gpu.get("endpoint_id"),
                "gpu_index": gpu.get("gpu_index"),
                "name": gpu.get("name"),
                "total_vram_mib": gpu.get("total_vram_mib"),
                "state": gpu.get("state"),
                "state_reason": gpu.get("state_reason"),
                "process_count": gpu.get(
                    "process_count",
                    len(gpu.get("processes", [])) if isinstance(gpu.get("processes"), list) else 0,
                ),
                "owner": gpu.get("owner", lease.get("actor_id")),
                "task_ref": gpu.get("task_ref", lease.get("task_ref")),
                "expires_at": gpu.get("expires_at", lease.get("expires_at")),
            }
            | {
                "telemetry": telemetry,
                "keepalive": {
                    "desired": keepalive.get("desired", "OFF"),
                    "actual": keepalive.get("actual", keepalive.get("state", "OFF")),
                    "state": keepalive.get("state", "OFF"),
                    "reason": keepalive.get("reason"),
                },
            }
        )

    servers = []
    for endpoint in data.get("endpoints", []):
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = endpoint.get("id")
        monitor = endpoint.get("monitor") if isinstance(endpoint.get("monitor"), dict) else {}
        host = capacity_by_endpoint.get(endpoint_id, {})
        capacity = host.get("capacity") if isinstance(host, dict) else {}
        capacity = capacity if isinstance(capacity, dict) else {}
        counts = gpu_counts.get(
            endpoint_id,
            {
                "total": 0,
                "available": 0,
                "workload_leased": 0,
                "keepalive_owned": 0,
                "verified_keepalive": 0,
            },
        )
        servers.append(
            {
                "server_id": endpoint_id,
                "workspace_path": endpoint.get("workspace_path"),
                "monitor_status": monitor.get("status"),
                "gpu_count": counts["total"],
                "available_gpu_count": counts["available"],
                "workload_leased_gpu_count": counts["workload_leased"],
                "keepalive_owned_gpu_count": counts["keepalive_owned"],
                "verified_keepalive_gpu_count": counts["verified_keepalive"],
                "available_cpu_cores": capacity.get("available_cpu_cores"),
                "available_memory_mib": capacity.get("available_memory_mib"),
                "total_memory_mib": capacity.get("total_memory_mib"),
                "last_error": monitor.get("last_error"),
            }
        )

    compact_data = {
        "summary": summary,
        "data_age_seconds": data.get("data_age_seconds"),
        "freshness_seconds": data.get("freshness_seconds"),
        "availability_semantics": (
            "Allocatable GPUs include free cards and cards on confirmed per-card idle hold. "
            "Before handing one over, ServerPilot stops that card's hold, re-confirms it, then "
            "allocates normally. Cards running a task or in conflict are not allocatable."
        ),
        "servers": servers,
        "gpus": compact_gpus,
    }
    return {
        "schema_version": payload.get("schema_version", "v1"),
        "snapshot_revision": payload.get("snapshot_revision"),
        "server_time": payload.get("server_time"),
        "data": compact_data,
    }


def _routine_gpu_status(payload: dict[str, Any], *, lease_id: str | None) -> dict[str, Any]:
    """Project the routine status view as three groups that answer three questions.

    ``gpus`` says what can be claimed, ``leased_gpus`` says how the caller's own
    workload is running, and ``busy_gpus`` says who holds the rest.  Telemetry
    belongs to exactly one of them: a card is only readable where its occupancy
    provably belongs to the reader.  On an unclaimed card the observable load is
    ServerPilot's own keepalive hold, which is stopped before allocation, so
    publishing it there would read as somebody else's work and turn a free card
    into a card that looks full.  Capacity is what an unclaimed card can answer.
    """

    data = payload.get("data")
    values = data.get("gpus", []) if isinstance(data, dict) else []
    endpoints = data.get("endpoints", []) if isinstance(data, dict) else []
    endpoint_by_id = {
        endpoint.get("id"): endpoint
        for endpoint in endpoints
        if isinstance(endpoint, dict) and endpoint.get("id")
    }
    gpus: list[dict[str, Any]] = []
    leased_gpus: list[dict[str, Any]] = []
    busy_gpus: list[dict[str, Any]] = []
    referenced_server_ids: list[Any] = []
    lease_windows: list[dict[str, Any] | None] = []
    lease_task: str | None = None

    def reference(server_id: Any) -> None:
        if server_id not in referenced_server_ids:
            referenced_server_ids.append(server_id)

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
        lease = gpu.get("lease")
        lease = lease if isinstance(lease, dict) else None
        server_id = gpu.get("endpoint_id")
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
            reference(server_id)
            row = dict(identity)
            row["name"] = gpu.get("name")
            row["vram_mib"] = gpu.get("total_vram_mib")
            telemetry = _routine_lease_gpu_telemetry(gpu, vram_mib=row["vram_mib"])
            if telemetry is not None:
                row.update(telemetry)
            source = gpu.get("telemetry")
            lease_windows.append(
                _routine_telemetry_window(source.get("recent_average"))
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
            reference(server_id)
            busy_gpus.append({**identity, "status": status, "task": task})
            continue
        reference(server_id)
        gpus.append(
            {
                **identity,
                "name": gpu.get("name"),
                "vram_mib": gpu.get("total_vram_mib"),
                "status": status,
            }
        )

    servers: list[dict[str, Any]] = []
    for server_id in referenced_server_ids:
        endpoint = endpoint_by_id.get(server_id)
        workspace_path = endpoint.get("workspace_path") if isinstance(endpoint, dict) else None
        server: dict[str, Any] = {
            "server_id": server_id,
            "workspace_path": workspace_path,
            "workspace": _routine_workspace(workspace_path),
        }
        ssh = _routine_ssh(endpoint)
        if ssh is not None:
            server["ssh"] = ssh
        servers.append(server)

    result: dict[str, Any] = {"servers": servers, "gpus": gpus}
    if lease_id is not None:
        result.update(_routine_lease_view(lease_id, leased_gpus, lease_windows, lease_task))
    if busy_gpus:
        result["busy_gpus"] = busy_gpus
    scheduler_servers: list[dict[str, Any]] = []
    cpu_only_servers: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        capacity = endpoint.get("scheduler_capacity")
        if (
            isinstance(capacity, dict)
            and isinstance(capacity.get("free_gpu_count"), int)
            and isinstance(capacity.get("gpu_name"), str)
            and capacity["gpu_name"]
        ):
            scheduler_servers.append(
                {
                    "server_id": endpoint.get("id"),
                    "free_gpu_count": capacity["free_gpu_count"],
                    "gpu_name": capacity["gpu_name"],
                    "note": "request on demand; nothing is queued",
                }
            )
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
    if scheduler_servers:
        result["scheduler_servers"] = scheduler_servers
    if cpu_only_servers:
        result["cpu_only_servers"] = cpu_only_servers
    summary = data.get("summary") if isinstance(data, dict) else None
    has_scheduler_free = any(
        isinstance(item.get("free_gpu_count"), int) and item["free_gpu_count"] > 0
        for item in scheduler_servers
    )
    if not gpus and not has_scheduler_free:
        if isinstance(summary, dict) and summary.get("total_gpus") == 0 and not scheduler_servers:
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


def _compact_coordination(payload: dict[str, Any]) -> dict[str, Any]:
    """Project the shared board to its operational fields."""

    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    if not data:
        return payload

    def project_agent(agent: Any) -> dict[str, Any]:
        if not isinstance(agent, dict):
            return {}
        return {
            key: agent.get(key)
            for key in (
                "agent_name",
                "active_leases",
                "active_workload_leases",
                "leased_gpus",
                "managed_running_gpus",
                "idle_leased_gpus",
                "projects",
                "servers",
            )
        }

    def project_lease(lease: Any) -> dict[str, Any]:
        if not isinstance(lease, dict):
            return {}
        return {
            key: lease.get(key)
            for key in (
                "lease_id",
                "agent_name",
                "project_id",
                "task",
                "state",
                "activity",
                "gpu_count",
                "servers",
                "observed_process_count",
                "expires_at",
            )
        }

    def project_server(server: Any) -> dict[str, Any]:
        if not isinstance(server, dict):
            return {}
        capacity = server.get("capacity")
        capacity = capacity if isinstance(capacity, dict) else {}
        return {
            "server_id": server.get("server_id"),
            "workspace_path": server.get("workspace_path"),
            "monitor_status": server.get("monitor_status"),
            "capacity": {
                key: capacity.get(key)
                for key in (
                    "total_gpus",
                    "available_gpus",
                    "leased_gpus",
                    "workload_leased_gpus",
                    "keepalive_owned_gpus",
                    "verified_keepalive_gpus",
                    "managed_running_gpus",
                    "idle_leased_gpus",
                    "unattributed_compute_gpus",
                    "gpu_states",
                    "available_cpu_cores",
                    "available_memory_mib",
                    "total_system_memory_mib",
                    "observed_gpu_utilization_pct",
                )
            },
            "consumers": [project_lease(item) for item in server.get("consumers", [])],
        }

    def project_queue(request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {}
        constraints = request.get("constraints")
        constraints = constraints if isinstance(constraints, dict) else {}
        return {
            "id": request.get("id"),
            "project_id": request.get("project_id"),
            "task_ref": request.get("task_ref"),
            "state": request.get("state"),
            "blocked_reason": request.get("blocked_reason"),
            "gpu_count": constraints.get("gpu_count"),
        }

    def project_signal(signal: Any) -> dict[str, Any]:
        if not isinstance(signal, dict):
            return {}
        return {
            key: signal.get(key)
            for key in (
                "kind",
                "severity",
                "lease_id",
                "agent_name",
                "request_id",
                "scheduler_job_id",
                "state",
                "message",
            )
        }

    def project_scheduler_job(job: Any) -> dict[str, Any]:
        if not isinstance(job, dict):
            return {}
        return {
            key: job.get(key)
            for key in (
                "id",
                "target_id",
                "project_id",
                "task_ref",
                "state",
                "scheduler_job_id",
                "allocated_tres",
                "node_list",
                "error_message",
            )
        }

    compact_data: dict[str, Any] = {
        "summary": data.get("summary", {}),
        "agents": [project_agent(item) for item in data.get("agents", [])],
        "leases": [project_lease(item) for item in data.get("leases", [])],
        "servers": [project_server(item) for item in data.get("servers", [])],
        "queue": [project_queue(item) for item in data.get("queue", [])],
        "signals": [project_signal(item) for item in data.get("signals", [])],
        "scheduler_jobs": [project_scheduler_job(item) for item in data.get("scheduler_jobs", [])],
        "guidance": data.get("guidance"),
    }
    return {
        "schema_version": payload.get("schema_version", "v1"),
        "snapshot_revision": payload.get("snapshot_revision"),
        "server_time": payload.get("server_time"),
        "data": compact_data,
    }


@mcp.tool()
async def control_plane_state(
    agent_name: str | None = None,
    minimum_snapshot_revision: int | None = None,
    timeout_seconds: float = 0,
    poll_interval_seconds: float = 0.25,
) -> dict[str, Any]:
    """Return the canonical broker state envelope from one control-plane revision."""

    return await _client_call(
        _client(agent_name),
        "control_plane_state",
        minimum_snapshot_revision=minimum_snapshot_revision,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
async def gpu_status(server_id: str | None = None, lease_id: str | None = None) -> dict[str, Any]:
    """List allocatable GPUs, busy_gpus and who holds them, CPU-only servers, and scheduler clusters you can request on demand; pass lease_id for per-card telemetry on cards you hold."""

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


@mcp.tool()
async def gpu_coordination(compact: bool = True) -> dict[str, Any]:
    """Return a read-only coordination board of visible agents, workload leases and server capacity."""

    payload = await _client_call(_client(), "coordination")
    return _compact_coordination(payload) if compact else payload


@mcp.tool()
async def gpu_list(
    state: str | None = None,
    server_id: str | None = None,
    only_available: bool = False,
    compact: bool = True,
) -> dict[str, Any]:
    """Advanced read: list project-visible GPUs from the narrow REST projection."""

    return await _client_call(
        _client(),
        "gpus",
        state=state,
        endpoint_id=server_id,
        only_available=only_available,
        compact=compact,
    )


@mcp.tool()
async def gpu_who(project_id: str | None = None) -> dict[str, Any]:
    """List project-visible leases and workload bindings; returns no SSH or task secrets."""

    return await _client_call(_client(), "leases", project_id=project_id)


@mcp.tool()
async def gpu_scheduler_targets() -> dict[str, Any]:
    """List globally registered external scheduler targets.

    Scheduler targets are not raw GPU servers. Their login helpers and access
    hints are metadata; Slurm remains the resource allocator.
    """

    return await _client_call(_client(), "scheduler_targets")


@mcp.tool()
async def gpu_scheduler_access_status(target_id: str) -> dict[str, Any]:
    """Check whether an external scheduler is reachable through its approved access path.

    This read-only check does not connect or change VPN state and does not submit a job.
    """

    return await _client_call(
        _client(), "get", f"/api/v1/scheduler-targets/{target_id}/access"
    )


@mcp.tool()
async def gpu_scheduler_profiles(project_id: str) -> dict[str, Any]:
    """List enabled Slurm profiles explicitly granted to a project."""

    result = await _client_call(_client(), "workload_profiles", project_id=project_id)
    result["data"] = [
        profile for profile in result.get("data", []) if profile.get("runtime_kind") == "slurm"
    ]
    return result


@mcp.tool()
async def gpu_scheduler_submit_profile(
    agent_name: str,
    profile_id: str,
    project_id: str,
    task: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Submit a project-owned Slurm profile for its current task."""

    if not profile_id.strip() or not project_id.strip() or not task.strip():
        raise ValueError("profile_id, project_id and task must not be empty")
    return await _client_call(
        _client(agent_name),
        "post",
        f"/api/v1/workload-profiles/{profile_id}/scheduler-submit",
        {"project_id": project_id, "task_ref": task},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_scheduler_submit_once(
    agent_name: str,
    request: SchedulerSubmitOnceRequest,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Submit one project-owned Slurm script and bounded resource request.

    The request must include target_id, project_id, task_ref, purpose,
    approval_ref, duration_seconds, constraints, scheduler, and script_body.
    ServerPilot omits the submitted body unless retain_submission_body is explicitly true.
    """

    request_body = _mapping(request)
    required = {
        "target_id",
        "project_id",
        "task_ref",
        "purpose",
        "approval_ref",
        "duration_seconds",
        "constraints",
        "scheduler",
        "script_body",
    }
    missing = sorted(field for field in required if not request_body.get(field))
    if missing:
        raise ValueError("gpu_scheduler_submit_once requires " + ", ".join(missing))
    return await _client_call(
        _client(agent_name),
        "post",
        "/api/v1/scheduler-jobs",
        request_body,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_scheduler_job_status(
    agent_name: str,
    job_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Read one live Slurm job or list the ServerPilot's persisted scheduler jobs."""

    client = _client(agent_name)
    if job_id:
        return await _client_call(client, "get", f"/api/v1/scheduler-jobs/{job_id}")
    return await _client_call(client, "scheduler_jobs", project_id=project_id)


@mcp.tool()
async def gpu_scheduler_cancel(
    agent_name: str,
    job_id: str,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Cancel a Slurm job owned by the calling project."""

    if not job_id.strip() or not reason.strip():
        raise ValueError("job_id and reason must not be empty")
    return await _client_call(
        _client(agent_name),
        "post",
        f"/api/v1/scheduler-jobs/{job_id}/cancel",
        {"reason": reason},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_scheduler_upload(
    agent_name: str,
    target_id: str,
    project_id: str,
    local_path: str,
    remote_directory: str,
    approval_ref: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Compatibility helper for the deferred staged-upload API.

    It is intentionally not exposed as an MCP tool. Keep it import-compatible
    while the public scheduler contract does not offer transfer operations.
    """

    if not all(
        value.strip()
        for value in (
            agent_name,
            target_id,
            project_id,
            local_path,
            remote_directory,
            approval_ref,
        )
    ):
        raise ValueError("all staged upload fields must not be empty")
    return _client(agent_name).post(
        "/api/v1/scheduler-transfers",
        {
            "target_id": target_id,
            "project_id": project_id,
            "local_path": local_path,
            "remote_directory": remote_directory,
            "approval_ref": approval_ref,
        },
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_scheduler_transfer_status(
    agent_name: str,
    transfer_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility helper for the deferred staged-upload API."""

    client = _client(agent_name)
    return client.scheduler_transfers(
        transfer_id=transfer_id,
        project_id=project_id,
    )


def gpu_request(request: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
    """Submit an atomic GPU request. Required: project_id, task_ref, purpose, and constraints."""

    _require_request_fields(request)
    return _client().post(
        "/api/v1/requests",
        request,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_request_status(request_id: str | None = None) -> dict[str, Any]:
    """List visible requests or return one request by id."""

    return _client().requests(request_id=request_id)


def gpu_wait_for_claim(
    agent_name: str,
    request_id: str,
    timeout_seconds: float = 25,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Poll visible request and lease state until a prior claim is placed, terminal, or timed out."""

    agent_name = agent_name.strip()
    request_id = request_id.strip()
    if not agent_name or not request_id:
        raise ValueError("agent_name and request_id must not be empty")
    timeout_seconds = _bounded_seconds(
        timeout_seconds, name="timeout_seconds", minimum=0, maximum=300
    )
    poll_interval_seconds = _bounded_seconds(
        poll_interval_seconds, name="poll_interval_seconds", minimum=0.1, maximum=10
    )

    client = _client(agent_name)
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    polls = 0
    request: dict[str, Any] | None = None
    lease: dict[str, Any] | None = None

    while True:
        polls += 1
        state_payload = client.control_plane_state()
        current = state_payload["data"]["current"]
        requests_payload = {
            "schema_version": state_payload.get("schema_version", "v1"),
            "snapshot_revision": state_payload["snapshot_revision"],
            "server_time": state_payload.get("server_time"),
            "data": current.get("requests", []),
        }
        leases_payload = {
            "schema_version": state_payload.get("schema_version", "v1"),
            "snapshot_revision": state_payload["snapshot_revision"],
            "server_time": state_payload.get("server_time"),
            "data": current.get("leases", []),
        }
        request = _matching_request(requests_payload, request_id)
        lease = _matching_lease(leases_payload, request_id)
        elapsed_seconds = round(time.monotonic() - started_at, 3)

        if request is None:
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "not_found",
                "request": None,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": elapsed_seconds,
            }
        if lease is not None and lease.get("state") in PLACED_LEASE_STATES:
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "allocated",
                "request": request,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": elapsed_seconds,
            }
        if request.get("state") in TERMINAL_REQUEST_STATES or (
            lease is not None and lease.get("state") in TERMINAL_LEASE_STATES
        ):
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "terminal",
                "request": request,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": elapsed_seconds,
            }

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return {
                "schema_version": requests_payload.get("schema_version", "v1"),
                "snapshot_revision": requests_payload["snapshot_revision"],
                "server_time": requests_payload.get("server_time"),
                "state": "timeout",
                "request": request,
                "lease": lease,
                "polls": polls,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            }
        time.sleep(min(poll_interval_seconds, remaining_seconds))


def gpu_cancel_request(request_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Cancel the caller's queued request. This does not stop a workload."""

    return _client().post(
        f"/api/v1/requests/{request_id}/cancel",
        {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_activate_lease(lease_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Record that a held lease is active; it does not launch any command."""

    return await _client_call(
        _client(),
        "post",
        f"/api/v1/leases/{lease_id}/activate",
        {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_renew_lease(lease_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """Renew a workload lease this caller holds."""

    return await _client_call(
        _client(),
        "post",
        f"/api/v1/leases/{lease_id}/renew",
        {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_release_lease(
    lease_id: str, reason: str, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Release a lease cooperatively. Real observed compute processes remain fail-closed."""

    return await _client_call(
        _client(),
        "post",
        f"/api/v1/leases/{lease_id}/release",
        {"reason": reason},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_bind_workload(
    lease_id: str,
    run_id: str,
    process_keys: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Bind a lease to a sanitized run/process identity for later reconciliation."""

    return await _client_call(
        _client(),
        "post",
        f"/api/v1/leases/{lease_id}/bind-workload",
        {"run_id": run_id, "process_keys": process_keys or []},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_bind_observed_workload(
    lease_id: str,
    run_id: str | None = None,
    idempotency_key: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Bind the latest observed processes of an already-started task to this caller's lease; never starts or changes a remote workload."""

    return await _client_call(
        _client(agent_name),
        "post",
        f"/api/v1/leases/{lease_id}/bind-observed-workload",
        {"run_id": run_id} if run_id else {},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_list_reservations() -> dict[str, Any]:
    """List visible future GPU reservations."""

    return _client().reservations()


@mcp.tool()
async def gpu_history(after_id: int = 0, limit: int = 20) -> dict[str, Any]:
    """Read the append-only, redacted audit history for visible resources."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be an integer between 1 and 200")
    return await _client_call(
        _client(), "get", "/api/v1/events", params={"after_id": after_id, "limit": limit}
    )


@mcp.tool()
async def resource_providers(
    agent_name: str | None = None,
    provider_type: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """List generic resource providers: direct GPU, host CPU/memory capacity, and external schedulers."""

    return await _client_call(
        _client(agent_name), "resource_providers", provider_type=provider_type, enabled=enabled
    )


@mcp.tool()
async def resource_monitor(
    agent_name: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Return real-time human/agent monitor data for generic resources and active claims."""

    return await _client_call(_client(agent_name), "resource_monitor", project_id=project_id)


@mcp.tool()
async def resource_claims(
    agent_name: str | None = None,
    project_id: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """List generic resource claims across visible projects and agents."""

    return await _client_call(
        _client(agent_name), "resource_claims", project_id=project_id, state=state
    )


@mcp.tool()
async def resource_evaluate_plan(
    agent_name: str,
    evaluation: ResourcePlanEvaluation,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Persist an explicit marginal-utility resource decision.

    The evaluation must include candidate forecasts. The only accepted expansion
    threshold is 10% remaining-time savings and 120 seconds absolute savings.
    """

    evaluation_body = _mapping(evaluation)
    _require_resource_plan_fields(evaluation_body)
    return await _client_call(
        _client(agent_name),
        "evaluate_resource_plan",
        evaluation_body,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def resource_claim(
    agent_name: str,
    claim: ResourceClaimBody,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Claim the selected generic resource plan.

    Claims must include explicit quantities and a forecast. The ServerPilot returns
    the placement; a queued or null allocation is not permission to run.
    """

    claim_body = _mapping(claim)
    _require_resource_claim_fields(claim_body)
    return await _client_call(
        _client(agent_name),
        "claim_resource",
        claim_body,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def resource_release(
    agent_name: str,
    claim_id: str,
    reason: str = "workload_completed",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Release a generic resource claim; this never stops remote work."""

    if not claim_id.strip():
        raise ValueError("claim_id must not be empty")
    return await _client_call(
        _client(agent_name),
        "release_resource_claim",
        claim_id,
        reason=reason,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def resource_record_actual(
    agent_name: str,
    actual: ResourceRunActual,
    claim_id: str | None = None,
    evaluation_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record observed runtime and outcome for later scheduling calibration and human monitoring."""

    actual_body = _mapping(actual)
    if (
        not actual_body.get("project_id")
        or not actual_body.get("task_ref")
        or not actual_body.get("quantities")
        or not actual_body.get("outcome")
    ):
        raise ValueError(
            "resource_record_actual requires project_id, task_ref, quantities, and outcome"
        )
    if not isinstance(actual_body["quantities"], dict) or not _has_resource_quantity(
        actual_body["quantities"]
    ):
        raise ValueError("resource_record_actual quantities must include a resource")
    return await _client_call(
        _client(agent_name),
        "record_resource_run_actual",
        actual_body,
        claim_id=claim_id,
        evaluation_id=evaluation_id,
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_claim(
    agent_name: str,
    project_id: str,
    task: str,
    gpu_count: int,
    server_id: str | None = None,
    gpu_ids: list[str] | None = None,
    min_available_cpu_cores: float | None = None,
    min_available_memory_mib: int | None = None,
    min_free_vram_mib: int | None = None,
    min_total_vram_mib: int | None = None,
    purpose: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Advanced compatibility helper for explicit direct-GPU claim contracts."""

    task = task.strip()
    if gpu_count < 1 or not task:
        raise ValueError("task must not be empty and gpu_count must be positive")
    if min_available_cpu_cores is not None and min_available_cpu_cores < 0:
        raise ValueError("min_available_cpu_cores must be non-negative")
    if min_available_memory_mib is not None and min_available_memory_mib < 0:
        raise ValueError("min_available_memory_mib must be non-negative")
    if min_free_vram_mib is not None and min_free_vram_mib < 0:
        raise ValueError("min_free_vram_mib must be non-negative")
    if min_total_vram_mib is not None and min_total_vram_mib < 1:
        raise ValueError("min_total_vram_mib must be positive")
    exact_gpu_ids = gpu_ids or []
    constraints = {
        "gpu_count": len(exact_gpu_ids) or gpu_count,
        "placement": "exact" if exact_gpu_ids else "pack",
        "endpoint_ids": [server_id] if server_id else [],
        "gpu_ids": exact_gpu_ids,
    }
    if min_available_cpu_cores is not None:
        constraints["min_available_cpu_cores"] = min_available_cpu_cores
    if min_available_memory_mib is not None:
        constraints["min_available_memory_mib"] = min_available_memory_mib
    if min_free_vram_mib is not None:
        constraints["min_free_vram_mib"] = min_free_vram_mib
    if min_total_vram_mib is not None:
        constraints["min_total_vram_mib"] = min_total_vram_mib
    return _client(agent_name).post(
        "/api/v1/claims",
        {
            "project_id": project_id,
            "task_ref": task,
            "purpose": (purpose or task).strip(),
            "constraints": constraints,
        },
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


def gpu_claim_profile(
    profile_id: str,
    task: str,
    idempotency_key: str | None = None,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Advanced compatibility helper for a direct-GPU workload profile."""

    if not profile_id.strip() or not task.strip():
        raise ValueError("profile_id and task must not be empty")
    return _client(agent_name).post(
        f"/api/v1/workload-profiles/{profile_id}/claim",
        {"task_ref": task},
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
async def gpu_apply(
    server_id: str | None = None,
    gpu_count: int = 1,
    task: str | None = None,
    context: Context | None = None,
) -> dict[str, Any]:
    """Claim GPUs on a single server now; returns SSH, the remote working directory and a CUDA selector. no_capacity is an answer and nothing is queued."""

    if isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 1:
        raise ValueError("gpu_count must be a positive integer")
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
        if exc.code == "no_capacity":
            return _routine_no_capacity(exc, gpu_count=gpu_count, server_id=server_id)
        if not str(exc).startswith("broker request failed:"):
            raise
        # The broker may have committed before the local HTTP response was
        # interrupted. Retry once with the same key so it cannot allocate a
        # second lease for this tool invocation.
        try:
            payload = await _client_call(
                client,
                "post",
                "/api/v1/routine/claims",
                body,
                idempotency_key=replay_key,
            )
        except BrokerClientError as retried:
            if retried.code != "no_capacity":
                raise
            return _routine_no_capacity(retried, gpu_count=gpu_count, server_id=server_id)
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


def gpu_schedule(
    agent_name: str,
    project_id: str,
    gpu_ids: list[str],
    start_at: str,
    end_at: str,
    reason: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Reserve specific GPUs for a future ISO-8601 time window."""

    client = _client(agent_name)
    return client.post(
        "/api/v1/reservations",
        {
            "project_id": project_id,
            "gpu_ids": gpu_ids,
            "start_at": start_at,
            "end_at": end_at,
            "reason": reason,
        },
        idempotency_key=idempotency_key or secrets.token_hex(16),
    )


@mcp.tool()
async def gpu_add_server(
    agent_name: str,
    project_id: str,
    host: str,
    workspace_path: str,
    approval_ref: str,
    idempotency_key: str,
    port: int = 22,
    ssh_user: str = "root",
    server_id: str | None = None,
    ssh_alias: str | None = None,
    observation_profile: str = "server-script-v1",
    labels: list[str] | None = None,
    storage_group: str | None = None,
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
) -> dict[str, Any]:
    """Create a shared endpoint after explicit current-task human approval."""

    if not project_id.strip():
        raise ValueError("project_id must not be empty")
    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    client = _client(agent_name)
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
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def gpu_update_server(
    agent_name: str,
    server_id: str,
    approval_ref: str,
    idempotency_key: str,
    ssh_user: str | None = None,
    ssh_alias: str | None = None,
    workspace_path: str | None = None,
    observation_profile: str | None = None,
    labels: list[str] | None = None,
    storage_group: str | None = None,
    expected_gpu_count: int | None = None,
    expected_gpu_total_vram_mib: int | None = None,
    owner_project_id: str | None = None,
) -> dict[str, Any]:
    """Update safe endpoint metadata; endpoint id, host, and port are immutable."""

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
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
    return await _client_call(
        _client(agent_name),
        "patch",
        f"/api/v1/endpoints/{server_id}",
        body,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def gpu_set_keepalive(
    agent_name: str,
    server_id: str,
    enabled: bool,
    approval_ref: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Turn a server's per-card idle hold policy on or off, with explicit authorisation.

    Each idle GPU is coordinated on its own. The caller cannot supply a GPU
    target, a PID, a shell fragment, or helper arguments. An agent request and a
    manual reassignment in the app both reuse the same per-card yield path.
    """

    _require_endpoint_admin_contract(approval_ref, idempotency_key)
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    return await _client_call(
        _client(agent_name),
        "post",
        f"/api/v1/endpoints/{server_id}/keepalive",
        {"enabled": enabled},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def gpu_list_observation_profiles() -> dict[str, Any]:
    """List built-in observation profiles and discovered local server plugins to choose an observation_profile."""

    from serverpilot.plugins import list_observation_profiles

    return {"profiles": list_observation_profiles()}


ROUTINE_MCP_TOOL_NAMES = (
    "gpu_status",
    "gpu_apply",
    "gpu_release",
)


def _build_routine_mcp() -> FastMCP:
    """Build the small default surface while retaining compatibility tools."""

    routine = _build_server(MCP_INSTRUCTIONS)
    routine.add_tool(
        gpu_status,
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    )
    routine.add_tool(
        gpu_apply,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    routine.add_tool(
        gpu_release,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    return routine


# ``mcp`` remains the import-compatible full registry for REST/MCP tests and
# advanced callers.  The stdio entry point uses this smaller registry by
# default, so tool discovery is intent-first rather than compatibility-first.
routine_mcp = _build_routine_mcp()


def main() -> None:
    profile = os.environ.get("SERVERPILOT_MCP_PROFILE", "routine").strip().lower()
    if profile == "advanced":
        mcp.run()
        return
    if profile != "routine":
        raise SystemExit("SERVERPILOT_MCP_PROFILE must be 'routine' or 'advanced'")
    routine_mcp.run()


if __name__ == "__main__":
    main()
