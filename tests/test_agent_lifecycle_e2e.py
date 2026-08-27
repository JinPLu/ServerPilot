"""End-to-end agent lifecycle over the real REST surface the MCP tools call.

These exercise the three routine tools against a live ASGI app and a real
database rather than a fake client, so a break in the wiring between the MCP
projection, the REST route and the domain service fails here.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot import mcp_server
from serverpilot.client import BrokerClientError
from serverpilot.models import Lease, LeaseResource
from serverpilot.timeutil import utcnow
from tests.helpers import observation, process_for_gpu, tools


@pytest.fixture()
def routine(build_app, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A live broker wired to the routine MCP tools through HTTP."""

    app = build_app("e2e")
    rest = TestClient(app)
    headers = {"X-ServerPilot-Actor": "agent"}

    class RoutineClient:
        def snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
            params = {key: value for key, value in kwargs.items() if value is not None}
            response = rest.get("/api/v1/snapshot", params=params, headers=headers)
            assert response.status_code == 200, response.text
            return response.json()

        def post(self, path, body=None, *, idempotency_key=None):  # type: ignore[no-untyped-def]
            request_headers = dict(headers)
            if idempotency_key is not None:
                request_headers["Idempotency-Key"] = idempotency_key
            response = rest.post(path, json=body, headers=request_headers)
            if response.is_error:
                error = response.json()["error"]
                raise BrokerClientError(
                    f"broker HTTP {response.status_code}: {error['code']}: {error['message']}"
                )
            return response.json()

    monkeypatch.setattr(mcp_server, "_routine_client", lambda: RoutineClient())
    return app.state.service


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
    def write(session):  # type: ignore[no-untyped-def]
        stamp = utcnow() - timedelta(seconds=seconds)
        for resource in session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
            )
        ).all():
            if gpu_ids is None or resource.gpu_id in gpu_ids:
                resource.idle_since = stamp

    service._write(write)


def _capacity_servers(status):  # type: ignore[no-untyped-def]
    """Collect per-server SKU capacity from grouped or ungrouped routine status."""

    servers = [
        server
        for group in status.get("server_groups") or []
        for server in group.get("servers") or []
    ]
    servers.extend(status.get("ungrouped_servers") or [])
    return servers


def _assert_sku_capacity(  # type: ignore[no-untyped-def]
    status, *, available_count: int, total_count: int, server_id: str | None = None
):
    """Allocatable capacity is per-server SKU counts, never a top-level per-card menu."""

    assert "gpus" not in status
    assert "servers" not in status
    servers = _capacity_servers(status)
    if server_id is not None:
        servers = [item for item in servers if item["server_id"] == server_id]
    assert len(servers) == 1
    skus = servers[0]["gpus"]
    assert len(skus) == 1
    sku = skus[0]
    assert sku == {
        "name": "Test GPU",
        "vram_mib": 100_000,
        "total_count": total_count,
        "available_count": available_count,
    }
    return sku


def test_apply_use_release_round_trip(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))

    status = tools.gpu_status()
    _assert_sku_capacity(status, available_count=2, total_count=2)
    assert "busy_gpus" not in status
    server_id = _capacity_servers(status)[0]["server_id"]

    claimed = tools.gpu_apply(server_id=server_id, gpu_count=2, task="端到端训练")
    assert claimed["cuda_device_order"] == "PCI_BUS_ID"
    assert len(claimed["gpus"]) == 2
    assert claimed["cuda_visible_devices"] == ",".join(
        str(gpu["cuda_ordinal"]) for gpu in claimed["gpus"]
    )

    # A claimed card leaves allocatable SKU count and is named in busy_gpus.
    after = tools.gpu_status()
    _assert_sku_capacity(after, available_count=0, total_count=2, server_id=server_id)
    assert {item["task"] for item in after["busy_gpus"]} == {"端到端训练"}
    assert {item["gpu_id"] for item in after["busy_gpus"]} == {
        gpu["gpu_id"] for gpu in claimed["gpus"]
    }
    assert after["no_capacity"]["reason"] == "all_gpus_busy_or_unavailable"

    released = tools.gpu_release(claimed["lease_id"])
    assert released == {
        "released": True,
        "lease_id": claimed["lease_id"],
        "state": "RELEASED",
    }
    _assert_sku_capacity(tools.gpu_status(), available_count=2, total_count=2, server_id=server_id)


def test_second_agent_sees_no_capacity_then_reclaims_the_idle_card(routine) -> None:  # type: ignore[no-untyped-def]
    """The whole point of reclaim: a forgotten claim stops blocking others."""

    routine.ingest_observation(observation(count=1))
    claimed = tools.gpu_apply(gpu_count=1, task="忘记释放的任务")

    with pytest.raises(BrokerClientError) as blocked:
        tools.gpu_apply(gpu_count=1, task="被挡住的任务")
    assert "no_capacity" in str(blocked.value)

    _age_idle(routine, claimed["lease_id"], routine.inventory.idle_lease_reclaim_seconds + 5)
    routine.ingest_observation(observation(count=1))

    reclaimed = tools.gpu_status()
    _assert_sku_capacity(reclaimed, available_count=1, total_count=1)
    assert "no_capacity" not in reclaimed
    second = tools.gpu_apply(gpu_count=1, task="被挡住的任务")
    assert second["lease_id"] != claimed["lease_id"]
    assert len(second["gpus"]) == 1


def test_partly_used_claim_keeps_its_working_card_and_returns_the_rest(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    claimed = tools.gpu_apply(gpu_count=2, task="只用一张卡")
    lease_id = claimed["lease_id"]
    busy_uuid = "GPU-endpoint-a-0"
    busy_id, idle_id = f"endpoint-a:{busy_uuid}", "endpoint-a:GPU-endpoint-a-1"

    routine.ingest_observation(observation(count=2, processes=[process_for_gpu(busy_uuid)]))
    _age_idle(
        routine,
        lease_id,
        routine.inventory.idle_lease_reclaim_seconds + 5,
        gpu_ids={idle_id},
    )
    routine.ingest_observation(observation(count=2, processes=[process_for_gpu(busy_uuid)]))

    assert _active_gpu_ids(routine, lease_id) == {busy_id}
    # Reclaim is per-card: the idle sibling is advertised as SKU capacity while
    # the working card stays named in busy_gpus.
    after_reclaim = tools.gpu_status()
    _assert_sku_capacity(after_reclaim, available_count=1, total_count=2)
    assert "no_capacity" not in after_reclaim
    assert [item["task"] for item in after_reclaim["busy_gpus"]] == ["只用一张卡"]
    # The returned card is immediately usable by another claim.
    other = tools.gpu_apply(gpu_count=1, task="接手空闲卡")
    assert len(other["gpus"]) == 1
    assert other["gpus"][0]["gpu_id"] == "GPU-endpoint-a-1"

    # And the original claim is still alive on its working card.
    def lease_state(session):  # type: ignore[no-untyped-def]
        return session.get(Lease, lease_id).state

    assert routine._read(lease_state) not in {"EXPIRED_EMPTY", "RELEASED"}


def test_releasing_one_of_two_leases_does_not_touch_the_other(routine) -> None:  # type: ignore[no-untyped-def]
    """The contract asks agents to confirm each lease; the broker must honour it."""

    routine.ingest_observation(observation(count=2))
    first = tools.gpu_apply(gpu_count=1, task="任务甲")
    assert len(first["gpus"]) == 1
    after_one = tools.gpu_status()
    # A one-GPU lease on a two-GPU host leaves the sibling advertised as capacity.
    _assert_sku_capacity(after_one, available_count=1, total_count=2)
    assert "no_capacity" not in after_one
    assert [item["task"] for item in after_one["busy_gpus"]] == ["任务甲"]
    assert after_one["busy_gpus"][0]["gpu_id"] == first["gpus"][0]["gpu_id"]

    second = tools.gpu_apply(gpu_count=1, task="任务乙")
    assert first["lease_id"] != second["lease_id"]
    assert len(second["gpus"]) == 1

    assert tools.gpu_release(first["lease_id"])["state"] == "RELEASED"

    status = tools.gpu_status()
    _assert_sku_capacity(status, available_count=1, total_count=2)
    assert [item["task"] for item in status["busy_gpus"]] == ["任务乙"]
    assert status["busy_gpus"][0]["gpu_id"] == second["gpus"][0]["gpu_id"]
    assert tools.gpu_release(second["lease_id"])["state"] == "RELEASED"
    _assert_sku_capacity(tools.gpu_status(), available_count=2, total_count=2)


def test_server_id_filter_narrows_status_to_one_server(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    full = tools.gpu_status()
    server_id = _capacity_servers(full)[0]["server_id"]
    _assert_sku_capacity(full, available_count=2, total_count=2, server_id=server_id)

    narrowed = tools.gpu_status(server_id=server_id)
    assert [item["server_id"] for item in _capacity_servers(narrowed)] == [server_id]
    _assert_sku_capacity(narrowed, available_count=2, total_count=2, server_id=server_id)

    missing = tools.gpu_status(server_id="no-such-server")
    assert "gpus" not in missing
    assert "servers" not in missing
    assert _capacity_servers(missing) == []
