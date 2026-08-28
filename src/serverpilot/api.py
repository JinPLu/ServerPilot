"""FastAPI REST and SSE surfaces."""

import asyncio
import contextlib
import os
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from serverpilot import API_CAPABILITIES, SCHEMA_VERSION, __version__
from serverpilot.adapters import AdapterCommandError, endpoint_keepalive_adapter
from serverpilot.collector import SSHCollector
from serverpilot.config import Settings, load_inventory
from serverpilot.database import Database
from serverpilot.keepalive_protocol import KEEPALIVE_WORKER_MARKER
from serverpilot.mcp_entry import mcp_entry_status
from serverpilot.schemas import (
    CollectorSettingsUpdate,
    ControlPlaneSnapshot,
    EndpointCreate,
    EndpointKeepaliveRequest,
    EndpointUpdate,
    LeaseBind,
    LeaseGPUAssignment,
    LeaseObservedBind,
    RequestCreate,
    ServerGroupCreate,
    ServerGroupUpdate,
)
from serverpilot.service import SYSTEM_ACTOR_ID, ActorContext, BrokerError, BrokerService
from serverpilot.timeutil import utcnow


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, actor_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[actor_id]
            while hits and hits[0] <= now - 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                raise BrokerError(
                    "rate_limited",
                    "rate limit exceeded; retry after one minute",
                    status_code=429,
                )
            hits.append(now)


class RequestBodyLimitMiddleware:
    """Enforce the configured limit against bytes actually received."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = None
            if declared_length is not None and declared_length > self.max_bytes:
                await self._reject(scope, receive, send)
                return

        received = 0
        messages: list[dict[str, Any]] = []
        while True:
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
            messages.append(message)
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break

        message_iterator = iter(messages)
        sentinel = object()

        async def replay_receive() -> dict[str, Any]:
            message = next(message_iterator, sentinel)
            if message is sentinel:
                # Keep forwarding lifecycle events such as http.disconnect
                # after the buffered request body has been replayed.
                return await receive()
            return message

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope: dict[str, Any], receive: Any, send: Any) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {"code": "body_too_large", "message": "请求内容过大。"},
            },
        )
        await response(scope, receive, send)


def _idempotency_key(value: str | None) -> str:
    if not value:
        raise BrokerError(
            "idempotency_key_required",
            "Idempotency-Key header is required for every mutation",
            status_code=422,
        )
    return value


def _public_error_message(exc: BrokerError) -> str:
    messages = {
        "no_capacity": "当前没有满足本次申请的可用 GPU；本次申请未排队。",
        "keepalive_cuda_target_unavailable": (
            "远端占卡程序已启动，但 PyTorch/CUDA 没有识别出唯一目标 GPU；"
            "请检查这台服务器的 CUDA 运行环境。"
        ),
        "keepalive_cuda_runtime_unavailable": (
            "远端 PyTorch 已安装 CUDA 支持，但无法初始化目标 GPU；"
            "请检查这台服务器的驱动与 CUDA 运行环境。"
        ),
        "keepalive_cuda_architecture_unsupported": (
            "远端 PyTorch 的 CUDA 内核不支持这台服务器的 GPU 架构。"
        ),
        "keepalive_pytorch_cuda_required": ("远端占卡程序使用的 Python 缺少支持 CUDA 的 PyTorch。"),
        "keepalive_cuda_index_mapping_failed": (
            "远端占卡程序无法把目标 GPU UUID 映射到当前 CUDA 设备编号。"
        ),
        "keepalive_cuda_uuid_not_found": ("远端当前 PCI GPU 清单中找不到目标 GPU UUID。"),
        "keepalive_helper_incompatible": (
            "远端占卡 helper 版本或能力不匹配；请先完成该服务器的 helper 升级。"
        ),
        "keepalive_attestation_invalid": "远端占卡程序的身份校验未通过；该 GPU 暂不可申请。",
        "keepalive_outcome_uncertain": "占卡程序返回结果不确定，本次没有分配任务。",
        "keepalive_adapter_failed": "占卡程序启动或停止失败；下一采集周期会继续尝试。",
        "keepalive_cleanup_failed": "占卡异常且未能完成清理；请在 APP 中确认该 GPU 的实际状态。",
        "keepalive_observation_stale": "占卡操作后没有取得完整的新状态；下一采集周期会继续尝试。",
        "keepalive_observation_incomplete": "占卡操作后没有取得完整的新状态；下一采集周期会继续尝试。",
        "keepalive_process_missing": "未检测到占卡程序；该卡仍按可用显示，下一采集周期会继续尝试。",
        "keepalive_process_still_running": "占卡程序仍在运行，本次没有分配任务。",
        "keepalive_partial_stop": "部分 GPU 未能确认让位；本次没有分配任务。",
        "gpu_already_assigned": "选中的 GPU 已分给其他任务。",
        "lease_not_found": "找不到这个 GPU 租约。",
        "lease_forbidden": "不能操作其他 Agent 的 GPU 租约。",
        "lease_already_released": "这个 GPU 租约已经结束。",
        "workload_process_not_observed": "每张已分配 GPU 都检测到新的任务进程后才能完成绑定。",
        "idempotency_key_required": "本次写操作缺少 idempotency_key。",
        "endpoint_not_found": "这台服务器已经不在本机资源池中。",
        "endpoint_has_active_leases": "这台服务器上还有进行中的租约，请先释放后再删除。",
        "endpoint_has_active_allocations": "这台服务器上还有进行中的资源分配，请先结束后再删除。",
        "group_selection_required": "请先选择服务器分组后再申请 GPU。",
        "ungrouped_endpoint_required": "未分组申请请选择一台未分组服务器。",
        "server_group_not_found": "找不到这个服务器分组。",
        "server_group_exists": "这个服务器分组编号已经存在。",
        "server_group_has_members": "这个分组下还有服务器，请先解绑后再删除。",
        "endpoint_workspace_required": "服务器需要工作目录，或加入带默认工作目录的分组。",
    }
    if exc.code in messages:
        return messages[exc.code]
    if re.search(r"[\u4e00-\u9fff]", exc.message):
        return exc.message
    return f"操作未完成（{exc.code}）。"


def _keepalive_adapter_failure_code(exc: AdapterCommandError) -> str:
    """Preserve a known actionable helper failure without exposing remote stderr."""

    message = str(exc)
    if "keepalive_helper_incompatible" in message:
        return "keepalive_helper_incompatible"
    if "PyTorch with CUDA support is required" in message:
        return "keepalive_pytorch_cuda_required"
    if "PyTorch CUDA runtime could not initialize the selected GPU" in message:
        return "keepalive_cuda_runtime_unavailable"
    if "no kernel image is available for execution on the device" in message:
        return "keepalive_cuda_architecture_unsupported"
    if "does not contain requested GPU UUID" in message:
        return "keepalive_cuda_uuid_not_found"
    if "keepalive CUDA PCI ordinal mapping" in message:
        return "keepalive_cuda_index_mapping_failed"
    if (
        "exactly one CUDA GPU must be visible" in message
        or "CUDA visible device count is" in message
    ):
        return "keepalive_cuda_target_unavailable"
    return "keepalive_outcome_uncertain" if exc.uncertain else "keepalive_adapter_failed"


def _public_keepalive_result(
    endpoint_id: str,
    keepalive: dict[str, Any],
    *,
    event_id: int | None = None,
    snapshot_revision: int | None = None,
) -> dict[str, Any]:
    """Return the occupancy policy and current per-GPU coverage."""

    policy = keepalive.get("policy")
    if not isinstance(policy, str) or policy not in {"disabled", "idle_keepalive"}:
        raise BrokerError(
            "invalid_keepalive_protocol",
            "服务返回了无法识别的空闲占卡策略。",
            status_code=500,
        )
    desired = keepalive.get("desired")
    actual = keepalive.get("actual")
    if desired not in {"ON", "OFF"} or actual not in {"ON", "OFF", "ERROR"}:
        raise BrokerError(
            "invalid_keepalive_protocol",
            "服务返回了无法识别的空闲占卡状态。",
            status_code=500,
        )
    public = {
        "endpoint_id": endpoint_id,
        "enabled": policy == "idle_keepalive",
        "policy": policy,
        "desired": desired,
        "actual": actual,
        # Compatibility alias for clients that have not yet switched to the
        # explicit desired/actual pair.
        "state": actual,
        "configured": bool(keepalive.get("configured", False)),
        "active_gpu_count": max(0, int(keepalive.get("active_gpu_count") or 0)),
        "error_gpu_count": max(0, int(keepalive.get("error_gpu_count") or 0)),
        "eligible_idle_gpu_count": max(0, int(keepalive.get("eligible_idle_gpu_count") or 0)),
    }
    message = keepalive.get("message")
    if isinstance(message, str) and message:
        public["message"] = message
    reasons = keepalive.get("reasons")
    if isinstance(reasons, list):
        public_reasons: list[str] = []
        for item in reasons:
            if isinstance(item, str):
                public_reasons.append(item)
            elif isinstance(item, dict) and isinstance(item.get("reason"), str):
                # Domain diagnostics may be keyed by an internal GPU ID.  The
                # endpoint API intentionally exposes the explanation only.
                public_reasons.append(item["reason"])
        if public_reasons:
            public["reasons"] = public_reasons[:16]
    return {
        "event_id": event_id,
        "snapshot_revision": snapshot_revision,
        "keepalive": public,
    }


def _plugin_overlay_gpu_ids(endpoint_id: str, overlay: Mapping[str, Any]) -> list[str]:
    gpus = overlay.get("gpus")
    if not isinstance(gpus, list):
        return []
    gpu_ids: list[str] = []
    for item in gpus:
        if not isinstance(item, dict):
            continue
        uuid = item.get("gpu_uuid")
        if isinstance(uuid, str) and uuid:
            gpu_ids.append(f"{endpoint_id}:{uuid}")
    return gpu_ids


def claim_candidate_endpoint_ids(
    request_data: RequestCreate,
    endpoints: list[Any],
) -> set[str]:
    """Endpoints this claim might select; lock them before the allocator runs.

    The keeper-start / claim race is per endpoint. Pinning or a group narrows
    the lock to that set. With neither constraint the allocator may pick any
    host, so every collectable endpoint stays locked.
    """

    pinned = request_data.constraints.endpoint_ids
    if pinned:
        return set(pinned)
    group_ids = set(request_data.constraints.server_group_ids)
    if group_ids:
        return {
            endpoint.id
            for endpoint in endpoints
            if endpoint.server_group_id in group_ids
        }
    return {endpoint.id for endpoint in endpoints}


def create_app(
    settings: Settings,
    *,
    collector: SSHCollector | None = None,
    keepalive_adapter_resolver: Callable[[str], Any] = endpoint_keepalive_adapter,
) -> FastAPI:
    inventory = load_inventory(settings.inventory_path)
    project_root = settings.project_root or _find_project_root()
    service = BrokerService(Database(settings.database_url, project_root), inventory)
    service.initialize()
    shared_collector = collector or SSHCollector(inventory)
    keepalive_reconcile_locks: dict[str, asyncio.Lock] = {}
    keepalive_reconcile_tasks: set[asyncio.Task[None]] = set()

    def keepalive_reconcile_lock(endpoint_id: str) -> asyncio.Lock:
        lock = keepalive_reconcile_locks.get(endpoint_id)
        if lock is None:
            lock = asyncio.Lock()
            keepalive_reconcile_locks[endpoint_id] = lock
        return lock

    @contextlib.asynccontextmanager
    async def keepalive_endpoint_locks(endpoint_ids: set[str]) -> AsyncIterator[None]:
        """Hold a stable endpoint set across reclaim and the ordinary claim."""

        locks = [keepalive_reconcile_lock(endpoint_id) for endpoint_id in sorted(endpoint_ids)]
        for lock in locks:
            await lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    async def collect_keepalive_endpoint(endpoint: Any) -> None:
        """Require a post-action, endpoint-scoped fresh collection."""

        try:
            collected = await shared_collector.collect_once(
                service,
                endpoints=[endpoint],
                concurrency=1,
            )
        except Exception:
            raise BrokerError(
                "keepalive_observation_failed",
                "endpoint keepalive state could not be observed",
                status_code=503,
            ) from None
        endpoint_result = collected.get(endpoint.id)
        if not isinstance(endpoint_result, dict) or "error" in endpoint_result:
            raise BrokerError(
                "keepalive_observation_failed",
                "endpoint keepalive state could not be observed",
                status_code=503,
            )

    def result_by_gpu_uuid(
        adapter_result: Any,
        gpu_uuids: list[str],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        """Read one result for every requested GPU."""

        results = getattr(adapter_result, "results", None)
        if not isinstance(results, tuple) or len(results) != len(gpu_uuids):
            raise BrokerError(
                "keepalive_adapter_failed",
                "endpoint keepalive operation could not be verified",
                status_code=503,
            )
        by_uuid: dict[str, Any] = {}
        expected_status = "running" if enabled else "stopped"
        for result in results:
            gpu_uuid = getattr(result, "gpu_uuid", None)
            status = getattr(result, "status", None)
            if not isinstance(gpu_uuid, str) or gpu_uuid in by_uuid or status != expected_status:
                raise BrokerError(
                    "keepalive_adapter_failed",
                    "endpoint keepalive operation could not be verified",
                    status_code=503,
                )
            by_uuid[gpu_uuid] = result
        if set(by_uuid) != set(gpu_uuids):
            raise BrokerError(
                "keepalive_adapter_failed",
                "endpoint keepalive operation could not be verified",
                status_code=503,
            )
        return by_uuid

    def attested_worker_identities_by_gpu_uuid(
        adapter_result: Any,
        gpu_uuids: list[str],
    ) -> dict[str, tuple[int, str]]:
        """Validate exact sealed-helper worker evidence for a GPU set.

        The adapter is the only caller of the remote helper.  This function
        deliberately keeps the REST/MCP surface out of that trust boundary:
        callers can neither provide a PID nor ask the helper to inspect an
        arbitrary process.  The service subsequently matches these narrow
        identities to its own fresh collector observation before it writes an
        expected process identity.
        """

        workers = getattr(adapter_result, "workers", None)
        if not isinstance(workers, tuple) or len(workers) != len(gpu_uuids):
            raise BrokerError(
                "keepalive_attestation_invalid",
                "endpoint keepalive worker evidence could not be verified",
                status_code=503,
            )
        by_uuid: dict[str, tuple[int, str]] = {}
        for worker in workers:
            gpu_uuid = getattr(worker, "gpu_uuid", None)
            pid = getattr(worker, "pid", None)
            driver_pid = getattr(worker, "driver_pid", None)
            boot_id = getattr(worker, "boot_id", None)
            start_time_ticks = getattr(worker, "start_time_ticks", None)
            worker_marker = getattr(worker, "worker_marker", None)
            if (
                not isinstance(gpu_uuid, str)
                or gpu_uuid in by_uuid
                or type(pid) is not int
                or pid <= 0
                or type(driver_pid) is not int
                or driver_pid <= 0
                or not isinstance(boot_id, str)
                or not boot_id
                or type(start_time_ticks) is not int
                or start_time_ticks <= 0
                or worker_marker != KEEPALIVE_WORKER_MARKER
            ):
                raise BrokerError(
                    "keepalive_attestation_invalid",
                    "endpoint keepalive worker evidence could not be verified",
                    status_code=503,
                )
            # ``pid`` is the helper's PID-namespace identity used for its
            # safe local signalling.  NVIDIA may report the host/driver PID
            # instead, so the collector match deliberately uses the helper's
            # separately attested driver-visible PID.
            by_uuid[gpu_uuid] = (driver_pid, boot_id)
        if set(by_uuid) != set(gpu_uuids):
            raise BrokerError(
                "keepalive_attestation_invalid",
                "endpoint keepalive worker evidence could not be verified",
                status_code=503,
            )
        return by_uuid

    async def reconcile_endpoint_keepalive(
        actor: ActorContext,
        endpoint_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Execute a service-produced per-GPU keepalive transition plan.

        Service planning and ownership writes are separated from adapter calls;
        every remote action is followed by a fresh endpoint observation before
        the corresponding lease is confirmed or released.  This callable is
        intentionally endpoint-level so the collector loop and the explicit
        API toggle use the same fail-closed orchestration path.
        """

        async with keepalive_reconcile_lock(endpoint_id):
            endpoint = service.collector_endpoint(endpoint_id)
            plan = service.list_keepalive_transitions(endpoint_id)
            transitions = plan.get("transitions")
            if not isinstance(transitions, list):
                raise BrokerError(
                    "keepalive_transition_plan_invalid",
                    "endpoint keepalive reconciliation plan is invalid",
                    status_code=503,
                )
            transitions = [
                transition
                for transition in transitions
                if isinstance(transition, dict) and transition.get("endpoint_id") == endpoint_id
            ]
            starts = [
                transition for transition in transitions if transition.get("action") == "start"
            ]
            stops = [transition for transition in transitions if transition.get("action") == "stop"]
            recovers = [
                transition for transition in transitions if transition.get("action") == "recover"
            ]
            if not starts and not stops and not recovers:
                return service.get_endpoint_keepalive_summary(endpoint_id)
            adapter_id = endpoint.keepalive_adapter_id
            if adapter_id is None:
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint keepalive is not configured",
                    status_code=409,
                )
            try:
                adapter = keepalive_adapter_resolver(adapter_id)
            except (KeyError, ValueError):
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint keepalive adapter is unavailable",
                    status_code=409,
                ) from None

            if starts:
                start_targets: list[tuple[str, str]] = []
                for transition in starts:
                    gpu_id = transition.get("gpu_id")
                    gpu_uuid = transition.get("gpu_uuid")
                    if not isinstance(gpu_id, str) or not isinstance(gpu_uuid, str):
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive start target is invalid",
                            status_code=503,
                        )
                    start_targets.append((gpu_id, gpu_uuid))
                gpu_uuids = [gpu_uuid for _gpu_id, gpu_uuid in start_targets]

                async def cleanup_failed_start(code: str, *, cleanup_remote: bool = True) -> None:
                    if cleanup_remote:
                        try:
                            cleanup = await adapter.set_enabled(endpoint, False, gpu_uuids)
                            result_by_gpu_uuid(cleanup, gpu_uuids, enabled=False)
                        except Exception:
                            code = "keepalive_cleanup_failed"
                    try:
                        await collect_keepalive_endpoint(endpoint)
                    except BrokerError:
                        if code != "keepalive_cleanup_failed":
                            code = "keepalive_observation_failed"
                    service.set_keepalive_error(
                        endpoint_id,
                        [gpu_id for gpu_id, _gpu_uuid in start_targets],
                        _public_error_message(
                            BrokerError(code, "keepalive start failed", status_code=503)
                        ),
                    )
                    raise BrokerError(
                        code,
                        "空闲占卡未能启动，将在下一次采集后重试。",
                        status_code=503,
                        details={"failed_gpu_ids": [gpu_id for gpu_id, _gpu_uuid in start_targets]},
                    )

                try:
                    adapter_result = await adapter.set_enabled(endpoint, True, gpu_uuids)
                    result_by_gpu_uuid(adapter_result, gpu_uuids, enabled=True)
                except AdapterCommandError as exc:
                    await cleanup_failed_start(
                        _keepalive_adapter_failure_code(exc), cleanup_remote=exc.uncertain
                    )
                except BrokerError as exc:
                    await cleanup_failed_start(exc.code)
                except Exception:
                    await cleanup_failed_start("keepalive_adapter_failed")

                try:
                    # The helper attests its own sealed v3 state first; the
                    # following normal collection must independently observe
                    # the same PID/boot identity before the service persists
                    # it.  A process-only collector observation can therefore
                    # never re-adopt a foreign workload.
                    observation_not_before = utcnow()
                    attested = await adapter.attest_workers(endpoint, gpu_uuids)
                    identities_by_uuid = attested_worker_identities_by_gpu_uuid(attested, gpu_uuids)
                    await collect_keepalive_endpoint(endpoint)
                    confirmed_worker_identities = {
                        gpu_id: identities_by_uuid[gpu_uuid] for gpu_id, gpu_uuid in start_targets
                    }
                    service.confirm_keepalive_workers(
                        actor,
                        endpoint_id,
                        [gpu_id for gpu_id, _gpu_uuid in start_targets],
                        confirmed_worker_identities=confirmed_worker_identities,
                        observation_not_before=observation_not_before,
                        idempotency_key=f"{idempotency_key}:activate-batch",
                    )
                except AdapterCommandError as exc:
                    await cleanup_failed_start(_keepalive_adapter_failure_code(exc))
                except BrokerError as exc:
                    await cleanup_failed_start(exc.code)
                except Exception:
                    await cleanup_failed_start("keepalive_activation_failed")
            if recovers:
                recover_targets: list[tuple[str, str]] = []
                for transition in recovers:
                    gpu_id = transition.get("gpu_id")
                    gpu_uuid = transition.get("gpu_uuid")
                    if not isinstance(gpu_id, str) or not isinstance(gpu_uuid, str):
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive recovery target is invalid",
                            status_code=503,
                        )
                    recover_targets.append((gpu_id, gpu_uuid))
                recover_gpu_uuids = [gpu_uuid for _gpu_id, gpu_uuid in recover_targets]
                try:
                    # Recovery has no mutation: it accepts an existing worker
                    # only when a sealed helper proof and a *new* normal
                    # observation agree.  A mismatch stays fail-closed.
                    observation_not_before = utcnow()
                    attested = await adapter.attest_workers(endpoint, recover_gpu_uuids)
                    identities_by_uuid = attested_worker_identities_by_gpu_uuid(
                        attested, recover_gpu_uuids
                    )
                    await collect_keepalive_endpoint(endpoint)
                    service.confirm_keepalive_workers(
                        actor,
                        endpoint_id,
                        [gpu_id for gpu_id, _gpu_uuid in recover_targets],
                        confirmed_worker_identities={
                            gpu_id: identities_by_uuid[gpu_uuid]
                            for gpu_id, gpu_uuid in recover_targets
                        },
                        observation_not_before=observation_not_before,
                        idempotency_key=f"{idempotency_key}:recover-batch",
                    )
                except AdapterCommandError as exc:
                    raise BrokerError(
                        _keepalive_adapter_failure_code(exc),
                        "空闲占卡身份校验失败，将在下一次采集后重试。",
                        status_code=503,
                    ) from None
                except BrokerError:
                    raise
                except Exception:
                    raise BrokerError(
                        "keepalive_attestation_invalid",
                        "空闲占卡身份校验失败，将在下一次采集后重试。",
                        status_code=503,
                    ) from None
            if stops:
                prepared_stops: list[tuple[dict[str, Any], str]] = []
                stop_failures: list[dict[str, str]] = []
                for transition in stops:
                    lease_id = transition.get("lease_id")
                    gpu_uuid = transition.get("gpu_uuid")
                    if not isinstance(lease_id, str) or not isinstance(gpu_uuid, str):
                        raise BrokerError(
                            "keepalive_transition_plan_invalid",
                            "endpoint keepalive stop target is invalid",
                            status_code=503,
                        )
                    # Resolve the lease through the service before remote I/O;
                    # a stale plan must never ask the helper to stop a GPU.
                    try:
                        pending = service.prepare_keepalive_stop(
                            actor,
                            endpoint_id,
                            transition.get("gpu_id"),
                        )
                        resolved_lease_id = pending.get("keepalive", {}).get("lease_id")
                        if resolved_lease_id != lease_id:
                            raise BrokerError(
                                "keepalive_transition_plan_invalid",
                                "endpoint keepalive stop reservation changed before execution",
                                status_code=409,
                            )
                    except BrokerError as exc:
                        stop_failures.append(
                            {"gpu_id": str(transition.get("gpu_id")), "code": exc.code}
                        )
                        continue
                    prepared_stops.append((transition, lease_id))
                for transition, lease_id in prepared_stops:
                    gpu_id = transition["gpu_id"]
                    gpu_uuid = transition["gpu_uuid"]
                    try:
                        # Stop is deliberately one GPU at a time.  The remote
                        # helper mutates each GPU before it reports a later
                        # failure, so a batch response cannot safely describe
                        # which local leases were actually released.
                        adapter_result = await adapter.set_enabled(endpoint, False, [gpu_uuid])
                        result_by_gpu_uuid(adapter_result, [gpu_uuid], enabled=False)
                    except AdapterCommandError as exc:
                        adapter_code = _keepalive_adapter_failure_code(exc)
                    except BrokerError as exc:
                        adapter_code = exc.code
                    except Exception:
                        adapter_code = "keepalive_adapter_failed"
                    else:
                        adapter_code = ""

                    # Even an adapter exception can mean that the helper
                    # stopped this GPU before failing on a later target.  A
                    # fresh observation is therefore mandatory on both the
                    # success and error paths; finalize only if it proves the
                    # target empty.
                    try:
                        observation_not_before = utcnow()
                        await collect_keepalive_endpoint(endpoint)
                        service.finalize_keepalive_stop(
                            actor,
                            endpoint_id,
                            lease_id,
                            observation_not_before=observation_not_before,
                            idempotency_key=f"{idempotency_key}:stop:{gpu_id}",
                        )
                    except BrokerError as exc:
                        stop_failures.append(
                            {
                                "gpu_id": gpu_id,
                                "code": exc.code if adapter_code == "" else adapter_code,
                            }
                        )

                if stop_failures:
                    deterministic_codes = {
                        item["code"]
                        for item in stop_failures
                        if item["code"] == "keepalive_helper_incompatible"
                    }
                    if len(deterministic_codes) == 1 and all(
                        item["code"] == "keepalive_helper_incompatible" for item in stop_failures
                    ):
                        code = next(iter(deterministic_codes))
                        raise BrokerError(
                            code,
                            "占卡 helper 版本或能力不匹配，请先完成该服务器的 helper 升级。",
                            status_code=503,
                            details={"failed_gpu_ids": [item["gpu_id"] for item in stop_failures]},
                        )
                    raise BrokerError(
                        "keepalive_partial_stop",
                        "已逐卡结束占卡；仍有 GPU 未能确认释放，请确认这些 GPU 上没有运行中的进程后再清理。",
                        status_code=409,
                        details={"failed_gpu_ids": [item["gpu_id"] for item in stop_failures]},
                    )
            return service.get_endpoint_keepalive_summary(endpoint_id)

    async def reclaim_keepalive_for_claim(
        actor: ActorContext,
        request_data_provider: Callable[[], RequestCreate],
        claim: Callable[[], dict[str, Any]],
        *,
        idempotency_key: str | None,
        locked_endpoint_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Make only a fully matching, verified keeper placement claimable.

        This is not generic preemption: the service's regular allocator first
        computes one complete placement made solely from ACTIVE per-GPU
        keepalive leases.  The API then stops exactly that physical set,
        verifies it empty, and runs the ordinary immediate claim. Any partial
        match, legacy worker, foreign process, or remote uncertainty leaves
        the claim blocked rather than broadening the set.
        """

        async def execute_locked() -> dict[str, Any] | None:
            request_data = request_data_provider()
            plan = service.plan_keepalive_reclaim(request_data)
            transitions = plan.get("transitions")
            if (
                plan.get("complete") is not True
                or not isinstance(transitions, list)
                or not transitions
            ):
                return None
            targets: list[dict[str, str]] = []
            for transition in transitions:
                if not isinstance(transition, dict) or transition.get("action") != "reclaim":
                    return None
                endpoint_id = transition.get("endpoint_id")
                gpu_id = transition.get("gpu_id")
                gpu_uuid = transition.get("gpu_uuid")
                lease_id = transition.get("lease_id")
                if not all(
                    isinstance(value, str) and value
                    for value in (endpoint_id, gpu_id, gpu_uuid, lease_id)
                ):
                    return None
                targets.append(
                    {
                        "endpoint_id": endpoint_id,
                        "gpu_id": gpu_id,
                        "gpu_uuid": gpu_uuid,
                        "lease_id": lease_id,
                    }
                )
            if len({target["gpu_id"] for target in targets}) != len(targets):
                return None
            by_endpoint: dict[str, list[dict[str, str]]] = defaultdict(list)
            for target in targets:
                by_endpoint[target["endpoint_id"]].append(target)
            for endpoint_id, endpoint_targets in by_endpoint.items():
                endpoint = service.collector_endpoint(endpoint_id)
                adapter_id = endpoint.keepalive_adapter_id
                if adapter_id is None:
                    return None
                try:
                    adapter = keepalive_adapter_resolver(adapter_id)
                except (KeyError, ValueError):
                    return None
                prepared: list[dict[str, str]] = []
                for target in endpoint_targets:
                    pending = service.prepare_keepalive_stop(
                        actor,
                        endpoint_id,
                        target["gpu_id"],
                    )
                    observed_lease_id = pending.get("keepalive", {}).get("lease_id")
                    if observed_lease_id != target["lease_id"]:
                        return None
                    prepared.append(target)
                gpu_uuids = [target["gpu_uuid"] for target in prepared]
                adapter_code: str | None = None
                # One helper call covers every target, so a failure cannot be
                # attributed to one card.  Report the whole targeted set rather
                # than naming an arbitrary member of it.
                targeted_gpu_ids = [target["gpu_id"] for target in prepared]
                try:
                    # One helper call can stop every target on this host. The
                    # remote helper may mutate then fail; observation after
                    # this call is what proves each GPU empty.
                    adapter_result = await adapter.set_enabled(endpoint, False, gpu_uuids)
                    result_by_gpu_uuid(adapter_result, gpu_uuids, enabled=False)
                except AdapterCommandError as exc:
                    adapter_code = _keepalive_adapter_failure_code(exc)
                except BrokerError as exc:
                    adapter_code = exc.code
                except Exception:
                    adapter_code = "keepalive_adapter_failed"
                # After the stop has been issued, before this collection.
                # finalize_keepalive_stop requires a snapshot newer than the stop.
                observation_not_before = utcnow()
                try:
                    await collect_keepalive_endpoint(endpoint)
                except BrokerError as exc:
                    raise BrokerError(
                        adapter_code or exc.code,
                        "占卡 GPU 未能确认释放，本次没有分配任务。",
                        status_code=503,
                        details={"failed_gpu_ids": targeted_gpu_ids},
                    ) from None
                for target in prepared:
                    try:
                        service.finalize_keepalive_stop(
                            actor,
                            endpoint_id,
                            target["lease_id"],
                            observation_not_before=observation_not_before,
                            idempotency_key=(
                                f"{idempotency_key}:reclaim:{target['gpu_id']}"
                                if idempotency_key is not None
                                else None
                            ),
                        )
                    except BrokerError as exc:
                        raise BrokerError(
                            adapter_code or exc.code,
                            "占卡 GPU 未能确认释放，本次没有分配任务。",
                            status_code=503,
                            details={"gpu_id": target["gpu_id"]},
                        ) from None
                if adapter_code is not None:
                    raise BrokerError(
                        adapter_code,
                        "占卡程序停止失败，本次没有分配任务。",
                        status_code=503,
                        details={"failed_gpu_ids": targeted_gpu_ids},
                    )
            # Keep the endpoint locks through the ordinary claim.
            # Otherwise collector reconciliation could observe the fresh
            # empty GPU and restart its keeper in the gap.
            return claim()

        if locked_endpoint_ids is not None:
            return await execute_locked()
        endpoint_ids = claim_candidate_endpoint_ids(
            request_data_provider(),
            service.collector_endpoints(),
        )
        async with keepalive_endpoint_locks(endpoint_ids):
            return await execute_locked()

    async def apply_plugin_for_claim(
        actor: ActorContext,
        request_data: RequestCreate,
        *,
        idempotency_key: str | None,
        persistent_lease: bool,
    ) -> dict[str, Any] | None:
        from serverpilot.plugins import (
            PluginError,
            apply_plugin,
            get_plugin,
            is_plugin_profile,
            release_plugin,
        )

        endpoint_ids = request_data.constraints.endpoint_ids
        group_ids = request_data.constraints.server_group_ids
        endpoint = None
        if len(endpoint_ids) == 1:
            try:
                endpoint = service.collector_endpoint(endpoint_ids[0])
            except BrokerError:
                return None
        elif not endpoint_ids and len(group_ids) == 1:
            matches = []
            for item in service.collector_endpoints():
                if item.server_group_id != group_ids[0]:
                    continue
                if not is_plugin_profile(item.observation_profile):
                    continue
                candidate = get_plugin(item.observation_profile)
                if candidate is not None and "apply" in candidate.capabilities:
                    matches.append(item)
            if len(matches) != 1:
                return None
            endpoint = matches[0]
        else:
            return None
        if endpoint is None or not is_plugin_profile(endpoint.observation_profile):
            return None
        plugin = get_plugin(endpoint.observation_profile)
        if plugin is None or "apply" not in plugin.capabilities:
            return None
        try:
            overlay = apply_plugin(
                plugin.plugin_id,
                gpu_count=request_data.constraints.gpu_count,
                task_ref=request_data.task_ref,
            )
        except PluginError as exc:
            if exc.no_capacity:
                return None
            raise BrokerError("plugin_apply_failed", str(exc), status_code=409) from exc
        try:
            await collect_keepalive_endpoint(endpoint)
        except BrokerError:
            with contextlib.suppress(PluginError):
                release_plugin(plugin.plugin_id, allocation_ref=str(overlay["allocation_ref"]))
            raise
        gpu_ids = _plugin_overlay_gpu_ids(endpoint.id, overlay)
        retry_data = request_data
        if gpu_ids and len(gpu_ids) == request_data.constraints.gpu_count:
            retry_data = request_data.model_copy(
                update={
                    "constraints": request_data.constraints.model_copy(
                        update={"gpu_ids": gpu_ids, "placement": "exact"}
                    )
                }
            )
        try:
            return service.create_request(
                actor,
                retry_data,
                idempotency_key=idempotency_key,
                activate_if_allocated=True,
                persistent_lease=persistent_lease,
                plugin_allocation=overlay,
            )
        except Exception:
            with contextlib.suppress(PluginError):
                release_plugin(plugin.plugin_id, allocation_ref=str(overlay["allocation_ref"]))
            raise

    async def claim_request_now(
        actor: ActorContext,
        request_data: RequestCreate,
        *,
        idempotency_key: str | None,
        persistent_lease: bool = False,
    ) -> dict[str, Any]:
        """Claim through the one shared per-GPU occupancy handoff."""

        endpoint_ids = claim_candidate_endpoint_ids(
            request_data,
            service.collector_endpoints(),
        )
        # A keeper start and an ordinary claim must not race between remote
        # start and ownership persistence. Keep this lock through a possible
        # exact keeper handoff and the ordinary claim. The race is per
        # endpoint, so only hosts this request could select are locked.
        async with keepalive_endpoint_locks(endpoint_ids):
            try:
                return service.create_request(
                    actor,
                    request_data,
                    idempotency_key=idempotency_key,
                    activate_if_allocated=True,
                    persistent_lease=persistent_lease,
                )
            except BrokerError as exc:
                if exc.code != "no_capacity":
                    raise
                claimed = await reclaim_keepalive_for_claim(
                    actor,
                    lambda: request_data,
                    lambda: service.create_request(
                        actor,
                        request_data,
                        idempotency_key=idempotency_key,
                        activate_if_allocated=True,
                        persistent_lease=persistent_lease,
                    ),
                    idempotency_key=idempotency_key,
                    locked_endpoint_ids=endpoint_ids,
                )
                if claimed is not None:
                    return claimed
                claimed = await apply_plugin_for_claim(
                    actor,
                    request_data,
                    idempotency_key=idempotency_key,
                    persistent_lease=persistent_lease,
                )
                if claimed is None:
                    raise exc
                return claimed

    # Reconcile tasks outlive the collection cycle that spawned them, so the
    # identity they run under is bound once instead of per cycle.
    collector_system_actor = ActorContext(
        id=SYSTEM_ACTOR_ID,
        role="admin",
        project_ids=frozenset(),
    )

    async def reconcile_collected(endpoint: Any, result: dict[str, Any]) -> None:
        # An explicit action or the previous collection cycle may still be
        # coordinating this endpoint. Do not queue a duplicate; the next
        # ordinary collection will see it.
        if keepalive_reconcile_lock(endpoint.id).locked():
            return
        revision = result.get("snapshot_revision")
        key_suffix = revision if isinstance(revision, int) else int(time.time())
        with contextlib.suppress(Exception):
            await reconcile_endpoint_keepalive(
                collector_system_actor,
                endpoint.id,
                idempotency_key=f"keepalive-reconcile:{endpoint.id}:{key_suffix}",
            )

    async def collector_loop() -> None:
        next_prune_at = 0.0
        while True:
            interval = service.collector_interval_seconds()
            cycle_started = time.monotonic()
            try:
                endpoints = service.collector_endpoints()
                collected = await shared_collector.collect_once(
                    service,
                    concurrency=5,
                    endpoints=endpoints,
                    stagger_seconds=0.0,
                )
                for endpoint in endpoints:
                    result = collected.get(endpoint.id)
                    if not isinstance(result, dict) or "error" in result:
                        continue
                    task = asyncio.create_task(
                        reconcile_collected(endpoint, result),
                        name=f"serverpilot-keepalive-{endpoint.id}",
                    )
                    keepalive_reconcile_tasks.add(task)
                    task.add_done_callback(keepalive_reconcile_tasks.discard)
            except Exception:
                # Per-endpoint failures are already recorded by SSHCollector. This
                # protects the service loop from an unexpected local failure.
                pass
            if time.monotonic() >= next_prune_at:
                with contextlib.suppress(Exception):
                    service.prune_telemetry_history()
                next_prune_at = time.monotonic() + 3600
            elapsed = time.monotonic() - cycle_started
            await asyncio.sleep(max(0.25, interval - elapsed))

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI):
        task = None
        if inventory.collector.enabled:
            task = asyncio.create_task(collector_loop(), name="serverpilot-collector")
        application.state.collector_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            reconcile_tasks = tuple(keepalive_reconcile_tasks)
            if reconcile_tasks:
                await asyncio.gather(*reconcile_tasks, return_exceptions=True)

    app = FastAPI(title="ServerPilot", version=__version__, lifespan=lifespan)
    app.state.service = service
    app.state.settings = settings
    # A narrow integration hook for the collector/recovery path.  It accepts
    # an endpoint only; callers cannot inject a remote target or worker
    # identity, and all execution stays behind the service transition plan.
    app.state.reconcile_endpoint_keepalive = reconcile_endpoint_keepalive
    limiter = RateLimiter(settings.rate_limit_per_minute)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.request_body_limit_bytes,
    )

    @app.exception_handler(BrokerError)
    async def broker_error_handler(_request: Request, exc: BrokerError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": exc.code,
                    "message": _public_error_message(exc),
                    "details": exc.details,
                },
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": "validation_error",
                    "message": "请求内容不完整或格式不正确。",
                    "details": jsonable_encoder(exc.errors()),
                },
            },
        )

    def api_actor(request: Request) -> ActorContext:
        actor = service.local_actor(request.headers.get("x-serverpilot-actor", "agent"))
        limiter.check(actor.id)
        return actor

    ApiActor = Annotated[ActorContext, Depends(api_actor)]

    # ---- health and REST read routes ------------------------------------------

    @app.get("/health/live")
    def health_live() -> dict[str, Any]:
        return {
            "status": "live",
            "schema_version": SCHEMA_VERSION,
            "version": __version__,
            "capabilities": list(API_CAPABILITIES),
        }

    @app.get("/health/ready")
    def health_ready() -> JSONResponse:
        ready = service.database.ready()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "database_ready": ready,
                "inventory_readable": settings.inventory_path.exists(),
                "single_writer": True,
                "daemon_instance_id": settings.daemon_instance_id,
                "process_id": os.getpid(),
            },
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return service.metrics()

    @app.get("/api/v1/snapshot")
    def snapshot(
        actor: ApiActor,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        return service.snapshot(
            actor,
            compact=compact,
            endpoint_id=endpoint_id,
            state=state,
            only_available=only_available,
        )

    @app.get("/api/v1/state", response_model=ControlPlaneSnapshot)
    def control_plane_state(
        actor: ApiActor,
    ) -> dict[str, Any]:
        return service.control_plane_state(actor)

    @app.get("/api/v1/observation-profiles")
    def observation_profiles(actor: ApiActor) -> dict[str, Any]:
        from serverpilot.plugins import list_observation_profiles

        _ = actor
        return {"data": list_observation_profiles()}

    @app.get("/api/v1/mcp-entry")
    def mcp_entry(actor: ApiActor) -> dict[str, Any]:
        _ = actor
        return {"data": mcp_entry_status()}

    @app.get("/api/v1/endpoints")
    def endpoints(actor: ApiActor) -> dict[str, Any]:
        return service.list_endpoints(actor)

    @app.get("/api/v1/endpoints/{endpoint_id}/history")
    def endpoint_history(
        endpoint_id: str,
        actor: ApiActor,
        window_seconds: int = 3600,
        points: int = 120,
    ) -> dict[str, Any]:
        return service.endpoint_history(
            actor,
            endpoint_id,
            window_seconds=window_seconds,
            max_points=points,
        )

    @app.get("/api/v1/gpus")
    def gpus(
        actor: ApiActor,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        return service.list_gpus(
            actor,
            state=state,
            endpoint_id=endpoint_id,
            only_available=only_available,
            compact=compact,
        )

    @app.get("/api/v1/requests")
    def requests(actor: ApiActor) -> dict[str, Any]:
        return service.list_requests(actor)

    @app.get("/api/v1/leases")
    def leases(actor: ApiActor) -> dict[str, Any]:
        return service.list_leases(actor)

    @app.get("/api/v1/reservations")
    def reservations(actor: ApiActor) -> dict[str, Any]:
        return service.list_reservations(actor)

    @app.get("/api/v1/events")
    def events(actor: ApiActor, after_id: int = 0, limit: int = 200) -> dict[str, Any]:
        return service.list_events(actor, after_id=after_id, limit=limit)

    @app.get("/api/v1/settings/collector")
    def collector_settings(actor: ApiActor) -> dict[str, Any]:
        return service.collector_settings(actor)

    @app.get("/api/v1/doctor")
    def doctor(actor: ApiActor) -> dict[str, Any]:
        return service.doctor(actor)

    # ---- REST mutation routes --------------------------------------------------

    @app.patch("/api/v1/settings/collector")
    def update_collector_settings(
        settings_data: CollectorSettingsUpdate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.update_collector_settings(
            actor,
            settings_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/requests")
    def create_request(
        request_data: RequestCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_request(
            actor,
            request_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/requests/{request_id}/cancel")
    def cancel_request(
        request_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.cancel_request(
            actor,
            request_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/claims")
    async def claim_now(
        request_data: RequestCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Create an immediate claim, reclaiming only its selected keepers once."""

        return await claim_request_now(
            actor,
            request_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/routine/claims")
    async def routine_claim_now(
        request_data: RequestCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Create a persistent routine GPU claim through the shared handoff."""

        if not request_data.constraints.same_host:
            payload = request_data.model_dump(mode="json")
            payload["constraints"]["same_host"] = True
            try:
                request_data = RequestCreate.model_validate(payload)
            except ValidationError as exc:
                raise RequestValidationError(exc.errors()) from exc
        return await claim_request_now(
            actor,
            request_data,
            idempotency_key=idempotency_key,
            persistent_lease=True,
        )

    @app.post("/api/v1/leases/{lease_id}/activate")
    def activate_lease(
        lease_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.activate_lease(
            actor, lease_id, idempotency_key=_idempotency_key(idempotency_key)
        )

    @app.post("/api/v1/leases/{lease_id}/renew")
    def renew_lease(
        lease_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.renew_lease(
            actor, lease_id, idempotency_key=_idempotency_key(idempotency_key)
        )

    @app.post("/api/v1/leases/{lease_id}/release")
    def release_lease(
        lease_id: str,
        body: dict[str, str],
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.release_lease(
            actor,
            lease_id,
            reason=body.get("reason", ""),
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/routine/leases/{lease_id}/release")
    def routine_release_lease(
        lease_id: str,
        actor: ApiActor,
    ) -> dict[str, Any]:
        return service.release_lease(
            actor,
            lease_id,
            reason="workload_completed",
            idempotency_key=None,
        )

    @app.post("/api/v1/operator/leases/{lease_id}/release")
    def operator_release_lease(
        request: Request,
        lease_id: str,
        body: dict[str, str],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Human correction surface for the loopback App only."""

        if request.headers.get("x-serverpilot-client") != "desktop-app":
            raise BrokerError(
                "operator_client_required",
                "human lease correction is only available to the local App",
                status_code=403,
            )
        label = request.headers.get("x-serverpilot-actor", "human").strip() or "human"
        local = service.local_actor(label)
        actor = ActorContext(id=local.id, role="operator", project_ids=local.project_ids)
        return service.release_lease(
            actor,
            lease_id,
            reason=body.get("reason", ""),
            idempotency_key=_idempotency_key(idempotency_key),
            operator_override=True,
        )

    async def apply_lease_gpu_reassignment(
        actor: ActorContext,
        lease_id: str,
        assignment: LeaseGPUAssignment,
        *,
        mutation_key: str,
        operator_override: bool,
    ) -> dict[str, Any]:
        endpoint_ids = {endpoint.id for endpoint in service.collector_endpoints()}
        async with keepalive_endpoint_locks(endpoint_ids):
            try:
                return service.reassign_lease_gpus(
                    actor,
                    lease_id,
                    assignment.gpu_ids,
                    idempotency_key=mutation_key,
                    operator_override=operator_override,
                )
            except BrokerError as exc:
                if exc.code != "gpu_already_assigned":
                    raise
                reclaim_request = service.keepalive_reclaim_request_for_reassignment(
                    actor,
                    lease_id,
                    assignment.gpu_ids,
                    operator_override=operator_override,
                )
                if reclaim_request is None:
                    raise exc
                reassigned = await reclaim_keepalive_for_claim(
                    actor,
                    lambda: reclaim_request,
                    lambda: service.reassign_lease_gpus(
                        actor,
                        lease_id,
                        assignment.gpu_ids,
                        idempotency_key=mutation_key,
                        operator_override=operator_override,
                    ),
                    idempotency_key=mutation_key,
                    locked_endpoint_ids=endpoint_ids,
                )
                if reassigned is None:
                    raise exc
                return reassigned

    @app.patch("/api/v1/operator/leases/{lease_id}/gpus")
    async def operator_reassign_lease_gpus(
        request: Request,
        lease_id: str,
        assignment: LeaseGPUAssignment,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Human GPU reassignment correction surface for the loopback App."""

        if request.headers.get("x-serverpilot-client") != "desktop-app":
            raise BrokerError(
                "operator_client_required",
                "human lease correction is only available to the local App",
                status_code=403,
            )
        label = request.headers.get("x-serverpilot-actor", "human").strip() or "human"
        local = service.local_actor(label)
        actor = ActorContext(id=local.id, role="operator", project_ids=local.project_ids)
        return await apply_lease_gpu_reassignment(
            actor,
            lease_id,
            assignment,
            mutation_key=_idempotency_key(idempotency_key),
            operator_override=True,
        )

    @app.post("/api/v1/endpoints/{endpoint_id}/leases/{lease_id}/release-empty")
    @app.post("/api/v1/endpoints/{endpoint_id}/conflicted-leases/{lease_id}/release-empty")
    async def release_empty_conflicted_lease(
        endpoint_id: str,
        lease_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Clear an empty workload/keepalive lease only after fresh collection."""

        mutation_key = _idempotency_key(idempotency_key)
        observation_not_before = utcnow()
        endpoint = service.collector_endpoint(endpoint_id)
        collected = await shared_collector.collect_once(
            service,
            endpoints=[endpoint],
            concurrency=1,
        )
        result = collected.get(endpoint_id)
        if not isinstance(result, dict) or "error" in result:
            raise BrokerError(
                "conflict_observation_failed",
                "服务器采集失败，暂不释放空闲占用",
                status_code=503,
            )
        return service.release_empty_conflicted_lease(
            actor,
            endpoint_id,
            lease_id,
            observation_not_before=observation_not_before,
            idempotency_key=mutation_key,
        )

    @app.post("/api/v1/leases/{lease_id}/bind-workload")
    def bind_workload(
        lease_id: str,
        binding: LeaseBind,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.bind_workload(
            actor,
            lease_id,
            binding,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/leases/{lease_id}/bind-observed-workload")
    def bind_observed_workload(
        lease_id: str,
        binding: LeaseObservedBind,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.bind_observed_workload(
            actor,
            lease_id,
            binding,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/endpoints")
    def create_endpoint(
        endpoint_data: EndpointCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_endpoint(
            actor,
            endpoint_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/server-groups")
    def create_server_group(
        group_data: ServerGroupCreate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.create_server_group(
            actor,
            group_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.patch("/api/v1/server-groups/{group_id}")
    def update_server_group(
        group_id: str,
        group_data: ServerGroupUpdate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.update_server_group(
            actor,
            group_id,
            group_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.delete("/api/v1/server-groups/{group_id}")
    def delete_server_group(
        group_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.delete_server_group(
            actor,
            group_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.patch("/api/v1/endpoints/{endpoint_id}")
    def update_endpoint(
        endpoint_id: str,
        endpoint_data: EndpointUpdate,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.update_endpoint(
            actor,
            endpoint_id,
            endpoint_data,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.delete("/api/v1/endpoints/{endpoint_id}")
    def delete_endpoint(
        endpoint_id: str,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return service.delete_endpoint(
            actor,
            endpoint_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )

    @app.post("/api/v1/endpoints/{endpoint_id}/keepalive")
    async def set_endpoint_keepalive(
        endpoint_id: str,
        state: EndpointKeepaliveRequest,
        actor: ApiActor,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        """Set endpoint desired policy, then reconcile independent GPU keepers.

        ``enabled`` is deliberately the only caller input.  It maps to the
        endpoint desired policy and never permits a client to name a worker,
        a GPU UUID, a PID, or arbitrary remote parameters.
        """

        mutation_key = _idempotency_key(idempotency_key)
        policy = "idle_keepalive" if state.enabled else "disabled"
        configured = service.configure_keepalive_policy(
            actor,
            endpoint_id,
            policy,
            idempotency_key=mutation_key,
        )
        reconciled = await reconcile_endpoint_keepalive(
            actor,
            endpoint_id,
            idempotency_key=mutation_key,
        )
        keepalive = reconciled.get("keepalive")
        if not isinstance(keepalive, dict):
            raise BrokerError(
                "keepalive_endpoint_observation_missing",
                "endpoint keepalive state could not be projected after reconciliation",
                status_code=503,
            )
        event_id = configured.get("event_id")
        revision = reconciled.get("snapshot_revision")
        return _public_keepalive_result(
            endpoint_id,
            keepalive,
            event_id=event_id if isinstance(event_id, int) else None,
            snapshot_revision=revision if isinstance(revision, int) else None,
        )

    return app


def _find_project_root() -> Path:
    """Find the source release root, falling back to packaged migrations."""

    configured = os.environ.get("SERVERPILOT_PROJECT_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend([Path.cwd(), *Path.cwd().parents])
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (
            candidate / "src" / "serverpilot" / "migrations"
        ).is_dir():
            return candidate
    return Path(__file__).resolve().parent
