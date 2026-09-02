from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from serverpilot import mcp_server
from serverpilot.models import Lease, LeaseResource, ProcessObservation
from serverpilot.schemas import EndpointObservation, ProcessInput, TelemetryInput


def observation(
    endpoint_id: str = "endpoint-a",
    *,
    count: int = 4,
    processes: list[ProcessInput] | None = None,
    prefix: str = "GPU",
    gpu_uuids: list[str] | None = None,
    observation_complete: bool = True,
    observed_at: datetime | None = None,
    host: dict[str, object] | None = None,
) -> EndpointObservation:
    now = observed_at or datetime.now(UTC)
    uuids = (
        gpu_uuids
        if gpu_uuids is not None
        else [f"{prefix}-{endpoint_id}-{index}" for index in range(count)]
    )
    host_payload = {
        "cpu_count": 64,
        "load_1m": 4.0,
        "memory_total_mib": 262_144,
        "memory_available_mib": 196_608,
    }
    if host is not None:
        host_payload.update(host)
    return EndpointObservation(
        endpoint_id=endpoint_id,
        observed_at=now,
        boot_id=f"boot-{endpoint_id}",
        host=host_payload,
        gpus=[
            TelemetryInput(
                gpu_uuid=gpu_uuid,
                gpu_index=index,
                cuda_ordinal=index,
                name="Test GPU",
                total_vram_mib=100_000,
                memory_used_mib=0,
                memory_free_mib=100_000,
                gpu_utilization_pct=0,
                memory_utilization_pct=0,
                temperature_c=35,
                power_watts=100.0,
                pstate="P0",
                health="OK",
            )
            for index, gpu_uuid in enumerate(uuids)
        ],
        processes=processes or [],
        observation_complete=observation_complete,
    )


def process_for_gpu(uuid: str, *, pid: int = 1234) -> ProcessInput:
    return ProcessInput(
        gpu_uuid=uuid,
        pid=pid,
        used_memory_mib=1024,
        executable="/usr/bin/python",
        username="tester",
        process_started_at=datetime.now(UTC),
    )


def age_out_processes(service: Any) -> None:
    """Put every stored process sighting one observation short of retirement.

    A process stays a current fact until the endpoint's own unbroken complete
    observations have left it out for ``process_absence_grace_seconds``, so
    simply not reporting it once is, by design, not enough to retire it. A test
    that means "this worker has gone" ages the absence clock and then reports
    the complete observation that leaves it out, which is the reading that
    retires it.
    """

    stale = datetime.now(UTC) - timedelta(
        seconds=service.collector.process_absence_grace_seconds + 60
    )

    def write(session: Any) -> None:
        for observed in session.scalars(select(ProcessObservation)).all():
            observed.last_seen_at = stale
            observed.absent_since = stale

    service._write(write)


def age_out_lease_holder(service: Any, lease_id: str) -> None:
    """Push a lease's holder past the manual-release liveness window.

    ``release_empty_conflicted_lease`` demands the same liveness age the
    automatic idle reclaim uses, so a lease claimed a second ago is protected
    on purpose -- that protection is the whole point. A test that means to
    exercise the recovery path has to be given a lease whose holder really has
    gone quiet.
    """

    stale = datetime.now(UTC) - timedelta(seconds=service.inventory.idle_lease_alert_seconds + 60)

    def write(session: Any) -> None:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.issued_at = stale
        lease.last_heartbeat_at = stale
        # A holder is heard from two ways -- its own check-in and a sighting of
        # its work -- so going quiet means both go quiet. Whether the card is
        # then free to hand out is a different question, answered by the
        # absence window and its own helper.
        for observed in session.scalars(select(ProcessObservation)).all():
            observed.last_seen_at = stale
            observed.process_started_at = stale

    service._write(write)


def age_out_workload_release(service: Any, lease_id: str) -> None:
    """Age a released workload lease so keepalive may start on its GPUs again."""

    stale = datetime.now(UTC) - timedelta(
        seconds=service.inventory.keepalive_start_cooldown_seconds + 60
    )

    def write(session: Any) -> None:
        for resource in session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease_id)
        ).all():
            resource.released_at = stale

    service._write(write)


class _SyncTools:
    """Call the async MCP tools from a synchronous test.

    The tools are coroutine functions. Wrapping them here keeps that fact
    out of the shipped module, where a name whose return type depended on
    whether a loop happened to be running was impossible to reason about.
    """

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(mcp_server, name)
        function = inspect.unwrap(attribute)
        if inspect.iscoroutinefunction(function):

            def call(*args: Any, **kwargs: Any) -> Any:
                return asyncio.run(function(*args, **kwargs))

            return call
        return attribute


tools = _SyncTools()


def keepalive_start_candidates(service, endpoint_id: str) -> list[str]:
    """GPU ids the service would start occupancy on right now.

    Reads the live transition plan and keeps the starts. The service used to
    expose this filter as a method of its own, but nothing in production called
    it -- only tests -- so the filter moved here and the assertions go on
    reading the same live rule through `list_keepalive_transitions`.
    """

    plan = service.list_keepalive_transitions(endpoint_id)
    return [item["gpu_id"] for item in plan["transitions"] if item["action"] == "start"]
