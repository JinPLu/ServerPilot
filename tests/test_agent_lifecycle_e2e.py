"""End-to-end agent lifecycle over the real REST surface the MCP tools call.

These exercise the three routine tools against a live ASGI app and a real
database rather than a fake client, so a break in the wiring between the MCP
projection, the REST route and the domain service fails here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import GPUDevice, Lease, LeaseResource
from serverpilot.service import ActorContext
from serverpilot.timeutil import utcnow
from tests.helpers import observation, process_for_gpu, tools


@pytest.fixture()
def routine(mcp_agent):  # type: ignore[no-untyped-def]
    return mcp_agent


ACTOR = ActorContext("claude-code", "operator", frozenset(), "session-under-test")


def _active_gpu_ids(service, lease_id: str) -> set[str]:  # type: ignore[no-untyped-def]
    def read(session):  # type: ignore[no-untyped-def]
        return {
            resource.gpu_id
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
                )
            ).all()
        }

    return service._read(read)


def _age_idle(service, lease_id: str, seconds: int, *, gpu_ids=None) -> None:  # type: ignore[no-untyped-def]
    """Age the per-GPU idle streak and the holder's last sign of life.

    Settling a whole claim needs both: its cards ran nothing, and its holder
    stopped asking about its own hold. Ageing only the streak would describe a
    claim whose agent is still there.
    """

    def write(session):  # type: ignore[no-untyped-def]
        stamp = utcnow() - timedelta(seconds=seconds)
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.last_heartbeat_at = stamp
        for resource in session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
            )
        ).all():
            if gpu_ids is None or resource.gpu_id in gpu_ids:
                resource.idle_since = stamp

    service._write(write)


def _free(status) -> int:  # type: ignore[no-untyped-def]
    return sum(group["free"] for group in status["groups"])


def test_apply_use_release_round_trip(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))

    assert _free(tools.gpu_status()) == 2
    claimed = tools.gpu_apply(task="端到端训练", duration_seconds=3600, gpu_count=2)
    env = claimed["servers"][0]["env"]
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert env["CUDA_VISIBLE_DEVICES"] == "0,1"

    after = tools.gpu_status()
    assert _free(after) == 0
    assert [item["task"] for item in after["mine"]] == ["端到端训练"]
    assert tools.gpu_release(lease_id=claimed["lease_id"])["ended"] is True
    assert _free(tools.gpu_status()) == 2


def test_second_agent_sees_no_capacity_then_reclaims_the_idle_card(routine) -> None:  # type: ignore[no-untyped-def]
    """The whole point of reclaim: a forgotten claim stops blocking others."""

    routine.ingest_observation(observation(count=1))
    claimed = tools.gpu_apply(task="忘记释放的任务", duration_seconds=3600)
    assert tools.gpu_apply(task="被挡住的任务", duration_seconds=3600)["error"]["code"] == "no_capacity"

    _age_idle(routine, claimed["lease_id"], routine.inventory.idle_lease_reclaim_seconds + 5)
    routine.ingest_observation(observation(count=1))

    assert _free(tools.gpu_status()) == 1
    second = tools.gpu_apply(task="被挡住的任务", duration_seconds=3600)
    assert second["lease_id"] != claimed["lease_id"]


def test_partly_used_claim_keeps_its_working_card_and_returns_the_rest(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    claimed = tools.gpu_apply(task="只用一张卡", duration_seconds=3600, gpu_count=2)
    lease_id = claimed["lease_id"]
    busy_uuid = "GPU-endpoint-a-0"
    busy_id, idle_id = f"endpoint-a:{busy_uuid}", "endpoint-a:GPU-endpoint-a-1"

    routine.ingest_observation(observation(count=2, processes=[process_for_gpu(busy_uuid)]))
    _age_idle(routine, lease_id, routine.inventory.idle_lease_reclaim_seconds + 5, gpu_ids={idle_id})
    routine.ingest_observation(observation(count=2, processes=[process_for_gpu(busy_uuid)]))

    assert _active_gpu_ids(routine, lease_id) == {busy_id}
    assert _free(tools.gpu_status()) == 1
    # The returned card is immediately usable by another claim.
    other = tools.gpu_apply(task="接手空闲卡", duration_seconds=3600)
    assert other["servers"][0]["env"]["CUDA_VISIBLE_DEVICES"] == "1"

    def lease_state(session):  # type: ignore[no-untyped-def]
        return session.get(Lease, lease_id).state

    assert routine._read(lease_state) not in {"EXPIRED_EMPTY", "RELEASED"}


def test_releasing_one_of_two_leases_does_not_touch_the_other(routine) -> None:  # type: ignore[no-untyped-def]
    """The contract asks agents to confirm each lease; the broker must honour it."""

    routine.ingest_observation(observation(count=2))
    first = tools.gpu_apply(task="任务甲", duration_seconds=3600)
    assert _free(tools.gpu_status()) == 1
    second = tools.gpu_apply(task="任务乙", duration_seconds=3600)
    assert first["lease_id"] != second["lease_id"]

    assert tools.gpu_release(lease_id=first["lease_id"])["ended"] is True
    status = tools.gpu_status()
    assert _free(status) == 1
    assert [item["task"] for item in status["mine"]] == ["任务乙"]
    assert tools.gpu_release(lease_id=second["lease_id"])["ended"] is True
    assert _free(tools.gpu_status()) == 2


def test_status_narrows_to_one_group(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    assert tools.gpu_status(server_group_id="no-such-group")["groups"] == []


def test_asking_about_your_own_lease_keeps_it_from_being_reclaimed(routine) -> None:  # type: ignore[no-untyped-def]
    """A staged job's quiet phase looks exactly like an abandoned claim.

    What separates them is that the holder is still asking about its own hold,
    and this proves that answer travels the whole way: MCP tool, REST route,
    domain service, reclaim.
    """

    routine.ingest_observation(observation(count=2))
    lease_id = tools.gpu_apply(task="分阶段的任务", duration_seconds=3600)["lease_id"]

    _age_idle(routine, lease_id, routine.inventory.idle_lease_reclaim_seconds + 5)
    tools.gpu_lease(lease_id=lease_id)
    routine.ingest_observation(observation(count=2))
    assert len(_active_gpu_ids(routine, lease_id)) == 1

    # The same sequence without asking settles the claim.
    _age_idle(routine, lease_id, routine.inventory.idle_lease_reclaim_seconds + 5)
    routine.ingest_observation(observation(count=2))
    assert _active_gpu_ids(routine, lease_id) == set()
    assert _free(tools.gpu_status()) == 2


def _hold_cards_on_second_server(service, lease_id: str) -> str:  # type: ignore[no-untyped-def]
    """Put endpoint-b's first card on the lease, as a resize would."""

    service.ingest_observation(observation("endpoint-b", count=1))

    def write(session):  # type: ignore[no-untyped-def]
        gpu = session.scalar(select(GPUDevice).where(GPUDevice.endpoint_id == "endpoint-b"))
        session.add(LeaseResource(lease_id=lease_id, gpu_id=gpu.id, active=True))
        return gpu.id

    return service._write(write)


def _running_on_first_card(service, lease_id: str, count: int = 1):  # type: ignore[no-untyped-def]
    first = min(_active_gpu_ids(service, lease_id) - _gpus_on(service, "endpoint-b"))

    def read(session):  # type: ignore[no-untyped-def]
        return session.get(GPUDevice, first)

    gpu = service._read(read)
    return observation(count=count, processes=[process_for_gpu(gpu.gpu_uuid)])


def _gpus_on(service, endpoint_id: str) -> set[str]:  # type: ignore[no-untyped-def]
    return service._read(lambda session: set(session.scalars(
        select(GPUDevice.id).where(GPUDevice.endpoint_id == endpoint_id)
    ).all()))


def _card(service, lease_id: str, gpu_id: str):  # type: ignore[no-untyped-def]
    return service._read(lambda session: session.scalar(select(LeaseResource).where(
        LeaseResource.lease_id == lease_id, LeaseResource.gpu_id == gpu_id
    )))


def test_a_process_on_one_server_does_not_cost_another_its_cards(routine) -> None:  # type: ignore[no-untyped-def]
    """A second server still being prepared is held by its holder, not its sibling."""

    routine.ingest_observation(observation(count=1))
    lease_id = tools.gpu_apply(task="两台机器", duration_seconds=3600)["lease_id"]
    second = _hold_cards_on_second_server(routine, lease_id)
    routine.ingest_observation(observation("endpoint-b", count=1))
    running = _running_on_first_card(routine, lease_id)
    routine.ingest_observation(running)

    reclaim_after = routine.inventory.idle_lease_reclaim_seconds
    _age_idle(routine, lease_id, reclaim_after + 5, gpu_ids={second})
    routine.record_lease_heartbeat(ACTOR, lease_id)
    routine.ingest_observation(_running_on_first_card(routine, lease_id))
    assert second in _active_gpu_ids(routine, lease_id)

    # Heard from no longer, and the working sibling is no evidence for it.
    _age_idle(routine, lease_id, reclaim_after + 5, gpu_ids={second})
    routine.ingest_observation(_running_on_first_card(routine, lease_id))
    assert second not in _active_gpu_ids(routine, lease_id)
    card = _card(routine, lease_id, second)
    assert card.release_reason == "idle without observed process"
    assert card.released_by
    lease = routine._read(lambda session: session.get(Lease, lease_id))
    assert lease.resource_epoch == 1
    assert lease.state in {"HELD", "ACTIVE"}


def test_keep_idle_postpones_reclaim_for_three_windows_and_no_more(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    lease_id = tools.gpu_apply(task="留空卡", duration_seconds=3600, gpu_count=2)["lease_id"]
    routine.ingest_observation(_running_on_first_card(routine, lease_id, count=2))
    idle = max(_active_gpu_ids(routine, lease_id))
    actor = ACTOR
    reclaim_after = routine.inventory.idle_lease_reclaim_seconds

    def age_card(seconds: int) -> None:
        def write(session):  # type: ignore[no-untyped-def]
            session.scalar(select(LeaseResource).where(
                LeaseResource.lease_id == lease_id, LeaseResource.gpu_id == idle
            )).idle_since = utcnow() - timedelta(seconds=seconds)
        routine._write(write)

    age_card(reclaim_after - 5)
    kept = routine.record_lease_heartbeat(actor, lease_id, keep_idle=True)["kept_idle"]
    assert [item["windows_left"] for item in kept if idle.endswith(item["gpu_id"])] == [1]
    age_card(reclaim_after + 5)
    routine.ingest_observation(_running_on_first_card(routine, lease_id, count=2))
    assert idle in _active_gpu_ids(routine, lease_id)

    routine.record_lease_heartbeat(actor, lease_id, keep_idle=True)
    age_card(reclaim_after * 3 - 5)
    routine.ingest_observation(_running_on_first_card(routine, lease_id, count=2))
    assert idle in _active_gpu_ids(routine, lease_id)

    # The limit holds however often the holder asks.
    routine.record_lease_heartbeat(actor, lease_id, keep_idle=True)
    age_card(reclaim_after * 3 + 5)
    routine.ingest_observation(_running_on_first_card(routine, lease_id, count=2))
    assert idle not in _active_gpu_ids(routine, lease_id)
    assert len(_active_gpu_ids(routine, lease_id)) == 1


def test_a_reclaim_is_told_once_and_stands_until_read(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=1))
    lease_id = tools.gpu_apply(task="被回收的任务", duration_seconds=3600)["lease_id"]
    _age_idle(routine, lease_id, routine.inventory.idle_lease_reclaim_seconds + 5)
    routine.ingest_observation(observation(count=1))

    pending = [item for item in routine.pending_notifications() if item["type"] == "lease_reclaimed"]
    assert [item["lease_id"] for item in pending] == [lease_id]
    assert "被回收的任务" in pending[0]["message"]

    routine.mark_reminders(ACTOR, [pending[0]["id"]], notified=True)
    assert not [item for item in routine.pending_notifications() if item["type"] == "lease_reclaimed"]
    assert any(item["id"] == pending[0]["id"] and item["state"] == "active" for item in routine.list_reminders())
