from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot import mcp_server
from serverpilot.config import InventoryConfig, ServerGroupConfig
from serverpilot.models import Endpoint, GPUDevice
from serverpilot.schemas import EndpointUpdate, ServerGroupCreate
from serverpilot.service import BrokerService
from tests.helpers import observation
from tests.test_service import _backdate_idle_since, _make_persistent, request_data

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="a plugin is a POSIX executable found by its shebang and executable bit",
)

PLUGIN_OVERLAY = {
    "plugin_id": "slurm-immediate",
    "allocation_ref": "1134001",
    "ssh": {"host": "login.example.test", "port": 22, "user": "alice"},
    "workspace_path": "/home/alice/work",
    "cuda_visible_devices": "0",
    "gpus": [{"gpu_uuid": "GPU-real", "name": "Example GPU 80GB", "total_vram_mib": 81920}],
}


def _use_plugin_profile(service, endpoint_id: str = "endpoint-a") -> None:
    def write(session):  # type: ignore[no-untyped-def]
        endpoint = session.get(Endpoint, endpoint_id)
        assert endpoint is not None
        endpoint.observation_profile = "slurm-immediate"

    service._write(write)


def test_plugin_endpoint_purges_unobserved_rows(service, admin) -> None:
    _use_plugin_profile(service)
    service.ingest_observation(observation(count=3, gpu_uuids=["fake-0", "fake-1", "fake-2"]))

    def count_gpus(session):  # type: ignore[no-untyped-def]
        return len(
            session.scalars(select(GPUDevice).where(GPUDevice.endpoint_id == "endpoint-a")).all()
        )

    assert service._read(count_gpus) == 3
    empty = observation(count=0, gpu_uuids=[])
    empty = empty.model_copy(
        update={
            "gpu_probe_status": "cpu_only",
            "scheduler": {"free_gpu_count": 30, "gpu_name": "Example GPU 80GB"},
        }
    )
    ingested = service.ingest_observation(empty)
    assert ingested["gpu_count"] == 0
    assert ingested["absent_gpu_count"] == 0
    assert service._read(count_gpus) == 0
    snapshot = service.snapshot(admin)
    endpoint = next(item for item in snapshot["data"]["endpoints"] if item["id"] == "endpoint-a")
    assert endpoint["scheduler_capacity"] == {
        "free_gpu_count": 30,
            "gpu_name": "Example GPU 80GB",
    }
    assert endpoint["monitor"]["absent_gpu_count"] == 0


def test_plugin_endpoint_purges_gpus_after_inactive_lease_rows(
    service, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_plugin_profile(service)
    service.ingest_observation(observation(count=1, gpu_uuids=["fake-0"]))
    monkeypatch.setattr(
        "serverpilot.plugins.apply_plugin",
        lambda *args, **kwargs: {
            "plugin_id": "slurm-immediate",
            "allocation_ref": "1",
            "ssh": {"host": "login.example.test", "port": 22, "user": "alice"},
            "workspace_path": "/home/work",
            "cuda_visible_devices": "0",
            "gpus": [],
        },
    )
    monkeypatch.setattr(
        "serverpilot.plugins.release_plugin",
        lambda *args, **kwargs: {"state": "released"},
    )
    allocated = service.create_request(admin, request_data("then-release"), idempotency_key="then-release")
    service.release_lease(
        admin, allocated["lease"]["id"], reason="cleanup", idempotency_key="then-release-out"
    )
    empty = observation(count=0, gpu_uuids=[]).model_copy(update={"gpu_probe_status": "cpu_only"})
    ingested = service.ingest_observation(empty)
    assert ingested["gpu_count"] == 0

    def count_gpus(session):  # type: ignore[no-untyped-def]
        return len(
            session.scalars(select(GPUDevice).where(GPUDevice.endpoint_id == "endpoint-a")).all()
        )

    assert service._read(count_gpus) == 0


@pytest.mark.parametrize("overlay", [PLUGIN_OVERLAY, None], ids=["with-overlay", "without-overlay"])
def test_allocating_a_lease_never_asks_the_plugin_for_more(
    service, admin, monkeypatch: pytest.MonkeyPatch, overlay
) -> None:
    """Cluster capacity is requested before a lease exists, never after.

    The ledger's allocatable cards on a scheduler endpoint are the caller's own
    running jobs. Asking the plugin again once they are leased would open a
    second cluster job for cards already held, and only the first of several
    allocations was ever recorded, so the rest could not be cancelled.
    """

    _use_plugin_profile(service)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))

    def fake_apply(plugin_id: str, *, gpu_count: int, task_ref: str) -> dict[str, object]:
        raise AssertionError("create_request must not allocate cluster capacity")

    monkeypatch.setattr("serverpilot.plugins.apply_plugin", fake_apply)
    result = service.create_request(
        admin,
        request_data("pin-overlay"),
        idempotency_key="pin-overlay",
        plugin_allocation=overlay,
    )

    assert result["lease"] is not None
    constraints = result["lease"]["constraints"]
    if overlay is None:
        assert "plugin_allocation" not in constraints
    else:
        assert constraints["plugin_allocation"]["allocation_ref"] == "1134001"


def test_idle_reclaim_releases_plugin_allocation(
    service, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_plugin_profile(service)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))
    monkeypatch.setattr(
        "serverpilot.plugins.apply_plugin",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("apply")),
    )
    releases: list[tuple[str, str]] = []

    def fake_release(plugin_id: str, *, allocation_ref: str) -> dict[str, str]:
        releases.append((plugin_id, allocation_ref))
        return {"state": "released"}

    monkeypatch.setattr("serverpilot.plugins.release_plugin", fake_release)
    allocated = service.create_request(
        admin,
        request_data("idle-plugin"),
        idempotency_key="idle-plugin",
        plugin_allocation=PLUGIN_OVERLAY,
    )
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))
    assert releases == [("slurm-immediate", "1134001")]


def test_linux_nvidia_idle_reclaim_does_not_call_plugin_release(
    service, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    releases: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "serverpilot.plugins.release_plugin",
        lambda plugin_id, *, allocation_ref: releases.append((plugin_id, allocation_ref))
        or {"state": "released"},
    )
    service.ingest_observation(observation(count=1))
    allocated = service.create_request(
        admin, request_data("builtin-idle"), idempotency_key="builtin-idle"
    )
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(observation(count=1))
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(observation(count=1))
    assert releases == []


def test_claim_applies_plugin_once_on_no_capacity(
    build_app, inventory: InventoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_inventory = inventory.model_copy(
        update={
            "endpoints": [
                inventory.endpoints[0].model_copy(
                    update={"observation_profile": "slurm-immediate"}
                ),
                *inventory.endpoints[1:],
            ]
        }
    )
    apply_calls: list[tuple[str, int, str]] = []
    collector = _ClaimCollector()

    def fake_apply(plugin_id: str, *, gpu_count: int, task_ref: str) -> dict[str, object]:
        apply_calls.append((plugin_id, gpu_count, task_ref))
        collector.ready = True
        return PLUGIN_OVERLAY

    monkeypatch.setattr("serverpilot.plugins.apply_plugin", fake_apply)
    monkeypatch.setattr(
        "serverpilot.plugins.release_plugin",
        lambda *args, **kwargs: {"state": "released"},
    )
    app = build_app(
        "plugin-claim",
        inventory_config=plugin_inventory,
        collector=collector,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/routine/claims",
        json={
            "project_id": "project-a",
            "task_ref": "need-plugin",
            "purpose": "apply then claim",
            "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-a"]},
        },
        headers={"X-ServerPilot-Actor": "plugin-agent", "Idempotency-Key": "need-plugin"},
    )
    assert response.status_code == 200, response.text
    assert apply_calls == [("slurm-immediate", 1, "need-plugin")]
    lease = response.json()["lease"]
    assert lease["gpu_ids"] == ["endpoint-a:GPU-real"]
    assert lease["resources"][0]["cuda_visible_devices"] == "0"


def test_legacy_count_name_plugin_capacity_still_decodes() -> None:
    assert BrokerService._decode_plugin_capacity("27|NVIDIA A100-SXM4-80GB") == {
        "free_gpu_count": 27,
        "gpu_name": "NVIDIA A100-SXM4-80GB",
    }
    encoded = BrokerService._encode_plugin_capacity(
        {
            "free_gpu_count": 27,
            "gpu_name": "NVIDIA A100-SXM4-80GB",
            "largest_free_block": 8,
            "vram_mib": 81920,
            "max_gpus_per_lease": 8,
        }
    )
    assert encoded.startswith("{")
    assert BrokerService._decode_plugin_capacity(encoded) == {
        "free_gpu_count": 27,
        "gpu_name": "NVIDIA A100-SXM4-80GB",
        "largest_free_block": 8,
        "vram_mib": 81920,
        "max_gpus_per_lease": 8,
    }


def test_delegated_group_snapshot_uses_scheduler_block_not_cluster_sum(
    service, admin
) -> None:
    service.create_server_group(
        admin,
        ServerGroupCreate(
            id="hanhai22",
            display_name="瀚海 22",
            workspace_path="/home/work",
        ),
        idempotency_key="make-hanhai",
    )
    _use_plugin_profile(service)
    service.update_endpoint(
        admin,
        "endpoint-a",
        EndpointUpdate(server_group_id="hanhai22"),
        idempotency_key="bind-hanhai",
    )
    empty = observation(count=0, gpu_uuids=[]).model_copy(
        update={
            "gpu_probe_status": "cpu_only",
            "scheduler": {
                "free_gpu_count": 27,
                "gpu_name": "NVIDIA A100-SXM4-80GB",
                "largest_free_block": 3,
                "vram_mib": 81920,
                "max_gpus_per_lease": 8,
                "cpu_cores_per_gpu": 8,
                "memory_mib_per_gpu": 16384,
            },
        }
    )
    service.ingest_observation(empty)
    snapshot = service.snapshot(admin)
    group = next(item for item in snapshot["data"]["server_groups"] if item["id"] == "hanhai22")
    assert group["allocation"] == "delegated"
    assert group["largest_allocatable_block"] == 3
    assert group["limits"]["max_gpus_per_lease"] == 8
    assert group["limits"]["lease_ends"] == "hard_kill_at_time_limit"
    assert group["limits"]["max_lease_seconds"] == 3600
    assert group["limits"]["cpu_cores_per_gpu"] == 8
    assert group["limits"]["memory_mib_per_gpu"] == 16384
    assert group["limits"]["apply_max_seconds"] == 33
    assert group["limits"]["queues"] is False
    projected = next(
        item
        for item in mcp_server._routine_gpu_status(snapshot, lease_id=None)["server_groups"]
        if item["id"] == "hanhai22"
    )
    sku = projected["servers"][0]["gpus"][0]
    assert sku["vram_mib"] == 81920
    assert "total_count" not in sku


def test_legacy_count_name_capacity_does_not_treat_pool_total_as_one_apply_limit(
    service, admin
) -> None:
    service.create_server_group(
        admin,
        ServerGroupCreate(
            id="hanhai22",
            display_name="瀚海 22",
            workspace_path="/home/work",
        ),
        idempotency_key="make-hanhai-legacy",
    )
    _use_plugin_profile(service)
    service.update_endpoint(
        admin,
        "endpoint-a",
        EndpointUpdate(server_group_id="hanhai22"),
        idempotency_key="bind-hanhai-legacy",
    )
    empty = observation(count=0, gpu_uuids=[]).model_copy(
        update={
            "gpu_probe_status": "cpu_only",
            "scheduler": {
                "free_gpu_count": 27,
                "gpu_name": "NVIDIA A100-SXM4-80GB",
            },
        }
    )
    service.ingest_observation(empty)
    snapshot = service.snapshot(admin)
    group = next(item for item in snapshot["data"]["server_groups"] if item["id"] == "hanhai22")
    endpoint = next(item for item in snapshot["data"]["endpoints"] if item["id"] == "endpoint-a")
    assert group["allocation"] == "delegated"
    assert group["largest_allocatable_block"] is None
    assert group["limits"]["max_gpus_per_lease"] is None
    assert group["limits"]["cpu_cores_per_gpu"] is None
    assert group["limits"]["memory_mib_per_gpu"] is None
    assert endpoint["scheduler_capacity"] == {
        "free_gpu_count": 27,
        "gpu_name": "NVIDIA A100-SXM4-80GB",
    }
    status = mcp_server._routine_gpu_status(snapshot, lease_id=None)
    projected = next(item for item in status["server_groups"] if item["id"] == "hanhai22")
    sku = projected["servers"][0]["gpus"][0]
    assert sku == {"name": "NVIDIA A100-SXM4-80GB", "available_count": 27}
    assert "vram_mib" not in sku
    assert "total_count" not in sku


def test_claim_applies_plugin_when_only_server_group_is_selected(
    build_app, inventory: InventoryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_inventory = inventory.model_copy(
        update={
            "server_groups": [
                ServerGroupConfig(
                    id="hanhai22",
                    display_name="瀚海 22",
                    workspace_path="/home/work",
                )
            ],
            "endpoints": [
                inventory.endpoints[0].model_copy(
                    update={
                        "observation_profile": "slurm-immediate",
                        "server_group_id": "hanhai22",
                    }
                ),
                *inventory.endpoints[1:],
            ],
        }
    )
    apply_calls: list[tuple[str, int, str]] = []
    collector = _ClaimCollector()

    def fake_apply(plugin_id: str, *, gpu_count: int, task_ref: str) -> dict[str, object]:
        apply_calls.append((plugin_id, gpu_count, task_ref))
        collector.ready = True
        return PLUGIN_OVERLAY

    monkeypatch.setattr("serverpilot.plugins.apply_plugin", fake_apply)
    monkeypatch.setattr(
        "serverpilot.plugins.release_plugin",
        lambda *args, **kwargs: {"state": "released"},
    )
    app = build_app(
        "plugin-group-claim",
        inventory_config=plugin_inventory,
        collector=collector,
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/routine/claims",
        json={
            "project_id": "project-a",
            "task_ref": "need-group-plugin",
            "purpose": "apply via group",
            "constraints": {"gpu_count": 1, "server_group_ids": ["hanhai22"]},
        },
        headers={"X-ServerPilot-Actor": "plugin-agent", "Idempotency-Key": "need-group-plugin"},
    )
    assert response.status_code == 200, response.text
    assert apply_calls == [("slurm-immediate", 1, "need-group-plugin")]
    lease = response.json()["lease"]
    assert lease["gpu_ids"] == ["endpoint-a:GPU-real"]


class _ClaimCollector:
    def __init__(self) -> None:
        self.ready = False

    async def collect_selected(self, service, endpoints):  # type: ignore[no-untyped-def]
        endpoint = (endpoints or [None])[0]
        if self.ready and endpoint is not None:
            service.ingest_observation(
                observation(endpoint_id=endpoint.id, count=1, gpu_uuids=["GPU-real"])
            )
            return {endpoint.id: {"ok": True}}
        if endpoint is None:
            return {}
        return {endpoint.id: {"ok": True}}
