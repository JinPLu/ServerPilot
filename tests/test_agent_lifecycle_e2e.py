"""End-to-end agent lifecycle over the real REST surface the MCP tools call.

These exercise the three routine tools against a live ASGI app and a real
database rather than a fake client, so a break in the wiring between the MCP
projection, the REST route and the domain service fails here.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot import mcp_server
from serverpilot.api import create_app
from serverpilot.client import BrokerClientError
from serverpilot.config import Settings
from serverpilot.models import Lease, LeaseResource
from serverpilot.timeutil import utcnow
from tests.helpers import observation, process_for_gpu


@pytest.fixture()
def routine(tmp_path: Path, inventory, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A live broker wired to the routine MCP tools through HTTP."""

    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'e2e.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
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


def test_apply_use_release_round_trip(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))

    status = mcp_server.gpu_status()
    assert len(status["servers"]) == 1
    assert len(status["gpus"]) == 2
    assert "busy_gpus" not in status
    server_id = status["servers"][0]["server_id"]

    claimed = mcp_server.gpu_apply(server_id=server_id, gpu_count=2, task="端到端训练")
    assert claimed["cuda_device_order"] == "PCI_BUS_ID"
    assert len(claimed["gpus"]) == 2
    assert claimed["cuda_visible_devices"] == ",".join(
        str(gpu["cuda_ordinal"]) for gpu in claimed["gpus"]
    )

    # A claimed card leaves the allocatable list and is named in busy_gpus.
    after = mcp_server.gpu_status()
    assert after["gpus"] == []
    assert {item["task"] for item in after["busy_gpus"]} == {"端到端训练"}
    assert after["no_capacity"]["reason"] == "all_gpus_busy_or_unavailable"

    released = mcp_server.gpu_release(claimed["lease_id"])
    assert released == {
        "released": True,
        "lease_id": claimed["lease_id"],
        "state": "RELEASED",
    }
    assert len(mcp_server.gpu_status()["gpus"]) == 2


def test_second_agent_sees_no_capacity_then_reclaims_the_idle_card(routine) -> None:  # type: ignore[no-untyped-def]
    """The whole point of reclaim: a forgotten claim stops blocking others."""

    routine.ingest_observation(observation(count=1))
    claimed = mcp_server.gpu_apply(gpu_count=1, task="忘记释放的任务")

    with pytest.raises(BrokerClientError) as blocked:
        mcp_server.gpu_apply(gpu_count=1, task="被挡住的任务")
    assert "no_capacity" in str(blocked.value)

    _age_idle(routine, claimed["lease_id"], routine.inventory.idle_lease_reclaim_seconds + 5)
    routine.ingest_observation(observation(count=1))

    assert len(mcp_server.gpu_status()["gpus"]) == 1
    second = mcp_server.gpu_apply(gpu_count=1, task="被挡住的任务")
    assert second["lease_id"] != claimed["lease_id"]


def test_partly_used_claim_keeps_its_working_card_and_returns_the_rest(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    claimed = mcp_server.gpu_apply(gpu_count=2, task="只用一张卡")
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
    # The returned card is immediately usable by another claim.
    other = mcp_server.gpu_apply(gpu_count=1, task="接手空闲卡")
    assert other["gpus"][0]["gpu_id"] == "GPU-endpoint-a-1"
    # And the original claim is still alive on its working card.
    def lease_state(session):  # type: ignore[no-untyped-def]
        return session.get(Lease, lease_id).state

    assert routine._read(lease_state) not in {"EXPIRED_EMPTY", "RELEASED"}


def test_releasing_one_of_two_leases_does_not_touch_the_other(routine) -> None:  # type: ignore[no-untyped-def]
    """The contract asks agents to confirm each lease; the broker must honour it."""

    routine.ingest_observation(observation(count=2))
    first = mcp_server.gpu_apply(gpu_count=1, task="任务甲")
    second = mcp_server.gpu_apply(gpu_count=1, task="任务乙")
    assert first["lease_id"] != second["lease_id"]

    assert mcp_server.gpu_release(first["lease_id"])["state"] == "RELEASED"

    status = mcp_server.gpu_status()
    assert len(status["gpus"]) == 1
    assert [item["task"] for item in status["busy_gpus"]] == ["任务乙"]
    assert mcp_server.gpu_release(second["lease_id"])["state"] == "RELEASED"
    assert len(mcp_server.gpu_status()["gpus"]) == 2


def test_server_id_filter_narrows_status_to_one_server(routine) -> None:  # type: ignore[no-untyped-def]
    routine.ingest_observation(observation(count=2))
    full = mcp_server.gpu_status()
    server_id = full["servers"][0]["server_id"]

    narrowed = mcp_server.gpu_status(server_id=server_id)
    assert [item["server_id"] for item in narrowed["servers"]] == [server_id]
    assert {item["server_id"] for item in narrowed["gpus"]} == {server_id}

    missing = mcp_server.gpu_status(server_id="no-such-server")
    assert missing["gpus"] == []
    assert missing["servers"] == []
