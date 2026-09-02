from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig
from serverpilot.database import Database
from serverpilot.models import (
    Actor,
    Alert,
    AllocationRequest,
    AuditEvent,
    EndpointTelemetrySnapshot,
    GPUDevice,
    Lease,
    LeaseResource,
    ProcessObservation,
    ProviderState,
    TelemetryCurrent,
    TelemetrySnapshot,
)
from serverpilot.schemas import (
    EndpointCreate,
    EndpointObservation,
    EndpointUpdate,
    LeaseObservedBind,
    ProcessInput,
    RequestCreate,
)
from serverpilot.service import ACTIVE_LEASE_STATES, ActorContext, BrokerError, BrokerService
from serverpilot.timeutil import utcnow
from tests.helpers import (
    age_out_lease_holder,
    age_out_processes,
    observation,
    process_for_gpu,
)


def request_data(task_ref: str, *, count: int = 1, project_id: str = "project-a") -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": project_id,
            "task_ref": task_ref,
            "purpose": "unit-test cooperative request",
            "duration_seconds": 3600,
            "constraints": {"gpu_count": count, "placement": "pack"},
        }
    )


def test_inventory_unknown_is_fail_closed(service, admin) -> None:
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("unknown"), idempotency_key="unknown-1")
    assert error.value.code == "no_capacity"
    assert service.list_requests(admin)["data"] == []


def test_initialize_has_no_bootstrap_login_token(tmp_path: Path, inventory) -> None:
    broker = BrokerService(
        Database(
            f"sqlite:///{tmp_path / 'bootstrap.sqlite3'}", Path(__file__).resolve().parents[1]
        ),
        inventory,
    )
    broker.initialize()
    broker.initialize()
    assert broker.local_actor("local-agent").role == "allocator"


def test_write_propagates_first_database_busy_error_without_retry(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class LockedSession:
        execute_calls = 0
        rollback_calls = 0

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def execute(self, _statement):  # type: ignore[no-untyped-def]
            self.execute_calls += 1
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

        def rollback(self) -> None:
            self.rollback_calls += 1

    locked = LockedSession()
    monkeypatch.setattr(service.database, "session", lambda: locked)

    with pytest.raises(OperationalError, match="database is locked"):
        service._write(lambda _session: None)

    assert locked.execute_calls == 1
    assert locked.rollback_calls == 1


def test_idempotent_request_and_stable_uuid_identity(service, admin) -> None:
    service.ingest_observation(observation())
    first = service.create_request(admin, request_data("idempotent"), idempotency_key="key-1")
    second = service.create_request(admin, request_data("idempotent"), idempotency_key="key-1")
    assert first == second
    assert first["lease"] is not None
    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["id"] == f"endpoint-a:{gpu['gpu_uuid']}"
    assert gpu["state"] == "HELD"


def test_complete_observation_reconciles_active_gpu_set(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old-0", "GPU-old-1", "GPU-stays"]))
    result = service.ingest_observation(observation(gpu_uuids=["GPU-new-0", "GPU-stays"]))

    snapshot = service.snapshot(admin)["data"]

    assert result["absent_gpu_count"] == 2
    assert [gpu["id"] for gpu in snapshot["gpus"]] == [
        "endpoint-a:GPU-new-0",
        "endpoint-a:GPU-stays",
    ]
    assert snapshot["summary"]["total_gpus"] == 2
    assert snapshot["endpoints"][0]["monitor"]["gpu_count"] == 2
    assert snapshot["endpoints"][0]["monitor"]["absent_gpu_count"] == 2
    assert snapshot["absent_gpu_ids"] == ["endpoint-a:GPU-old-0", "endpoint-a:GPU-old-1"]


def test_incomplete_observation_does_not_mark_prior_gpus_absent(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old-0", "GPU-stays"]))
    result = service.ingest_observation(
        observation(gpu_uuids=["GPU-stays"], observation_complete=False)
    )

    snapshot = service.snapshot(admin)["data"]

    assert result["absent_gpu_count"] == 0
    assert result["observation_complete"] is False
    assert [gpu["id"] for gpu in snapshot["gpus"]] == [
        "endpoint-a:GPU-old-0",
        "endpoint-a:GPU-stays",
    ]


def test_observation_gpu_uuid_index_and_cuda_ordinal_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique gpu_uuid"):
        observation(gpu_uuids=["GPU-dup", "GPU-dup"])

    base = observation(gpu_uuids=["GPU-a", "GPU-b"]).model_dump()
    base["gpus"][1]["gpu_index"] = base["gpus"][0]["gpu_index"]
    with pytest.raises(ValidationError, match="unique gpu_index"):
        EndpointObservation.model_validate(base)

    base = observation(gpu_uuids=["GPU-a", "GPU-b"]).model_dump()
    base["gpus"][1]["cuda_ordinal"] = base["gpus"][0]["cuda_ordinal"]
    with pytest.raises(ValidationError, match="unique cuda_ordinal"):
        EndpointObservation.model_validate(base)


def test_absent_gpu_keeps_lease_but_is_not_eligible(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    exact_old = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "old-gpu",
            "purpose": "hold old GPU",
            "duration_seconds": 3600,
            "constraints": {
                "gpu_count": 1,
                "placement": "exact",
                "gpu_ids": ["endpoint-a:GPU-old"],
            },
        }
    )
    claimed_old = service.create_request(admin, exact_old, idempotency_key="old-gpu")
    assert claimed_old["lease"] is not None

    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    claim_new = service.create_request(admin, request_data("new-gpu"), idempotency_key="new-gpu")
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("no-more-gpus"), idempotency_key="no-more-gpus")
    snapshot = service.snapshot(admin)["data"]

    assert claim_new["lease"]["gpu_ids"] == ["endpoint-a:GPU-new"]
    assert error.value.code == "no_capacity"
    assert snapshot["gpus"][0]["id"] == "endpoint-a:GPU-new"
    assert any("endpoint-a:GPU-old" in lease["gpu_ids"] for lease in snapshot["leases"])


def test_absent_gpu_in_multi_gpu_lease_suppresses_all_executable_resources(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    claimed = service.create_request(
        admin,
        request_data("two-gpu-lease", count=2),
        idempotency_key="two-gpu-lease",
    )
    assert claimed["lease"] is not None
    assert len(claimed["lease"]["resources"]) == 1

    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    lease = service.list_leases(admin)["data"][0]
    snapshot_lease = service.snapshot(admin)["data"]["leases"][0]

    assert set(lease["gpu_ids"]) == {"endpoint-a:GPU-old", "endpoint-a:GPU-new"}
    assert lease["absent_gpu_ids"] == ["endpoint-a:GPU-old"]
    assert lease["resources"] == []
    assert snapshot_lease["resources"] == []


def test_incomplete_observation_preserves_unobserved_process_and_blocks_exact_claim(
    service, admin
) -> None:
    service.ingest_observation(
        observation(
            gpu_uuids=["GPU-old", "GPU-new"],
            processes=[process_for_gpu("GPU-old")],
        )
    )
    result = service.ingest_observation(
        observation(gpu_uuids=["GPU-new"], observation_complete=False)
    )

    gpus = {gpu["id"]: gpu for gpu in service.list_gpus(admin)["data"]}
    exact_old = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "old-incomplete",
            "purpose": "should remain blocked by preserved process",
            "duration_seconds": 3600,
            "constraints": {
                "gpu_count": 1,
                "placement": "exact",
                "gpu_ids": ["endpoint-a:GPU-old"],
            },
        }
    )
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, exact_old, idempotency_key="old-incomplete")

    assert result["observation_complete"] is False
    assert gpus["endpoint-a:GPU-old"]["state"] == "BUSY_UNMANAGED"
    assert len(gpus["endpoint-a:GPU-old"]["processes"]) == 1
    assert error.value.code == "no_capacity"


def test_absent_gpu_reappearance_restores_presence(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-stays"]))
    service.ingest_observation(observation(gpu_uuids=["GPU-stays"]))
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-stays"]))

    snapshot = service.snapshot(admin)["data"]

    assert [gpu["id"] for gpu in snapshot["gpus"]] == [
        "endpoint-a:GPU-old",
        "endpoint-a:GPU-stays",
    ]
    assert snapshot["absent_gpu_ids"] == []
    assert snapshot["summary"]["total_gpus"] == 2


def test_stale_observation_is_ignored_without_mutating_latest_presence(service, admin) -> None:
    first_seen = utcnow()
    second_seen = first_seen + timedelta(seconds=10)
    service.ingest_observation(observation(gpu_uuids=["GPU-new"], observed_at=second_seen))
    before = service.snapshot(admin)

    result = service.ingest_observation(observation(gpu_uuids=["GPU-old"], observed_at=first_seen))
    after = service.snapshot(admin)

    assert result["ignored"] is True
    assert result["ignore_reason"] == "stale_observation"
    assert result["snapshot_revision"] == before["snapshot_revision"]
    assert after["snapshot_revision"] == before["snapshot_revision"]
    assert [gpu["id"] for gpu in after["data"]["gpus"]] == ["endpoint-a:GPU-new"]
    assert after["data"]["absent_gpu_ids"] == []


def test_routine_claim_fails_immediately_and_can_be_retried_when_capacity_arrives(
    service, admin
) -> None:
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            request_data("queued-run"),
            idempotency_key="queued-claim",
        )
    assert error.value.code == "no_capacity"
    assert service.list_requests(admin)["data"] == []

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("retried-run"),
        idempotency_key="retried-claim",
    )
    assert claimed["request"]["state"] == "LEASED"
    assert claimed["lease"]["state"] == "HELD"


def test_gang_all_or_nothing_and_no_partial_write(service, admin) -> None:
    service.ingest_observation(observation(count=3))
    first = service.create_request(admin, request_data("gang-a", count=2), idempotency_key="gang-a")
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("gang-b", count=2), idempotency_key="gang-b")
    assert first["lease"] is not None
    assert len(first["lease"]["gpu_ids"]) == 2
    assert error.value.code == "no_capacity"
    leases = service.list_leases(admin)["data"]
    assert (
        sum(len(lease["gpu_ids"]) for lease in leases if lease["state"] in ACTIVE_LEASE_STATES) == 2
    )


def test_host_resource_constraints_are_absolute_and_fail_closed(service, admin) -> None:
    service.ingest_observation(observation(count=2))

    too_much_cpu = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "needs-absolute-cpu",
            "purpose": "request more free CPU cores than the endpoint currently has",
            "constraints": {"gpu_count": 1, "min_available_cpu_cores": 61},
        }
    )
    with pytest.raises(BrokerError) as cpu_error:
        service.create_request(admin, too_much_cpu, idempotency_key="absolute-cpu")
    assert cpu_error.value.code == "no_capacity"

    too_much_memory = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "needs-absolute-memory",
            "purpose": "request more free system memory than the endpoint currently has",
            "constraints": {"gpu_count": 1, "min_available_memory_mib": 200 * 1024},
        }
    )
    with pytest.raises(BrokerError) as memory_error:
        service.create_request(admin, too_much_memory, idempotency_key="absolute-memory")
    assert memory_error.value.code == "no_capacity"

    right_sized = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "right-sized-absolute-resources",
            "purpose": "request absolute resources within current telemetry",
            "constraints": {
                "gpu_count": 1,
                "min_available_cpu_cores": 16,
                "min_available_memory_mib": 64 * 1024,
                "min_free_vram_mib": 60 * 1024,
                "min_total_vram_mib": 80 * 1024,
            },
        }
    )
    allocated = service.create_request(admin, right_sized, idempotency_key="absolute-right-sized")
    assert allocated["lease"] is not None


def test_failed_claims_are_not_queued_when_capacity_arrives(service, admin) -> None:
    for task_ref, project_id in (
        ("story-a", "project-a"),
        ("story-b", "project-a"),
        ("project-b-task", "project-b"),
    ):
        with pytest.raises(BrokerError) as error:
            service.create_request(
                admin,
                request_data(task_ref, project_id=project_id),
                idempotency_key=task_ref,
            )
        assert error.value.code == "no_capacity"
    service.ingest_observation(observation(count=3))
    assert service.list_requests(admin)["data"] == []
    assert service.list_leases(admin)["data"] == []


def test_endpoint_identity_is_enforced(service, admin) -> None:
    service.ingest_observation(observation(count=4))
    created = service.create_endpoint(
        admin,
        EndpointCreate(
            id="endpoint-new",
            host="127.0.0.1",
            port=2203,
            ssh_user="gpu",
            workspace_path="/srv/project-new",
            project_ids=["project-a"],
        ),
        idempotency_key="endpoint-new",
    )
    assert created["endpoint"]["id"] == "endpoint-new"
    assert created["endpoint"]["workspace_path"] == "/srv/project-new"
    updated = service.update_endpoint(
        admin,
        "endpoint-new",
        EndpointUpdate(
            ssh_user="gpu-updated",
            workspace_path="/srv/project-new-updated",
        ),
        idempotency_key="endpoint-update",
    )
    assert updated["endpoint"]["ssh_user"] == "gpu-updated"
    assert updated["endpoint"]["workspace_path"] == "/srv/project-new-updated"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EndpointUpdate.model_validate({"host": "127.0.0.1"})
    with pytest.raises(BrokerError) as error:
        service.create_endpoint(
            admin,
            EndpointCreate(
                id="endpoint-new",
                host="127.0.0.1",
                port=2299,
                ssh_user="gpu",
                workspace_path="/srv/project-new",
                project_ids=["project-a"],
            ),
            idempotency_key="endpoint-move",
        )
    assert error.value.code == "endpoint_exists"


def test_endpoint_inventory_mutations_do_not_require_project_membership(service, admin) -> None:
    desktop_actor = ActorContext(
        id=admin.id,
        role="allocator",
        project_ids=frozenset(),
    )
    created = service.create_endpoint(
        desktop_actor,
        EndpointCreate(
            id="desktop-ownerless",
            host="127.0.0.1",
            port=2298,
            ssh_user="gpu",
            workspace_path="/srv/desktop-ownerless",
        ),
        idempotency_key="desktop-ownerless-create",
    )

    assert created["endpoint"]["owner_project_id"] is None
    updated = service.update_endpoint(
        desktop_actor,
        "endpoint-a",
        EndpointUpdate(labels=["desktop-updated"]),
        idempotency_key="desktop-update-without-project",
    )
    assert updated["endpoint"]["labels"] == ["desktop-updated"]


def test_delete_endpoint_removes_idle_server_from_list(service, admin) -> None:
    created = service.create_endpoint(
        admin,
        EndpointCreate(
            id="endpoint-idle-delete",
            host="127.0.0.1",
            port=2296,
            ssh_user="gpu",
            workspace_path="/srv/endpoint-idle-delete",
        ),
        idempotency_key="endpoint-idle-delete-create",
    )
    assert created["endpoint"]["id"] == "endpoint-idle-delete"
    deleted = service.delete_endpoint(
        admin, "endpoint-idle-delete", idempotency_key="endpoint-idle-delete"
    )
    assert deleted["changed"] is True
    assert deleted["endpoint_id"] == "endpoint-idle-delete"
    listed = {endpoint["id"] for endpoint in service.list_endpoints(admin)["data"]}
    assert "endpoint-idle-delete" not in listed
    replayed = service.delete_endpoint(
        admin, "endpoint-idle-delete", idempotency_key="endpoint-idle-delete"
    )
    assert replayed == deleted


def test_delete_endpoint_rejects_active_leases_then_deletes_after_release(service, admin) -> None:
    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "keep-endpoint-b",
                "purpose": "block delete while leased",
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
            }
        ),
        idempotency_key="endpoint-b-claim",
    )
    assert claimed["lease"] is not None
    with pytest.raises(BrokerError) as error:
        service.delete_endpoint(admin, "endpoint-b", idempotency_key="endpoint-b-delete-blocked")
    assert error.value.code == "endpoint_has_active_leases"
    assert error.value.status_code == 409
    service.release_lease(
        admin,
        claimed["lease"]["id"],
        reason="finished before delete",
        idempotency_key="endpoint-b-release",
    )
    deleted = service.delete_endpoint(admin, "endpoint-b", idempotency_key="endpoint-b-delete")
    assert deleted["changed"] is True
    listed = {endpoint["id"] for endpoint in service.list_endpoints(admin)["data"]}
    assert "endpoint-b" not in listed
    assert "endpoint-a" in listed


def test_deleted_inventory_endpoint_is_not_resurrected_on_restart(tmp_path: Path) -> None:
    inventory = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A", weight=1)],
        endpoints=[
            EndpointConfig(
                id="only-server",
                host="127.0.0.1",
                port=2298,
                ssh_user="gpu",
                workspace_path="/srv/only-server",
            )
        ],
    )
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'tombstone-restart.sqlite3'}", root)
    first = BrokerService(database, inventory)
    first.initialize()
    first.local_actor("tombstone-admin")
    with first.database.session() as session:
        actor = session.get(Actor, "tombstone-admin")
        assert actor is not None
        actor.role = "admin"
        session.commit()
    admin = ActorContext(
        id="tombstone-admin",
        role="admin",
        project_ids=frozenset({"project-a"}),
    )
    assert {endpoint["id"] for endpoint in first.list_endpoints(admin)["data"]} == {"only-server"}

    deleted = first.delete_endpoint(admin, "only-server", idempotency_key="delete-only-server")
    assert deleted["changed"] is True
    assert first.list_endpoints(admin)["data"] == []

    restarted = BrokerService(database, inventory)
    restarted.initialize(sync_inventory=True)
    assert restarted.list_endpoints(admin)["data"] == []

    created = restarted.create_endpoint(
        admin,
        EndpointCreate(
            id="only-server",
            host="127.0.0.1",
            port=2298,
            ssh_user="gpu",
            workspace_path="/srv/only-server",
        ),
        idempotency_key="readd-only-server",
    )
    assert created["endpoint"]["id"] == "only-server"
    assert {endpoint["id"] for endpoint in restarted.list_endpoints(admin)["data"]} == {
        "only-server"
    }


def test_claim_auto_creates_project_without_extra_endpoint_scope(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("cross-project-claim", project_id="storyboard"),
        idempotency_key="storyboard-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    assert claimed["request"]["state"] == "LEASED"
    assert claimed["lease"]["state"] == "HELD"
    assert claimed["lease"]["project_id"] == "storyboard"
    assert any(
        lease["project_id"] == "storyboard" for lease in service.snapshot(admin)["data"]["leases"]
    )


def test_observed_binding_is_visible_on_the_control_plane_snapshot(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("coordination-run"),
        idempotency_key="coordination-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu = service.list_gpus(admin)["data"][0]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu["gpu_uuid"])]))

    bound = service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(),
        idempotency_key="coordination-bind",
    )
    assert bound["lease"]["state"] == "ACTIVE"
    assert bound["lease"]["workloads"][0]["run_id"] == (f"explicit:lease:{claimed['lease']['id']}")
    assert len(bound["lease"]["workloads"][0]["process_keys"]) == 1
    assert service.list_requests(admin)["data"][0]["state"] == "ACTIVE"

    current = service.control_plane_state(admin)["data"]["current"]
    gpu = current["gpus"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["process_key"]
    assert current["leases"][0]["actor_id"] == admin.id
    assert current["leases"][0]["state"] == "ACTIVE"


def test_collector_auto_binds_new_process_to_its_exact_workload_lease(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("auto-observed-run"),
        idempotency_key="auto-observed-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    claimed_gpu_id = claimed["lease"]["gpu_ids"][0]
    gpus = service.list_gpus(admin)["data"]
    claimed_gpu_uuid = next(gpu["gpu_uuid"] for gpu in gpus if gpu["id"] == claimed_gpu_id)
    other_gpu_uuid = next(gpu["gpu_uuid"] for gpu in gpus if gpu["id"] != claimed_gpu_id)

    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu(claimed_gpu_uuid, pid=4101),
                process_for_gpu(other_gpu_uuid, pid=4102),
            ],
        )
    )

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    snapshot_lease = next(
        item for item in service.snapshot(admin)["data"]["leases"] if item["id"] == lease_id
    )
    assert snapshot_lease["runtime_state"] == "RUNNING"
    assert lease["workloads"][0]["run_id"] == f"collector:lease:{lease_id}"
    assert lease["workloads"][0]["process_keys"] == [
        next(
            gpu["processes"][0]["process_key"]
            for gpu in service.list_gpus(admin)["data"]
            if gpu["id"] == claimed_gpu_id
        )
    ]
    states = {gpu["gpu_uuid"]: gpu["state"] for gpu in service.list_gpus(admin)["data"]}
    assert states[claimed_gpu_uuid] == "RUNNING_MANAGED"
    assert states[other_gpu_uuid] == "BUSY_UNMANAGED"
    assert service.list_requests(admin)["data"][0]["state"] == "ACTIVE"


def test_bound_workload_clears_historical_keepalive_error_without_mutating_lease(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.set_keepalive_error("endpoint-a", ["endpoint-a:" + gpu_uuid], "start failed")
    claimed = service.create_request(
        admin,
        request_data("workload-after-keepalive-error"),
        idempotency_key="workload-after-keepalive-error-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    process = process_for_gpu(gpu_uuid, pid=4401)

    service.ingest_observation(observation(count=1, processes=[process]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["keepalive"]["actual"] == "OFF"
    assert gpu["keepalive"]["reason"] is None
    assert gpu["state"] == "RUNNING_MANAGED"
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    assert lease["gpu_ids"] == [gpu["id"]]
    assert lease["workloads"][0]["process_keys"] == [gpu["processes"][0]["process_key"]]


def test_unbound_or_unknown_process_does_not_clear_keepalive_error(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    gpu_id = "endpoint-a:" + gpu_uuid
    service.set_keepalive_error("endpoint-a", [gpu_id], "ordinary keepalive failure")

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4402)])
    )

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "BUSY_UNMANAGED"
    assert gpu["keepalive"]["actual"] == "ERROR"
    assert gpu["keepalive"]["reason"] == "ordinary keepalive failure"


def test_assigned_workload_clears_error_after_process_turnover(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("unknown-after-bound"),
        idempotency_key="unknown-after-bound-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    initial = process_for_gpu(gpu_uuid, pid=4403)
    service.ingest_observation(observation(count=1, processes=[initial]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="known-workload"),
        idempotency_key="known-workload-bind",
    )
    service.set_keepalive_error(
        "endpoint-a",
        ["endpoint-a:" + gpu_uuid],
        "ordinary keepalive failure",
    )

    replacement = process_for_gpu(gpu_uuid, pid=4404)
    service.ingest_observation(observation(count=1, processes=[replacement]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["keepalive"]["actual"] == "OFF"
    assert gpu["keepalive"]["reason"] is None


def test_observed_workload_binding_survives_one_second_process_start_jitter(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("jitter-stable-run"),
        idempotency_key="jitter-stable-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    started_at = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"process_started_at": started_at}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="jitter-stable-run-1"),
        idempotency_key="jitter-stable-bind",
    )

    jittered_process = initial_process.model_copy(
        update={"process_started_at": started_at + timedelta(seconds=1)}
    )
    service.ingest_observation(observation(count=1, processes=[jittered_process]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["observations"] == 2
    assert gpu["lease"]["workloads"][0]["process_keys"] == [gpu["processes"][0]["process_key"]]


def test_observed_workload_binding_survives_continuous_invisible_pid_metadata(
    service, admin
) -> None:
    """A namespace-hidden PID must not acquire a new identity every collection."""

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("namespace-hidden-run"),
        idempotency_key="namespace-hidden-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    first_seen = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"username": None, "process_started_at": first_seen}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="namespace-hidden-run-1"),
        idempotency_key="namespace-hidden-bind",
    )
    original_key = service.list_gpus(admin)["data"][0]["processes"][0]["process_key"]

    # Missing `ps` metadata makes the collector use each observation time as
    # its fallback start time. Continuous GPU/PID/boot observations still
    # identify the already-active process.
    later_sample = initial_process.model_copy(
        update={"process_started_at": first_seen + timedelta(seconds=10)}
    )
    service.ingest_observation(observation(count=1, processes=[later_sample]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["observations"] == 2
    assert gpu["processes"][0]["process_key"] == original_key
    assert gpu["lease"]["workloads"][0]["process_keys"] == [original_key]


def test_multi_gpu_task_stays_running_during_mixed_worker_turnover(service, admin) -> None:
    service.ingest_observation(observation(count=4))
    claimed = service.create_request(
        admin,
        request_data("bridge-to-lpt", count=4, project_id="agent"),
        idempotency_key="bridge-to-lpt-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuids = [
        gpu["gpu_uuid"]
        for gpu in service.list_gpus(admin)["data"]
        if gpu["id"] in claimed["lease"]["gpu_ids"]
    ]
    first_cohort = [
        process_for_gpu(gpu_uuid, pid=4_100 + index) for index, gpu_uuid in enumerate(gpu_uuids)
    ]
    service.ingest_observation(observation(count=4, processes=first_cohort))

    replacement_cohort = [
        process_for_gpu(gpu_uuid, pid=5_100 + index) for index, gpu_uuid in enumerate(gpu_uuids)
    ]
    # A bridge-to-LPT hand-off can have old and new workers overlap on some
    # lanes while other lanes have already switched or are still on bridge.
    mixed_cohort = [
        first_cohort[0],
        replacement_cohort[0],
        replacement_cohort[1],
        first_cohort[2],
        first_cohort[3],
        replacement_cohort[3],
    ]
    # One lane has already finished with its bridge worker. A process is
    # retired by age now, so "gone" has to be expressed by ageing its last
    # sighting -- one observation that leaves it out is a single absent sample.
    age_out_processes(service)
    service.ingest_observation(observation(count=4, processes=mixed_cohort))
    service.ingest_observation(observation(count=4, processes=mixed_cohort))

    lease = next(lease for lease in service.list_leases(admin)["data"] if lease["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    lease_gpus = [
        gpu for gpu in service.list_gpus(admin)["data"] if gpu["id"] in claimed["lease"]["gpu_ids"]
    ]
    assert {gpu["state"] for gpu in lease_gpus} == {"RUNNING_MANAGED"}
    assert {
        gpu["gpu_uuid"]: {process["pid"] for process in gpu["processes"]} for gpu in lease_gpus
    } == {
        gpu_uuids[0]: {4_100, 5_100},
        gpu_uuids[1]: {5_101},
        gpu_uuids[2]: {4_102},
        gpu_uuids[3]: {4_103, 5_103},
    }
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.snapshot(admin)["data"]["alerts"]
    )


def test_task_stays_running_during_overlapping_worker_turnover(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("overlapping-routine-retry", project_id="agent"),
        idempotency_key="overlapping-routine-retry-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    bound_process = process_for_gpu(gpu_uuid, pid=4_150)
    service.ingest_observation(observation(count=1, processes=[bound_process]))
    replacement = process_for_gpu(gpu_uuid, pid=5_150)

    # The old and new workers overlap while a task transitions between
    # execution stages. Process identities are observations, not ownership.
    service.ingest_observation(observation(count=1, processes=[bound_process, replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"

    service.ingest_observation(observation(count=1, processes=[bound_process, replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"
    assert (
        next(
            lease
            for lease in service.list_leases(admin)["data"]
            if lease["id"] == claimed["lease"]["id"]
        )["state"]
        == "ACTIVE"
    )


def test_worker_turnover_then_empty_gpu_keeps_task_lease_active(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("empty-routine-retry-window", project_id="agent"),
        idempotency_key="empty-routine-retry-window-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    initial = process_for_gpu(gpu_uuid, pid=4_175)
    service.ingest_observation(observation(count=1, processes=[initial]))
    replacement = process_for_gpu(gpu_uuid, pid=5_175)
    service.ingest_observation(observation(count=1, processes=[initial, replacement]))
    service.ingest_observation(observation(count=1, processes=[initial, replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"

    age_out_processes(service)
    service.ingest_observation(observation(count=1, processes=[]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "LEASED_IDLE"
    assert gpu["lease"]["id"] == lease_id
    assert gpu["lease"]["state"] == "ACTIVE"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.snapshot(admin)["data"]["alerts"]
    )


def test_workload_lease_process_restart_preserves_task_assignment(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("strict-retry"),
        idempotency_key="strict-retry-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_201)])
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_201)
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))

    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"
    assert (
        next(
            lease
            for lease in service.list_leases(admin)["data"]
            if lease["id"] == claimed["lease"]["id"]
        )["state"]
        == "ACTIVE"
    )


def test_explicit_workload_binding_process_restart_preserves_task_assignment(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("explicit-agent-run", project_id="agent"),
        idempotency_key="explicit-agent-run-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_251)])
    )
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="strict-run-id"),
        idempotency_key="explicit-agent-run-bind",
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_251)
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))

    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"


def test_default_explicit_binding_does_not_block_process_turnover(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("default-explicit-agent-run", project_id="agent"),
        idempotency_key="default-explicit-agent-run-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_261)])
    )
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(),
        idempotency_key="default-explicit-agent-run-bind",
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_261)
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))

    lease = next(
        lease
        for lease in service.list_leases(admin)["data"]
        if lease["id"] == claimed["lease"]["id"]
    )
    assert lease["workloads"][0]["run_id"] == f"explicit:lease:{claimed['lease']['id']}"
    assert lease["state"] == "ACTIVE"


def test_incomplete_process_observation_does_not_change_task_assignment(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("incomplete-routine-retry", project_id="agent"),
        idempotency_key="incomplete-routine-retry-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_301)])
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_301)
    service.ingest_observation(
        observation(
            count=1,
            processes=[replacement],
            observation_complete=False,
        )
    )
    service.ingest_observation(
        observation(
            count=1,
            processes=[replacement],
            observation_complete=False,
        )
    )

    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"
    assert (
        next(
            lease
            for lease in service.list_leases(admin)["data"]
            if lease["id"] == claimed["lease"]["id"]
        )["state"]
        == "ACTIVE"
    )


def test_namespace_hidden_pid_reuse_remains_an_observed_task_process(service, admin) -> None:
    """A reused PID changes process telemetry without changing the task assignment."""

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("namespace-gap-run"),
        idempotency_key="namespace-gap-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    first_seen = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"username": None, "process_started_at": first_seen}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="namespace-gap-run-1"),
        idempotency_key="namespace-gap-bind",
    )

    # One complete observation without the process closes the continuity
    # window. The same PID after that gap must receive a new identity.
    service.ingest_observation(observation(count=1, processes=[]))
    replacement = initial_process.model_copy(
        update={"process_started_at": first_seen + timedelta(seconds=30)}
    )
    service.ingest_observation(observation(count=1, processes=[replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"

    repeated_replacement = replacement.model_copy(
        update={"process_started_at": first_seen + timedelta(seconds=40)}
    )
    service.ingest_observation(observation(count=1, processes=[repeated_replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"


def test_initialize_normalizes_legacy_process_attribution_conflict(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("recover-attribution-run"),
        idempotency_key="recover-attribution-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    started_at = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"process_started_at": started_at}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="recover-attribution-run-1"),
        idempotency_key="recover-attribution-bind-initial",
    )

    lease_id = claimed["lease"]["id"]

    def seed_legacy_conflict(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        session.add(
            Alert(
                id="legacy-process-attribution-conflict",
                alert_type="lease_process_conflict",
                severity="critical",
                resource_type="lease",
                resource_id=lease_id,
                message="legacy PID attribution conflict",
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )

    service._write(seed_legacy_conflict)
    service.initialize()

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["lease"]["state"] == "ACTIVE"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.snapshot(admin)["data"]["alerts"]
    )
    assert any(
        event["action"] == "lease.conflict_resolved"
        and event["resource_id"] == lease_id
        and event["summary"].get("source") == "retired_process_attribution_policy"
        for event in service.list_events(admin, limit=1000)["data"]
    )


def test_endpoint_operator_can_release_empty_conflicted_lease_after_fresh_observation(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("clear-empty-conflict"),
        idempotency_key="clear-empty-conflict-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu_uuid)]))

    def seed_legacy_conflict(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        session.add(
            Alert(
                id="legacy-empty-release-conflict",
                alert_type="lease_process_conflict",
                severity="critical",
                resource_type="lease",
                resource_id=lease_id,
                message="legacy PID attribution conflict",
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )

    service._write(seed_legacy_conflict)

    barrier = utcnow()
    # The card really is empty: the sighting has run out its absence window,
    # and this complete observation is the reading that retires it.
    age_out_processes(service)
    service.ingest_observation(observation(count=1, processes=[]))
    # This path is for a lease whose holder is gone. It now says so: a claim
    # heard from moments ago is protected, so the wedged lease this test is
    # about has to have actually gone quiet.
    age_out_lease_holder(service, lease_id)
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="clear-empty-conflict-release",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    assert service.list_gpus(admin)["data"][0]["state"] == "AVAILABLE"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.snapshot(admin)["data"]["alerts"]
    )


def test_owner_can_release_legacy_conflict_while_process_is_observed(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("keep-conflict"),
        idempotency_key="keep-conflict-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu_uuid)]))

    def seed_legacy_conflict(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        session.add(
            Alert(
                id="legacy-owner-release-conflict",
                alert_type="lease_process_conflict",
                severity="critical",
                resource_type="lease",
                resource_id=lease_id,
                message="legacy PID attribution conflict",
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )

    service._write(seed_legacy_conflict)
    barrier = utcnow() - timedelta(seconds=1)

    with pytest.raises(BrokerError) as error:
        service.release_empty_conflicted_lease(
            admin,
            "endpoint-a",
            lease_id,
            observation_not_before=barrier,
            idempotency_key="keep-conflict-release",
        )

    assert error.value.code == "conflict_process_present"
    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["lease"]["state"] == "CONFLICT"

    released = service.release_lease(
        admin,
        lease_id,
        reason="workload completed",
        idempotency_key="release-legacy-conflict-with-process",
    )
    assert released["lease"]["state"] == "RELEASED"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.snapshot(admin)["data"]["alerts"]
    )
    assert service.list_gpus(admin)["data"][0]["state"] == "BUSY_UNMANAGED"


def test_initialize_resolves_stale_alerts_for_terminal_lease(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("startup-alert-repair"),
        idempotency_key="startup-alert-repair-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    service.release_lease(
        admin,
        lease_id,
        reason="workload completed",
        idempotency_key="startup-alert-repair-release",
    )

    def seed_stale_alerts(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        for alert_type in ("lease_process_conflict", "orphaned_busy"):
            session.add(
                Alert(
                    id=f"startup-{alert_type}",
                    alert_type=alert_type,
                    severity="critical",
                    resource_type="lease",
                    resource_id=lease_id,
                    message="stale test alert",
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                    acknowledged_at=None,
                    acknowledged_by=None,
                )
            )

    service._write(seed_stale_alerts)
    assert {
        alert["type"]
        for alert in service.snapshot(admin)["data"]["alerts"]
        if alert["resource_id"] == lease_id
    } == {"lease_process_conflict", "orphaned_busy"}
    service.initialize()
    assert not any(
        alert["resource_id"] == lease_id for alert in service.snapshot(admin)["data"]["alerts"]
    )


def test_a_collector_round_resolves_alerts_for_a_lease_without_active_resources(
    service, admin
) -> None:
    """An alert about a claim that no longer holds anything is noise.

    The claim is still live, so nothing terminal closes its alerts; only the
    repair that runs on every collector round can, and it is reached through
    ingestion rather than any operator verb.
    """

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("resource-alert-repair"),
        idempotency_key="resource-alert-repair-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]

    def orphan_alerts_from_resources(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        resources = session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease_id)
        ).all()
        assert resources
        for resource in resources:
            resource.active = False
            resource.released_at = now
        for alert_type in ("lease_process_conflict", "orphaned_busy"):
            session.add(
                Alert(
                    id=f"reconcile-{alert_type}",
                    alert_type=alert_type,
                    severity="critical",
                    resource_type="lease",
                    resource_id=lease_id,
                    message="stale test alert",
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                    acknowledged_at=None,
                    acknowledged_by=None,
                )
            )

    service._write(orphan_alerts_from_resources)
    assert {
        alert["type"]
        for alert in service.snapshot(admin)["data"]["alerts"]
        if alert["resource_id"] == lease_id
    } == {"lease_process_conflict", "orphaned_busy"}

    service.ingest_observation(observation(count=1))

    assert not any(
        alert["resource_id"] == lease_id for alert in service.snapshot(admin)["data"]["alerts"]
    )


def test_endpoint_operator_can_release_empty_idle_workload_lease(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("clear-idle-lease"),
        idempotency_key="clear-idle-lease-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    assert service.list_gpus(admin)["data"][0]["state"] == "HELD"

    barrier = utcnow()
    service.ingest_observation(observation(count=1, processes=[]))
    age_out_lease_holder(service, lease_id)
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="clear-idle-lease-release",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    assert service.list_gpus(admin)["data"][0]["state"] == "AVAILABLE"


def test_process_and_stale_telemetry_block_admission(service, admin) -> None:
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    # A compute process blocks immediately; a second sample is only needed to label a lease conflict.
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("process-busy"), idempotency_key="proc-busy")
    assert error.value.code == "no_capacity"
    assert service.list_gpus(admin)["data"][0]["state"] == "BUSY_UNMANAGED"

    def age_telemetry(session) -> None:  # type: ignore[no-untyped-def]
        snapshot = session.scalar(select(TelemetryCurrent))
        assert snapshot is not None
        snapshot.observed_at = utcnow() - timedelta(seconds=1000)

    service._write(age_telemetry)
    assert service.list_gpus(admin)["data"][0]["state"] == "UNKNOWN_STALE"


def test_attention_summary_separates_endpoint_and_gpu_units(service, admin) -> None:
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    service.record_provider_failure("endpoint-b", "timeout")

    snapshot = service.snapshot(admin)["data"]

    assert snapshot["summary"]["abnormal_gpus"] == 0
    assert snapshot["summary"]["attention"] == {
        "endpoint_count": 1,
        "endpoint_status_counts": {"ERROR": 1},
        "gpu_count": 1,
        "gpu_state_counts": {"BUSY_UNMANAGED": 1},
        "unmanaged_gpu_count": 1,
        "total_resource_count": 2,
    }


def test_current_telemetry_is_bounded_and_routine_samples_do_not_audit(service, admin) -> None:
    first = observation(count=3)
    service.ingest_observation(first)
    service.ingest_observation(observation(count=3))

    def counts(session):  # type: ignore[no-untyped-def]
        return (
            len(session.scalars(select(TelemetryCurrent)).all()),
            len(session.scalars(select(TelemetrySnapshot)).all()),
            len(session.scalars(select(AuditEvent)).all()),
        )

    current_count, history_count, audit_count = service._read(counts)
    assert current_count == 3
    assert history_count == 3
    assert audit_count == 0


def test_snapshot_includes_per_gpu_recent_telemetry_average(service, admin) -> None:
    start = utcnow() - timedelta(minutes=9)

    def sample(
        observed_at,
        *,
        memory_used_mib: int,
        gpu_utilization_pct: int,
        memory_utilization_pct: int,
        temperature_c: int,
    ) -> EndpointObservation:
        value = observation(
            count=1,
            observed_at=observed_at,
            host={
                "cpu_count": 64,
                "load_1m": {
                    10_000: 6.4,
                    20_000: 12.8,
                    30_000: 32.0,
                }[memory_used_mib],
                "memory_total_mib": 100_000,
                "memory_available_mib": {
                    10_000: 90_000,
                    20_000: 80_000,
                    30_000: 60_000,
                }[memory_used_mib],
            },
        )
        value.gpus[0] = value.gpus[0].model_copy(
            update={
                "memory_used_mib": memory_used_mib,
                "memory_free_mib": 100_000 - memory_used_mib,
                "gpu_utilization_pct": gpu_utilization_pct,
                "memory_utilization_pct": memory_utilization_pct,
                "temperature_c": temperature_c,
            }
        )
        return value

    service.ingest_observation(
        sample(
            start,
            memory_used_mib=10_000,
            gpu_utilization_pct=10,
            memory_utilization_pct=20,
            temperature_c=40,
        )
    )
    service.ingest_observation(
        sample(
            start + timedelta(seconds=61),
            memory_used_mib=20_000,
            gpu_utilization_pct=40,
            memory_utilization_pct=50,
            temperature_c=50,
        )
    )
    # This newest current sample is not yet another persisted history point,
    # but must still contribute to the average exactly once.
    service.ingest_observation(
        sample(
            start + timedelta(seconds=90),
            memory_used_mib=30_000,
            gpu_utilization_pct=80,
            memory_utilization_pct=90,
            temperature_c=60,
        )
    )

    recent_average = service.snapshot(admin)["data"]["gpus"][0]["telemetry"]["recent_average"]
    assert recent_average == {
        "window_seconds": 600,
        "sample_count": 3,
        "first_observed_at": start.isoformat(),
        "last_observed_at": (start + timedelta(seconds=90)).isoformat(),
        "memory_used_mib": 20_000.0,
        "memory_free_mib": 80_000.0,
        "memory_used_pct": 20.0,
        "gpu_utilization_pct": 43.33,
        "memory_utilization_pct": 53.33,
        "temperature_c": 50.0,
    }
    recent_host_average = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"][
        "recent_average"
    ]
    assert recent_host_average == {
        "window_seconds": 600,
        "sample_count": 3,
        "first_observed_at": start.isoformat(),
        "last_observed_at": (start + timedelta(seconds=90)).isoformat(),
        "cpu_utilization_pct": None,
        "cpu_load_fraction": None,
        "memory_used_pct": 23.33,
    }


def test_cpu_only_observation_is_persisted_as_endpoint_resource_kind(service, admin) -> None:
    service.ingest_observation(
        observation(
            count=0,
            observation_complete=True,
        ).model_copy(update={"gpu_probe_status": "cpu_only"})
    )

    endpoint = service.snapshot(admin)["data"]["endpoints"][0]

    assert endpoint["resource_kind"] == "cpu_only"
    assert endpoint["monitor"] == {
        **endpoint["monitor"],
        "status": "ONLINE",
        "gpu_count": 0,
        "last_error": None,
    }


def test_endpoint_cpu_and_memory_telemetry_is_exposed_in_snapshot(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    endpoint = service.snapshot(admin)["data"]["endpoints"][0]
    assert endpoint["host_telemetry"] == {
        "observed_at": endpoint["host_telemetry"]["observed_at"],
        "collected_at": endpoint["host_telemetry"]["collected_at"],
        "cpu_count": 64,
        "load_1m": 4.0,
        "cpu_utilization_pct": None,
        "cpu_total_ticks": None,
        "cpu_idle_ticks": None,
        "cpu_usage_usec": None,
        "cpu_quota_usec": None,
        "cpu_period_usec": None,
        "memory_total_mib": 262_144,
        "memory_available_mib": 196_608,
        "memory_limit_mib": None,
        "memory_current_mib": None,
        "capacity": {
            "cpu_scope": "host",
            "cpu_cores": 64.0,
            "cpu_available_cores": 60.0,
            "memory_scope": "host",
            "memory_total_mib": 262_144,
            "memory_used_mib": 65_536,
            "memory_available_mib": 196_608,
        },
        "provider": "raw-ssh",
        "recent_average": {
            "window_seconds": 600,
            "sample_count": 1,
            "first_observed_at": endpoint["host_telemetry"]["observed_at"],
            "last_observed_at": endpoint["host_telemetry"]["observed_at"],
            "cpu_utilization_pct": None,
            "cpu_load_fraction": None,
            "memory_used_pct": 25.0,
        },
    }


def test_endpoint_history_is_throttled_and_calculates_cgroup_cpu_utilization(
    service, admin
) -> None:
    start = utcnow() - timedelta(minutes=10)
    first = service.ingest_observation(
        observation(
            count=1,
            observed_at=start,
            host={
                "cpu_usage_usec": 1_000_000_000,
                "cpu_quota_usec": 3_000_000,
                "cpu_period_usec": 100_000,
            },
        )
    )
    second = service.ingest_observation(
        observation(
            count=1,
            observed_at=start + timedelta(seconds=30),
            host={
                "cpu_usage_usec": 1_000_000_000 + 1_500_000 * 30,
                "cpu_quota_usec": 3_000_000,
                "cpu_period_usec": 100_000,
            },
        )
    )
    third = service.ingest_observation(
        observation(
            count=1,
            observed_at=start + timedelta(seconds=61),
            host={
                "cpu_usage_usec": 1_000_000_000 + 1_500_000 * 61,
                "cpu_quota_usec": 3_000_000,
                "cpu_period_usec": 100_000,
            },
        )
    )

    def endpoint_history_count(session):  # type: ignore[no-untyped-def]
        return len(session.scalars(select(EndpointTelemetrySnapshot)).all())

    assert first["endpoint_history_points_written"] == 1
    assert second["endpoint_history_points_written"] == 0
    assert third["endpoint_history_points_written"] == 1
    assert service._read(endpoint_history_count) == 2

    current_host = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]
    assert current_host["cpu_utilization_pct"] == 5.0
    assert current_host["cpu_usage_usec"] == 1_000_000_000 + 1_500_000 * 61
    assert current_host["cpu_quota_usec"] == 3_000_000
    assert current_host["cpu_period_usec"] == 100_000
    assert current_host["recent_average"]["cpu_utilization_pct"] == 5.0
    assert current_host["recent_average"]["cpu_load_fraction"] == 0.05
    history = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)
    assert history["data"]["point_count"] == 2
    assert history["data"]["points"][0]["cpu_utilization_pct"] is None
    assert history["data"]["points"][1]["cpu_utilization_pct"] == 5.0
    assert history["data"]["points"][1]["memory_used_pct"] == 25.0
    gpu_series = history["data"]["gpu_series"]
    assert len(gpu_series) == 1
    assert gpu_series[0]["gpu_id"] == "endpoint-a:GPU-endpoint-a-0"
    assert gpu_series[0]["gpu_uuid"] == "GPU-endpoint-a-0"
    assert gpu_series[0]["gpu_index"] == 0
    assert len(gpu_series[0]["points"]) == 2
    assert gpu_series[0]["points"][1]["gpu_utilization_pct"] == 0.0
    assert gpu_series[0]["points"][1]["memory_used_pct"] == 0.0


def test_host_capacity_reports_the_cgroup_budget_instead_of_the_whole_machine(
    service, admin
) -> None:
    start = utcnow() - timedelta(minutes=10)
    cgroup_host = {
        "cpu_quota_usec": 6_000_000,
        "cpu_period_usec": 100_000,
        "memory_total_mib": 1_029_910,
        "memory_available_mib": 819_837,
        "memory_limit_mib": 491_520,
        "memory_current_mib": 132_337,
    }
    service.ingest_observation(
        observation(
            count=1,
            observed_at=start,
            host={**cgroup_host, "cpu_usage_usec": 1_000_000_000},
        )
    )
    service.ingest_observation(
        observation(
            count=1,
            observed_at=start + timedelta(seconds=60),
            # 30 of the 60 quota cores busy over the interval.
            host={**cgroup_host, "cpu_usage_usec": 1_000_000_000 + 30_000_000 * 60},
        )
    )

    host = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]

    assert host["cpu_count"] == 64
    assert host["capacity"] == {
        "cpu_scope": "container",
        "cpu_cores": 60.0,
        "cpu_available_cores": 30.0,
        "memory_scope": "container",
        "memory_total_mib": 491_520,
        "memory_used_mib": 132_337,
        "memory_available_mib": 359_183,
    }


def test_admission_sizes_a_container_endpoint_by_its_cgroup_budget(service, admin) -> None:
    """The allocator admits against the same figures the interface shows.

    The node has 64 cores and about 1 TiB; this endpoint owns 60 cores and
    480 GiB of it.  A request that only fits the node must not be admitted.
    """

    now = utcnow()
    cgroup_host = {
        "cpu_quota_usec": 6_000_000,
        "cpu_period_usec": 100_000,
        "memory_total_mib": 1_029_910,
        "memory_available_mib": 819_837,
        "memory_limit_mib": 491_520,
        "memory_current_mib": 132_337,
    }
    service.ingest_observation(
        observation(
            count=1,
            observed_at=now - timedelta(seconds=60),
            host={**cgroup_host, "cpu_usage_usec": 1_000_000_000},
        )
    )
    # The latest observation stays fresh so the cards remain allocatable; only
    # the CPU share is at issue here.
    service.ingest_observation(
        observation(
            count=1,
            observed_at=now,
            # 30 of the 60 quota cores busy over the interval.
            host={**cgroup_host, "cpu_usage_usec": 1_000_000_000 + 30_000_000 * 60},
        )
    )

    capacity = service.snapshot(admin)["data"]["host_capacity"][0]["capacity"]
    assert capacity["total_cpu_cores"] == 60.0
    assert capacity["observed_available_cpu_cores"] == 30.0
    assert capacity["total_memory_mib"] == 491_520
    assert capacity["observed_available_memory_mib"] == 359_183

    # 40 free cores exist on the node but not in this endpoint's share.
    beyond_share = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "beyond-container-share",
            "purpose": "ask for more free cores than the cgroup budget leaves",
            "constraints": {"gpu_count": 1, "min_available_cpu_cores": 40},
        }
    )
    with pytest.raises(BrokerError) as cpu_error:
        service.create_request(admin, beyond_share, idempotency_key="beyond-container-cpu")
    assert cpu_error.value.code == "no_capacity"

    beyond_memory = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "beyond-container-memory",
            "purpose": "ask for more free memory than the cgroup budget leaves",
            "constraints": {"gpu_count": 1, "min_available_memory_mib": 400 * 1024},
        }
    )
    with pytest.raises(BrokerError) as memory_error:
        service.create_request(admin, beyond_memory, idempotency_key="beyond-container-memory")
    assert memory_error.value.code == "no_capacity"

    within_share = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "within-container-share",
            "purpose": "ask for resources the cgroup budget actually leaves free",
            "constraints": {
                "gpu_count": 1,
                "min_available_cpu_cores": 25,
                "min_available_memory_mib": 300 * 1024,
            },
        }
    )
    allocated = service.create_request(admin, within_share, idempotency_key="within-container")
    assert allocated["lease"] is not None


def test_host_memory_used_pct_prefers_cgroup_limit_over_host_memtotal(service, admin) -> None:
    service.ingest_observation(
        observation(
            count=1,
            host={
                "memory_total_mib": 1_029_120,
                "memory_available_mib": 921_600,
                "memory_limit_mib": 249_856,
                "memory_current_mib": 51_200,
            },
        )
    )

    host = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]
    assert host["memory_total_mib"] == 1_029_120
    assert host["memory_limit_mib"] == 249_856
    assert host["memory_current_mib"] == 51_200
    assert host["recent_average"]["memory_used_pct"] == 20.49


def test_host_memory_used_pct_falls_back_to_host_when_cgroup_unlimited(service, admin) -> None:
    service.ingest_observation(
        observation(
            count=1,
            host={
                "memory_total_mib": 1_029_120,
                "memory_available_mib": 921_600,
                "memory_limit_mib": None,
                "memory_current_mib": 51_200,
            },
        )
    )

    host = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]
    assert host["memory_limit_mib"] is None
    assert host["memory_current_mib"] == 51_200
    assert host["recent_average"]["memory_used_pct"] == 10.45


def test_endpoint_cpu_utilization_stays_null_without_cgroup(service, admin) -> None:
    start = utcnow() - timedelta(minutes=10)
    service.ingest_observation(
        observation(
            count=1,
            observed_at=start,
            host={
                "cpu_count": 128,
                "load_1m": 380.0,
                "cpu_total_ticks": 1_000,
                "cpu_idle_ticks": 700,
            },
        )
    )
    service.ingest_observation(
        observation(
            count=1,
            observed_at=start + timedelta(seconds=61),
            host={
                "cpu_count": 128,
                "load_1m": 380.0,
                "cpu_total_ticks": 1_200,
                "cpu_idle_ticks": 810,
            },
        )
    )

    current_host = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]
    assert current_host["cpu_utilization_pct"] is None
    assert current_host["cpu_usage_usec"] is None
    assert current_host["recent_average"]["cpu_utilization_pct"] is None
    assert current_host["recent_average"]["cpu_load_fraction"] is None
    history = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)
    assert history["data"]["point_count"] == 2
    assert [point["cpu_utilization_pct"] for point in history["data"]["points"]] == [None, None]


def test_endpoint_history_validates_window_points_and_identity(service, admin) -> None:
    service.ingest_observation(observation(count=1))

    assert (
        service.endpoint_history(admin, "endpoint-a", window_seconds=21_600)["data"][
            "window_seconds"
        ]
        == 21_600
    )
    with pytest.raises(BrokerError) as bad_window:
        service.endpoint_history(admin, "endpoint-a", window_seconds=300)
    with pytest.raises(BrokerError) as bad_points:
        service.endpoint_history(admin, "endpoint-a", max_points=121)
    with pytest.raises(BrokerError) as missing_endpoint:
        service.endpoint_history(admin, "missing", window_seconds=3600)

    assert bad_window.value.code == "invalid_history_window"
    assert bad_points.value.code == "invalid_history_points"
    assert missing_endpoint.value.code == "endpoint_not_found"


def test_endpoint_history_excludes_current_outside_requested_window(service, admin) -> None:
    service.ingest_observation(observation(count=1, observed_at=utcnow() - timedelta(hours=2)))

    history = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)

    assert history["data"]["point_count"] == 0


def test_endpoint_history_is_downsampled_to_requested_cap(service, admin) -> None:
    service.ingest_observation(observation(count=1))

    def seed_history(session) -> None:  # type: ignore[no-untyped-def]
        start = utcnow() - timedelta(hours=3)
        for index in range(130):
            session.add(
                EndpointTelemetrySnapshot(
                    endpoint_id="endpoint-a",
                    observed_at=start + timedelta(minutes=index),
                    collected_at=start + timedelta(minutes=index),
                    cpu_count=64,
                    load_1m=float(index % 10),
                    memory_total_mib=262_144,
                    memory_available_mib=196_608,
                )
            )

    service._write(seed_history)
    history = service.endpoint_history(admin, "endpoint-a", window_seconds=21_600, max_points=120)
    assert history["data"]["point_count"] == 120


def test_provider_audit_is_written_only_on_failure_and_recovery_transitions(service) -> None:
    service.record_provider_failure("endpoint-a", "timeout")
    service.record_provider_failure("endpoint-a", "timeout")
    service.ingest_observation(observation(count=1))
    service.ingest_observation(observation(count=1))

    def actions(session):  # type: ignore[no-untyped-def]
        return [
            event.action for event in session.scalars(select(AuditEvent).order_by(AuditEvent.id))
        ]

    assert service._read(actions) == ["telemetry.failed", "telemetry.recovered"]


def test_human_monitoring_sees_endpoint_failures_and_can_read_latest_events(service) -> None:
    service.record_provider_failure("endpoint-a", "timeout")
    service.record_provider_failure("endpoint-b", "connection refused")

    human_events = service.list_events(service.local_actor("human"))["data"]
    assert [event["action"] for event in human_events] == [
        "telemetry.failed",
        "telemetry.failed",
    ]
    latest = service.list_events(service.local_actor("human"), latest_first=True, limit=1)["data"]
    assert latest[0]["resource_id"] == "endpoint-b"


def test_expired_lease_with_process_becomes_orphan_and_stays_blocked(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("will-orphan"), idempotency_key="orphan")
    assert allocated["lease"] is not None
    lease_id = allocated["lease"]["id"]

    def expire(session) -> None:  # type: ignore[no-untyped-def]
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = utcnow() - timedelta(seconds=1)

    service._write(expire)
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ORPHANED_BUSY"
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin, request_data("must-not-reuse"), idempotency_key="blocked-orphan"
        )
    assert error.value.code == "no_capacity"


def test_allocator_can_claim_an_unregistered_project_without_login_token(service, admin) -> None:
    agent = service.local_actor("story-agent")
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        agent,
        request_data("unregistered-project", project_id="storyboard"),
        idempotency_key="unregistered-project",
    )
    assert claimed["request"]["project_id"] == "storyboard"
    assert any(
        item["id"] == claimed["request"]["id"] for item in service.list_requests(agent)["data"]
    )
    assert "tokens" not in str(service.snapshot(admin))


def test_one_hundred_concurrent_requests_never_double_lease(service, admin) -> None:
    service.ingest_observation(observation(count=4))

    def submit(index: int):  # type: ignore[no-untyped-def]
        try:
            return service.create_request(
                admin,
                request_data(f"concurrent-{index}"),
                idempotency_key=f"concurrent-{index}",
            )
        except BrokerError as error:
            assert error.code == "no_capacity"
            return None

    results = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(submit, index) for index in range(100)]
        for future in as_completed(futures):
            results.append(future.result())
    leases = [result["lease"] for result in results if result is not None]
    gpu_ids = [gpu_id for lease in leases for gpu_id in lease["gpu_ids"]]
    assert len(gpu_ids) == len(set(gpu_ids)) == 4
    assert all(result["request"]["state"] == "LEASED" for result in results if result is not None)


def test_database_unique_index_rejects_duplicate_active_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    first = service.create_request(admin, request_data("first"), idempotency_key="first")
    assert first["lease"] is not None
    gpu_id = first["lease"]["gpu_ids"][0]
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("second"), idempotency_key="second")
    assert error.value.code == "no_capacity"

    def illegal_duplicate(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        request = AllocationRequest(
            id="illegal-request",
            actor_id=admin.id,
            project_id="project-a",
            auto_activate=False,
            task_ref="illegal-duplicate",
            purpose="verify active GPU uniqueness",
            constraints_json="{}",
            duration_seconds=3600,
            expected_duration_seconds=None,
            start_after=None,
            deadline=None,
            approval_ref=None,
            state="LEASED",
            priority_class="normal",
            blocked_reason=None,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        session.flush()
        lease = Lease(
            id="illegal",
            request_id=request.id,
            actor_id=admin.id,
            project_id="project-a",
            state="HELD",
            issued_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
            last_heartbeat_at=utcnow(),
            issued_revision=1,
        )
        session.add(lease)
        session.flush()
        session.add(LeaseResource(lease_id=lease.id, gpu_id=gpu_id, active=True))
        session.flush()

    with pytest.raises(IntegrityError):
        service._write(illegal_duplicate)


def test_cooperative_actor_labels_are_not_admin_and_lease_ownership_is_exact(service) -> None:
    service.ingest_observation(observation(count=1))
    owner = service.local_actor("lease-owner")
    other = service.local_actor("lease-other")
    assert owner.role == "allocator"
    claimed = service.create_request(
        owner, request_data("owner-only"), idempotency_key="owner-only"
    )
    assert claimed["lease"] is not None
    with pytest.raises(BrokerError) as forbidden:
        service.release_lease(
            other,
            claimed["lease"]["id"],
            reason="not the owner",
            idempotency_key="other-release",
        )
    assert forbidden.value.code == "lease_forbidden"

    with pytest.raises(BrokerError) as override_forbidden:
        service.release_lease(
            other,
            claimed["lease"]["id"],
            reason="not a human operator",
            idempotency_key="other-override-release",
            operator_override=True,
        )
    assert override_forbidden.value.code == "operator_role_required"
    assert service.list_leases(other)["data"] == []
    assert service.list_requests(other)["data"] == []


def test_direct_lease_returns_executable_resources_and_accounts_endpoint_commitments(
    service, admin
) -> None:
    service.ingest_observation(observation(count=2))
    first = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "commitment-one",
                "purpose": "per endpoint commitment",
                "constraints": {"gpu_count": 1, "cpu_cores": 40, "memory_mib": 200_000},
            }
        ),
        idempotency_key="commitment-one",
    )
    assert first["lease"] is not None
    resource = first["lease"]["resources"][0]
    assert resource["endpoint"] == {
        "id": "endpoint-a",
        "host": "127.0.0.1",
        "port": 2201,
        "ssh_user": "gpu",
        "workspace_path": "/srv/project-a",
    }
    assert resource["gpus"][0]["gpu_uuid"].startswith("GPU-")
    assert resource["cuda_visible_devices"] == str(resource["gpus"][0]["cuda_ordinal"])
    assert resource["cuda_device_order"] == "PCI_BUS_ID"
    assert resource["commitment"] == {"cpu_cores": 40.0, "memory_mib": 200_000}
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            RequestCreate.model_validate(
                {
                    "project_id": "project-a",
                    "task_ref": "commitment-two",
                    "purpose": "must not overcommit endpoint",
                    "constraints": {"gpu_count": 1, "cpu_cores": 40, "memory_mib": 200_000},
                }
            ),
            idempotency_key="commitment-two",
        )
    assert error.value.code == "no_capacity"


def test_gpu_without_current_cuda_ordinal_is_not_allocated(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    with service.database.session() as session:
        gpu = session.get(GPUDevice, "endpoint-a:GPU-endpoint-a-0")
        assert gpu is not None
        gpu.cuda_ordinal = None
        session.commit()

    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            request_data("missing-cuda-selector"),
            idempotency_key="missing-cuda-selector",
        )

    assert error.value.code == "no_capacity"


def test_human_can_reassign_a_task_to_an_exact_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("manual-gpu-move"),
        idempotency_key="manual-gpu-move-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    assert lease["gpu_ids"] == ["endpoint-a:GPU-endpoint-a-0"]

    moved = service.reassign_lease_gpus(
        admin,
        lease["id"],
        ["endpoint-a:GPU-endpoint-a-1"],
        idempotency_key="manual-gpu-move-apply",
    )

    assert moved["restart_required"] is True
    assert moved["lease"]["gpu_ids"] == ["endpoint-a:GPU-endpoint-a-1"]
    assert moved["lease"]["resources"][0]["cuda_visible_devices"] == "1"
    assert moved["lease"]["resources"][0]["cuda_device_order"] == "PCI_BUS_ID"
    gpus = {gpu["id"]: gpu for gpu in service.list_gpus(admin)["data"]}
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["lease"]["id"] == lease["id"]


def test_lease_gpu_reassignment_requires_the_lease_owner(service) -> None:
    service.ingest_observation(observation(count=2))
    owner = service.local_actor("reassignment-owner")
    other = service.local_actor("reassignment-other")
    claimed = service.create_request(
        owner,
        request_data("owner-reassignment-only"),
        idempotency_key="owner-reassignment-only-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    target_gpu_id = "endpoint-a:GPU-endpoint-a-1"

    with pytest.raises(BrokerError) as planning_error:
        service.keepalive_reclaim_request_for_reassignment(
            other,
            lease["id"],
            [target_gpu_id],
        )
    assert planning_error.value.code == "lease_forbidden"

    with pytest.raises(BrokerError) as mutation_error:
        service.reassign_lease_gpus(
            other,
            lease["id"],
            [target_gpu_id],
            idempotency_key="foreign-reassignment",
        )
    assert mutation_error.value.code == "lease_forbidden"
    current = service.list_leases(owner)["data"][0]
    assert current["gpu_ids"] == ["endpoint-a:GPU-endpoint-a-0"]


def test_operator_override_can_plan_and_reassign_a_foreign_lease(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    owner = service.local_actor("operator-correction-owner")
    allocator = service.local_actor("operator-correction-allocator")
    claimed = service.create_request(
        owner,
        request_data("operator-correction"),
        idempotency_key="operator-correction-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    target_gpu_id = "endpoint-a:GPU-endpoint-a-1"

    with pytest.raises(BrokerError) as role_error:
        service.reassign_lease_gpus(
            allocator,
            lease["id"],
            [target_gpu_id],
            idempotency_key="allocator-override-rejected",
            operator_override=True,
        )
    assert role_error.value.code == "operator_role_required"

    reclaim_request = service.keepalive_reclaim_request_for_reassignment(
        admin,
        lease["id"],
        [target_gpu_id],
        operator_override=True,
    )
    assert reclaim_request is None

    moved = service.reassign_lease_gpus(
        admin,
        lease["id"],
        [target_gpu_id],
        idempotency_key="operator-override-reassignment",
        operator_override=True,
    )
    assert moved["lease"]["gpu_ids"] == [target_gpu_id]


def test_reassigned_active_task_auto_binds_process_on_its_new_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("running-gpu-move"),
        idempotency_key="running-gpu-move-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4201)])
    )
    assert service.list_leases(admin)["data"][0]["state"] == "ACTIVE"

    moved = service.reassign_lease_gpus(
        admin,
        lease_id,
        ["endpoint-a:GPU-endpoint-a-1"],
        idempotency_key="running-gpu-move-apply",
    )
    assert moved["lease"]["state"] == "ACTIVE"
    assert moved["lease"]["workloads"] == []

    age_out_processes(service)
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-1", pid=4202)])
    )

    lease = service.list_leases(admin)["data"][0]
    assert lease["state"] == "ACTIVE"
    assert lease["workloads"][0]["run_id"] == f"collector:lease:{lease_id}"
    snapshot_lease = service.snapshot(admin)["data"]["leases"][0]
    assert snapshot_lease["runtime_state"] == "RUNNING"
    gpus = {gpu["id"]: gpu for gpu in service.list_gpus(admin)["data"]}
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "RUNNING_MANAGED"


def _lease_idle_since(service, lease_id: str):  # type: ignore[no-untyped-def]
    def read(session):  # type: ignore[no-untyped-def]
        lease = session.get(Lease, lease_id)
        assert lease is not None
        return lease.idle_since

    return service._read(read)


def _lease_heartbeat_at(service, lease_id: str):  # type: ignore[no-untyped-def]
    def read(session):  # type: ignore[no-untyped-def]
        lease = session.get(Lease, lease_id)
        assert lease is not None
        return lease.last_heartbeat_at

    return service._read(read)


def _make_persistent(service, lease_id: str) -> None:  # type: ignore[no-untyped-def]
    """Match a routine claim, which is created with no expiry at all."""

    def write(session):  # type: ignore[no-untyped-def]
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = None

    service._write(write)


def _backdate_idle_since(service, lease_id: str, seconds: int, *, gpu_ids=None) -> None:  # type: ignore[no-untyped-def]
    """Age the observed idle streak, which lives per leased GPU.

    Settling a whole claim now needs both facts: its cards ran nothing, and its
    holder stopped asking about its own hold. A helper that aged only the
    streak would be describing a claim whose agent is still there.
    """

    def write(session):  # type: ignore[no-untyped-def]
        stamp = utcnow() - timedelta(seconds=seconds)
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.idle_since = stamp
        lease.last_heartbeat_at = stamp
        for resource in session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
            )
        ).all():
            if gpu_ids is None or resource.gpu_id in gpu_ids:
                resource.idle_since = stamp

    service._write(write)


def test_idle_workload_lease_is_warned_then_reclaimed_without_a_process(service, admin) -> None:
    """A persistent claim that never runs must not hold GPUs forever.

    Routine claims carry no expiry, so before this path nothing ever returned
    their GPUs when an agent forgot to release.
    """

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("idle-claim"), idempotency_key="idle")
    assert allocated["lease"] is not None
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)

    # First idle observation only starts the clock.
    service.ingest_observation(observation(count=1))
    assert _lease_idle_since(service, lease_id) is not None
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] in {"HELD", "ACTIVE"}

    # Past the alert window the lease is flagged but still owns its GPU.
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_alert_seconds + 5)
    service.ingest_observation(observation(count=1))
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] in {"HELD", "ACTIVE"}
    alerts = service.snapshot(admin)["data"]["alerts"]
    idle_alerts = [item for item in alerts if item["type"] == "idle_lease"]
    assert len(idle_alerts) == 1
    assert idle_alerts[0]["resource_id"] == lease_id

    # Past the reclaim window the GPU comes back to the allocatable pool.
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(observation(count=1))
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "EXPIRED_EMPTY"
    assert lease["release_reason"] == "idle without observed process"
    assert not [
        item
        for item in service.snapshot(admin)["data"]["alerts"]
        if item["type"] == "idle_lease" and item["active"]
    ]
    reclaimed = service.create_request(
        admin, request_data("after-reclaim"), idempotency_key="after"
    )
    assert reclaimed["lease"] is not None


def test_idle_clock_resets_when_a_process_appears(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("busy-claim"), idempotency_key="busy")
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)

    service.ingest_observation(observation(count=1))
    assert _lease_idle_since(service, lease_id) is not None

    # A real workload starting must clear the streak so it is never reclaimed.
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    assert _lease_idle_since(service, lease_id) is None
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] in {"HELD", "ACTIVE", "RUNNING_MANAGED"}
    assert lease["state"] != "EXPIRED_EMPTY"


def test_gpu_public_status_separates_an_idle_claim_from_a_running_task() -> None:
    """DESIGN_SYSTEM 5 requires these states to read differently.

    Collapsing them hides exactly the case a human needs to spot: a GPU that is
    claimed but running nothing.
    """

    def project(state: str, lease: object | None = None) -> str:
        return BrokerService._gpu_public_projection(
            {"state": state, "lease": lease, "keepalive": {}},
            monitor_status="ONLINE",
        )["public_status"]

    assert project("LEASED_IDLE", lease={"id": "lease-a"}) == "占卡"
    assert project("HELD", lease={"id": "lease-a"}) == "占卡"
    assert project("RUNNING_MANAGED", lease={"id": "lease-a"}) == "任务占用"
    assert project("BUSY_UNMANAGED") == "未归属占用"
    assert project("ORPHANED_BUSY", lease={"id": "lease-a"}) == "未归属占用"
    assert project("CONFLICT", lease={"id": "lease-a"}) == "归属冲突"


def test_stale_telemetry_resets_the_idle_clock_instead_of_reclaiming(service, admin) -> None:
    """A collector outage is not evidence of an idle workload.

    Without this reset a long outage would accumulate into a reclaim and take
    GPUs away from a job the broker simply could not see. The outage is one
    endpoint going quiet while collection elsewhere keeps running, which is
    what makes the reconciliation on another endpoint's round the live path.
    """

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("unseen"), idempotency_key="unseen")
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(observation(count=1))
    assert _lease_idle_since(service, lease_id) is not None
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)

    stale_cutoff = utcnow() - timedelta(
        seconds=service.inventory.collector.stale_after_seconds * 10
    )

    def age_telemetry(session):  # type: ignore[no-untyped-def]
        for row in session.scalars(select(TelemetryCurrent)).all():
            row.observed_at = stale_cutoff

    service._write(age_telemetry)
    # endpoint-a has gone quiet; the round that still reports is endpoint-b's,
    # and it reconciles every lease including this one.
    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] != "EXPIRED_EMPTY"
    assert _lease_idle_since(service, lease_id) is None


def test_a_declared_duration_no_longer_exempts_a_silent_claim(service, admin) -> None:
    """Being evidenced alive replaced the declared-duration exemption.

    The exemption was meant to protect a job sitting at zero GPU processes
    through a long CPU phase, but a routine claim clears ``expires_at`` by
    construction and so could never reach it: it covered every lease except the
    ones that needed it. One criterion now covers both.
    """

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("timed"), idempotency_key="timed")
    lease_id = allocated["lease"]["id"]
    assert allocated["lease"]["expires_at"] is not None

    service.ingest_observation(observation(count=1))
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(observation(count=1))

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "EXPIRED_EMPTY"


def test_idle_reclaim_keeps_a_claim_whose_holder_still_checks_in(service, admin) -> None:
    """A staged job between two batches must not be settled as abandoned.

    Every card of a claim runs nothing for the minutes between two shards. The
    holder asking about its own hold is what separates that from a claim nobody
    is coming back to.
    """

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("staged"), idempotency_key="staged")
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(observation(count=1))
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)

    # This is all the heartbeat is: the holder asking about its own hold.
    assert service.record_lease_heartbeat(admin, lease_id)["recorded"] is True
    service.ingest_observation(observation(count=1))

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] != "EXPIRED_EMPTY"
    idle_alerts = [
        item
        for item in service.snapshot(admin)["data"]["alerts"]
        if item["type"] == "idle_lease" and item["active"]
    ]
    assert len(idle_alerts) == 1, "a human still has to be able to see the idle claim"


def test_a_live_holder_keeps_its_spare_cards_when_the_streaks_disagree(service, admin) -> None:
    """The gate is the holder, not "every card matured in the same pass".

    A staged job that used four of its eight cards in phase one, or a claim one
    of whose cards blipped out of a collection, reaches the reclaim window on
    some cards and not others. Nothing is running anywhere -- that is the shape
    the whole liveness primitive exists for -- so a gate that only fired when
    all cards matured together would hand this claim's cards to keepalive while
    it is still checking in.
    """

    service.ingest_observation(observation(count=2))
    allocated = service.create_request(
        admin, request_data("desynced", count=2), idempotency_key="desynced"
    )
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(observation(count=2))
    # Only the first card's streak is old enough to be reclaimed; no card runs
    # anything, and the holder is asking about its own hold right now.
    _backdate_idle_since(
        service,
        lease_id,
        service.inventory.idle_lease_reclaim_seconds + 5,
        gpu_ids={"endpoint-a:GPU-endpoint-a-0"},
    )
    assert service.record_lease_heartbeat(admin, lease_id)["recorded"] is True
    service.ingest_observation(observation(count=2))

    def active_gpu_ids(session):  # type: ignore[no-untyped-def]
        return {
            resource.gpu_id
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
                )
            ).all()
        }

    assert service._read(active_gpu_ids) == {
        "endpoint-a:GPU-endpoint-a-0",
        "endpoint-a:GPU-endpoint-a-1",
    }


def test_one_absent_sample_does_not_make_a_running_claim_reclaimable(service, admin) -> None:
    """The p8908 shape: a blip used to mark the process row inactive at once.

    A complete observation that did not contain a process flipped its row to
    inactive with no repeat requirement, so the card read empty from that
    instant. The row's last sighting is still moments old, and that is what
    says the holder is there.
    """

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("blip"), idempotency_key="blip")
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    # The holder has not called in for longer than the reclaim window, and the
    # streak is aged past it too; only the process sighting is fresh.
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(observation(count=1))

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] != "EXPIRED_EMPTY"


def test_a_status_read_of_a_lease_that_is_gone_answers_without_recording(service, admin) -> None:
    """A heartbeat rides on a status read, so it answers instead of failing."""

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("beat"), idempotency_key="beat")
    lease_id = allocated["lease"]["id"]

    before = _lease_heartbeat_at(service, lease_id)
    recorded = service.record_lease_heartbeat(admin, lease_id)
    assert recorded["recorded"] is True
    assert _lease_heartbeat_at(service, lease_id) >= before

    assert service.record_lease_heartbeat(admin, "lease-that-never-existed") == {
        "lease_id": "lease-that-never-existed",
        "recorded": False,
    }

    service.release_lease(admin, lease_id, reason="done", idempotency_key="beat-release")
    assert service.record_lease_heartbeat(admin, lease_id) == {
        "lease_id": lease_id,
        "recorded": False,
    }


def test_legacy_conflict_repair_does_not_forge_a_holder_heartbeat(service, admin) -> None:
    """Normalising a stored state observed nothing about who holds the cards.

    The repair runs on every collector round, so writing a heartbeat there
    would have kept a lease looking alive forever without anybody being there.
    """

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("legacy"), idempotency_key="legacy")
    lease_id = allocated["lease"]["id"]

    def seed_conflict(session) -> None:  # type: ignore[no-untyped-def]
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        lease.last_heartbeat_at = utcnow() - timedelta(hours=6)

    service._write(seed_conflict)
    stored = _lease_heartbeat_at(service, lease_id)

    service.ingest_observation(observation(count=1))

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    assert _lease_heartbeat_at(service, lease_id) == stored


def test_idle_reclaim_returns_only_the_unused_gpus_of_a_working_claim(service, admin) -> None:
    """A claim that keeps eight cards and uses one must return the rest.

    Whole-lease granularity let a single running process protect every other
    GPU in the same claim, which is the most common way capacity is hoarded.
    """

    service.ingest_observation(observation(count=2))
    allocated = service.create_request(
        admin, request_data("partly-used", count=2), idempotency_key="partly-used"
    )
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    busy_gpu, idle_gpu = "GPU-endpoint-a-0", "GPU-endpoint-a-1"
    busy_id, idle_id = f"endpoint-a:{busy_gpu}", f"endpoint-a:{idle_gpu}"

    # One card runs real work; the other never does.
    service.ingest_observation(observation(count=2, processes=[process_for_gpu(busy_gpu)]))
    _backdate_idle_since(
        service,
        lease_id,
        service.inventory.idle_lease_reclaim_seconds + 5,
        gpu_ids={idle_id},
    )
    service.ingest_observation(observation(count=2, processes=[process_for_gpu(busy_gpu)]))

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] != "EXPIRED_EMPTY", "the working GPU must keep the claim alive"

    def active_gpu_ids(session):  # type: ignore[no-untyped-def]
        return {
            resource.gpu_id
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
                )
            ).all()
        }

    remaining = service._read(active_gpu_ids)
    assert remaining == {busy_id}, f"only the idle GPU should be returned, got {remaining}"

    # The returned GPU is allocatable again while the claim keeps working.
    reused = service.create_request(admin, request_data("reuse-idle"), idempotency_key="reuse")
    assert reused["lease"] is not None


def test_idle_reclaim_keeps_a_gpu_whose_process_appears_before_the_window(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    allocated = service.create_request(
        admin, request_data("late-start", count=2), idempotency_key="late-start"
    )
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    late_gpu = "GPU-endpoint-a-1"
    late_id = f"endpoint-a:{late_gpu}"

    service.ingest_observation(observation(count=2))
    _backdate_idle_since(
        service, lease_id, service.inventory.idle_lease_alert_seconds + 5, gpu_ids={late_id}
    )
    # The workload finally starts on that card before the reclaim window.
    service.ingest_observation(observation(count=2, processes=[process_for_gpu(late_gpu)]))

    def idle_marks(session):  # type: ignore[no-untyped-def]
        return {
            resource.gpu_id: resource.idle_since
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
                )
            ).all()
        }

    marks = service._read(idle_marks)
    assert late_id in marks, "the GPU that started working must stay in the claim"
    assert marks[late_id] is None, "its idle streak must be cleared"


def _active_alerts(service) -> set[tuple[str, str]]:  # noqa: ANN001
    with service.database.session() as session:
        return {
            (alert.alert_type, alert.resource_id)
            for alert in session.scalars(select(Alert).where(Alert.active.is_(True))).all()
        }


def test_alerts_do_not_outlive_the_lease_or_server_they_describe(service, admin) -> None:
    """An alert about something that no longer exists is noise, not a warning.

    Nothing used to close a released lease's idle warning or a deleted
    server's collector warning, so the state page filled with warnings about
    resources that were gone and the real ones stopped standing out.
    """

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    # The alert waits for the same staleness criterion the monitor status
    # uses: a single failed probe right after a fresh observation must not
    # raise it, so age the last success before failing the probe.
    with service.database.session() as session:
        state = session.scalar(
            select(ProviderState).where(ProviderState.endpoint_id == "endpoint-b")
        )
        assert state is not None
        state.last_success_at = utcnow() - timedelta(
            seconds=service.inventory.collector.stale_after_seconds + 60
        )
        session.commit()
    service.record_provider_failure("endpoint-b", "CollectionError: timed out")
    assert ("collector_unreachable", "endpoint-b") in _active_alerts(service)

    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "alert-lifetime",
                "purpose": "an idle lease raises a warning",
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
            }
        ),
        idempotency_key="alert-lifetime-claim",
    )
    lease_id = claimed["lease"]["id"]
    with service.database.session() as session:
        service._upsert_alert(
            session,
            alert_type="idle_lease",
            severity="warning",
            resource_type="lease",
            resource_id=lease_id,
            message="idle for over 10 minutes",
            now=utcnow(),
        )
        session.commit()
    assert ("idle_lease", lease_id) in _active_alerts(service)

    service.release_lease(
        admin, lease_id, reason="finished", idempotency_key="alert-lifetime-release"
    )
    assert ("idle_lease", lease_id) not in _active_alerts(service)

    service.delete_endpoint(admin, "endpoint-b", idempotency_key="alert-lifetime-delete")
    assert _active_alerts(service) == set()


def _endpoint_monitor(service, endpoint_id: str) -> dict[str, object]:  # noqa: ANN001
    snapshot = service.snapshot(service.local_actor("human"))["data"]
    endpoint = next(item for item in snapshot["endpoints"] if item["id"] == endpoint_id)
    return endpoint["monitor"]


def test_single_failed_probe_within_stale_window_stays_online(service, admin) -> None:
    """One dropped probe is not proof the host is gone.

    GPU eligibility already keys off telemetry age (`_eligible_gpus`), not
    `provider_state.last_error`. The endpoint monitor status must use the
    same clock, or a server reads as unreachable while its cards still
    allocate just fine.
    """

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    service.record_provider_failure("endpoint-b", "TimeoutError: SSH observation timed out")

    monitor = _endpoint_monitor(service, "endpoint-b")
    assert monitor["status"] == "ONLINE"
    assert monitor["last_error"] == "TimeoutError: SSH observation timed out"
    assert ("collector_unreachable", "endpoint-b") not in _active_alerts(service)


def test_stale_last_success_with_error_reports_error_and_alerts(service, admin) -> None:
    """Once the last successful observation is itself stale, the failure is believed."""

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    with service.database.session() as session:
        state = session.scalar(
            select(ProviderState).where(ProviderState.endpoint_id == "endpoint-b")
        )
        assert state is not None
        state.last_success_at = utcnow() - timedelta(
            seconds=service.inventory.collector.stale_after_seconds + 60
        )
        session.commit()

    service.record_provider_failure("endpoint-b", "TimeoutError: SSH observation timed out")

    monitor = _endpoint_monitor(service, "endpoint-b")
    assert monitor["status"] == "ERROR"
    assert ("collector_unreachable", "endpoint-b") in _active_alerts(service)


def test_recovering_observation_clears_error_status_and_alert(service, admin) -> None:
    """A complete observation is what proves the host is back, and only that."""

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    with service.database.session() as session:
        state = session.scalar(
            select(ProviderState).where(ProviderState.endpoint_id == "endpoint-b")
        )
        assert state is not None
        state.last_success_at = utcnow() - timedelta(
            seconds=service.inventory.collector.stale_after_seconds + 60
        )
        session.commit()
    service.record_provider_failure("endpoint-b", "TimeoutError: SSH observation timed out")
    assert _endpoint_monitor(service, "endpoint-b")["status"] == "ERROR"
    assert ("collector_unreachable", "endpoint-b") in _active_alerts(service)

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))

    monitor = _endpoint_monitor(service, "endpoint-b")
    assert monitor["status"] == "ONLINE"
    assert monitor["last_error"] is None
    assert ("collector_unreachable", "endpoint-b") not in _active_alerts(service)


def test_gpus_stay_allocatable_through_a_single_probe_jitter(service, admin) -> None:
    """A dropped probe must not take a server's cards off the table.

    `_eligible_gpus` was already clock-consistent with telemetry age; this
    pins that the endpoint-level monitor status no longer disagrees with it
    for the duration of the claim.
    """

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    service.record_provider_failure("endpoint-b", "TimeoutError: SSH observation timed out")
    assert _endpoint_monitor(service, "endpoint-b")["status"] == "ONLINE"

    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "jitter-still-allocatable",
                "purpose": "one failed probe must not block allocation",
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
            }
        ),
        idempotency_key="jitter-still-allocatable-claim",
    )
    assert len(claimed["lease"]["gpu_ids"]) == 1


def _present_uuid_owners(service, gpu_uuid: str) -> set[str]:  # noqa: ANN001
    with service.database.session() as session:
        return {
            gpu.endpoint_id
            for gpu in session.scalars(
                select(GPUDevice).where(GPUDevice.gpu_uuid == gpu_uuid, GPUDevice.present.is_(True))
            ).all()
        }


def test_one_physical_gpu_belongs_to_exactly_one_endpoint(service, admin) -> None:
    """Re-registering a machine on a new port must not duplicate its cards.

    A GPU UUID is one physical card. Two endpoint rows for it would let two
    callers lease the same card, so the endpoint that still sees it keeps it
    and a second registration stands down until the first goes stale.
    """

    shared = ["GPU-shared-0"]
    service.ingest_observation(observation("endpoint-a", gpu_uuids=shared))
    assert _present_uuid_owners(service, "GPU-shared-0") == {"endpoint-a"}

    # The same container reached through a second registration stands down.
    service.ingest_observation(observation("endpoint-b", gpu_uuids=shared))
    assert _present_uuid_owners(service, "GPU-shared-0") == {"endpoint-a"}

    # The incumbent keeps it for as long as it keeps answering.
    service.ingest_observation(observation("endpoint-a", gpu_uuids=shared))
    service.ingest_observation(observation("endpoint-b", gpu_uuids=shared))
    assert _present_uuid_owners(service, "GPU-shared-0") == {"endpoint-a"}

    # Once the old registration stops answering the new one takes over: this is
    # the restarted-container case, where only the forwarded port changed.
    with service.database.session() as session:
        gpu = session.scalar(
            select(GPUDevice).where(
                GPUDevice.endpoint_id == "endpoint-a", GPUDevice.gpu_uuid == "GPU-shared-0"
            )
        )
        assert gpu is not None
        gpu.last_seen_at = utcnow() - timedelta(
            seconds=service.inventory.collector.stale_after_seconds + 60
        )
        session.commit()

    service.ingest_observation(observation("endpoint-b", gpu_uuids=shared))
    assert _present_uuid_owners(service, "GPU-shared-0") == {"endpoint-b"}


def test_a_card_leased_through_one_endpoint_is_not_allocatable_through_another(
    service, admin
) -> None:
    """The allocator stays fail-closed even before ownership converges."""

    shared = ["GPU-shared-0"]
    service.ingest_observation(observation("endpoint-a", gpu_uuids=shared))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "hold-shared-card",
                "purpose": "hold the only card",
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-a"]},
            }
        ),
        idempotency_key="hold-shared-card",
    )
    assert claimed["lease"] is not None

    # An incomplete observation never converges ownership, so a duplicate row
    # can exist; the allocator must still refuse to hand out the same card.
    service.ingest_observation(
        observation("endpoint-b", gpu_uuids=shared, observation_complete=False)
    )
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            RequestCreate.model_validate(
                {
                    "project_id": "project-b",
                    "task_ref": "double-claim-shared-card",
                    "purpose": "must not get the same physical card",
                    "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
                }
            ),
            idempotency_key="double-claim-shared-card",
        )
    assert error.value.code == "no_capacity"


def test_registering_the_same_host_twice_is_refused_by_the_domain(service, admin) -> None:
    """The replay key never deduplicated a registration; two domain rules do.

    gpu_add_server now generates its own key, so a second call carries a
    different one. What stops a duplicate is the endpoint id derived from
    host:port, and the address rule behind it — and the caller gets a named
    409 it can act on, not a silently duplicated server.
    """

    created = service.create_endpoint(
        admin,
        EndpointCreate.model_validate(
            {
                "id": "server-10-0-0-8-p22",
                "host": "10.0.0.8",
                "port": 22,
                "ssh_user": "root",
                "workspace_path": "/srv/work",
                "owner_project_id": "project-a",
            }
        ),
        idempotency_key="register-once",
    )
    assert created["endpoint"]["id"] == "server-10-0-0-8-p22"

    with pytest.raises(BrokerError) as same_id:
        service.create_endpoint(
            admin,
            EndpointCreate.model_validate(
                {
                    "id": "server-10-0-0-8-p22",
                    "host": "10.0.0.8",
                    "port": 22,
                    "ssh_user": "root",
                    "workspace_path": "/srv/work",
                    "owner_project_id": "project-a",
                }
            ),
            idempotency_key="register-again-different-key",
        )
    assert same_id.value.code == "endpoint_exists"
    assert same_id.value.status_code == 409

    with pytest.raises(BrokerError) as same_address:
        service.create_endpoint(
            admin,
            EndpointCreate.model_validate(
                {
                    "id": "server-renamed",
                    "host": "10.0.0.8",
                    "port": 22,
                    "ssh_user": "root",
                    "workspace_path": "/srv/work",
                    "owner_project_id": "project-a",
                }
            ),
            idempotency_key="register-under-a-new-id",
        )
    assert same_address.value.code == "endpoint_address_exists"
    assert same_address.value.status_code == 409


def _make_unreachable(service, endpoint_id: str) -> None:  # noqa: ANN001
    """Stop the endpoint answering, the way a withdrawn container does."""
    from serverpilot.models import ProviderState

    service.record_provider_failure(endpoint_id, "TimeoutError: SSH observation timed out")
    with service.database.session() as session:
        state = session.scalar(
            select(ProviderState).where(ProviderState.endpoint_id == endpoint_id)
        )
        assert state is not None
        state.last_success_at = utcnow() - timedelta(
            seconds=service.inventory.collector.stale_after_seconds + 600
        )
        session.commit()


def test_a_server_that_stopped_answering_can_still_be_cleared_and_deleted(service, admin) -> None:
    """Otherwise a withdrawn container wedges its cards permanently.

    Every recovery path asks the machine for proof. When the machine is gone
    the proof can never arrive: the cards cannot be shown free, so the lease
    cannot be released, so the endpoint cannot be deleted. Holding the ledger
    protects nothing — ServerPilot cannot reach those processes either.
    """

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "stranded",
                "purpose": "the host disappears under it",
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
            }
        ),
        idempotency_key="stranded-claim",
    )
    lease_id = claimed["lease"]["id"]

    # While the host answers, the proof is still required.
    with pytest.raises(BrokerError) as still_live:
        service.release_empty_conflicted_lease(
            admin,
            "endpoint-b",
            lease_id,
            observation_not_before=utcnow(),
            idempotency_key="clear-while-live",
        )
    assert still_live.value.code == "conflict_observation_stale"

    _make_unreachable(service, "endpoint-b")

    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-b",
        lease_id,
        observation_not_before=utcnow(),
        idempotency_key="clear-when-gone",
    )
    assert released["lease"]["state"] == "RELEASED"
    # The reason states what happened; it must not claim the cards were seen empty.
    assert released["lease"]["release_reason"] == "server unreachable; operator settled the ledger"

    deleted = service.delete_endpoint(admin, "endpoint-b", idempotency_key="delete-when-gone")
    assert deleted["changed"] is True
    assert "endpoint-b" not in {e["id"] for e in service.list_endpoints(admin)["data"]}


def test_deleting_an_unreachable_server_settles_the_leases_it_still_holds(service, admin) -> None:
    """The delete is the operator saying the machine is gone; nothing may dangle."""

    service.ingest_observation(observation(endpoint_id="endpoint-b", count=1))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "still-held",
                "purpose": "never released by hand",
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
            }
        ),
        idempotency_key="still-held-claim",
    )
    lease_id = claimed["lease"]["id"]

    # A reachable host still refuses, so a live server cannot be deleted out
    # from under a running job.
    with pytest.raises(BrokerError) as live:
        service.delete_endpoint(admin, "endpoint-b", idempotency_key="delete-while-live")
    assert live.value.code == "endpoint_has_active_leases"

    _make_unreachable(service, "endpoint-b")

    deleted = service.delete_endpoint(admin, "endpoint-b", idempotency_key="delete-gone")
    assert deleted["settled_lease_ids"] == [lease_id]
    with service.database.session() as session:
        settled = session.get(Lease, lease_id)
        assert settled is not None
        assert settled.state == "RELEASED"
        assert settled.release_reason == "server deleted while unreachable"


def test_one_absent_complete_observation_does_not_retire_a_process(service, admin) -> None:
    """A single listing without a process is one absent sample, not an ending.

    The compute-app probe is separate from the GPU query and reports an empty
    list both when nothing runs and when it cannot see its own PID namespace,
    which is what a containerised host does. Retiring on that one reading is
    how a card running an eight-card job read as empty.
    """

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )
    service.ingest_observation(observation(count=1, processes=[]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "BUSY_UNMANAGED"
    assert [item["pid"] for item in gpu["processes"]] == [4321]

    def read(session):  # type: ignore[no-untyped-def]
        return session.scalars(select(ProcessObservation)).all()[0].active

    assert service._read(read) is True


def test_process_is_retired_once_the_absence_grace_elapses(service, admin) -> None:
    """The criterion is age, so a process that really ended does go away."""

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )
    age_out_processes(service)
    service.ingest_observation(observation(count=1, processes=[]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "AVAILABLE"
    assert gpu["processes"] == []

    assert service._read(lambda s: s.scalars(select(ProcessObservation)).all()[0].active) is False


def test_a_collection_outage_restarts_the_absence_instead_of_counting_toward_it(
    service, admin
) -> None:
    """The absence window is an evidence chain, not a wall clock.

    p8908 flapped three times in 45 minutes while a real job ran on its cards.
    A window measured on wall time would have run out during an outage and
    retired a running process; the clock has to be cleared by the very failure
    that stopped producing evidence.
    """

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )
    # One complete observation leaves it out, so the absence starts.
    service.ingest_observation(observation(count=1, processes=[]))
    absence = service._read(lambda s: s.scalars(select(ProcessObservation)).all()[0].absent_since)
    assert absence is not None

    service.record_provider_failure("endpoint-a", "ssh: connect: connection refused")
    assert (
        service._read(lambda s: s.scalars(select(ProcessObservation)).all()[0].absent_since) is None
    )

    # Even after the window's worth of wall time, the chain restarted: the
    # first complete observation after the outage only begins the absence.
    age_out_processes(service)
    service.record_provider_failure("endpoint-a", "ssh: connect: connection refused")
    service.ingest_observation(observation(count=1, processes=[]))
    process = service._read(lambda s: s.scalars(select(ProcessObservation)).all()[0])
    assert process.active is True
    assert service.list_gpus(admin)["data"][0]["state"] == "BUSY_UNMANAGED"


def test_a_card_its_owner_released_comes_back_once_the_absence_closes(service, admin) -> None:
    """Release then re-apply is the primary agent path, so it must converge.

    The card is not free the instant the lease ends -- a retained process still
    blocks it, on purpose -- but once the endpoint's own observations have left
    the work out for the window, the same cards are claimable again.
    """

    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("release-then-reapply", count=2),
        idempotency_key="release-then-reapply-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu("GPU-endpoint-a-0", pid=4321),
                process_for_gpu("GPU-endpoint-a-1", pid=4322),
            ],
        )
    )
    service.release_lease(
        admin, lease_id, reason="stage finished", idempotency_key="release-then-reapply-release"
    )

    # The job really ended; the endpoint keeps saying so for the whole window.
    age_out_processes(service)
    service.ingest_observation(observation(count=2, processes=[]))

    assert [gpu["state"] for gpu in service.list_gpus(admin)["data"]] == [
        "AVAILABLE",
        "AVAILABLE",
    ]
    again = service.create_request(
        admin,
        request_data("release-then-reapply-2", count=2),
        idempotency_key="release-then-reapply-claim-2",
        activate_if_allocated=True,
    )
    assert again["lease"] is not None
    assert len(again["lease"]["gpu_ids"]) == 2


def test_host_pid_identity_survives_a_collection_gap(service, admin) -> None:
    """A host PID's identity is only reusable while its row is still active.

    On a containerised host the collector cannot read a username or a start
    time, so the row itself is the identity. Losing it across one gap made the
    same running process come back as a new, unattributable one.
    """

    host_pid = ProcessInput(
        gpu_uuid="GPU-endpoint-a-0",
        pid=804753,
        used_memory_mib=8192,
        executable="[Not Found]",
        username=None,
        process_started_at=utcnow(),
    )
    service.ingest_observation(observation(count=1, processes=[host_pid]))
    service.ingest_observation(observation(count=1, observation_complete=False))
    later = host_pid.model_copy(update={"process_started_at": utcnow() + timedelta(seconds=20)})
    service.ingest_observation(observation(count=1, processes=[later]))

    def read(session):  # type: ignore[no-untyped-def]
        return session.scalars(select(ProcessObservation)).all()

    rows = service._read(read)
    assert len(rows) == 1, "the same running process must not come back as a second identity"
    # Two sightings, not three: the incomplete collection in the middle
    # observed nothing at all, and it did not cost the row its identity either.
    assert rows[0].observations == 2
    assert rows[0].pid == 804753


def test_idle_since_is_not_stamped_across_a_collection_flap(service, admin) -> None:
    """A telemetry failure and recovery is not an observation of idleness."""

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("flap"), idempotency_key="flap")
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )

    service.ingest_observation(observation(count=1, observation_complete=False))
    service.ingest_observation(observation(count=1, processes=[]))

    def read(session):  # type: ignore[no-untyped-def]
        return [
            resource.idle_since
            for resource in session.scalars(
                select(LeaseResource).where(LeaseResource.lease_id == lease_id)
            ).all()
        ]

    assert service._read(read) == [None]


def test_manual_release_refuses_a_workload_lease_whose_holder_was_just_alive(
    service, admin
) -> None:
    """This is the click that cleared a working eight-card claim.

    Every card of a staged job runs nothing between two batches of shards, and
    that gap satisfied the old criterion on its own.
    """

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin, request_data("staged-manual"), idempotency_key="staged-manual-claim"
    )
    lease_id = claimed["lease"]["id"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )

    age_out_processes(service)
    barrier = utcnow()
    service.ingest_observation(observation(count=1, processes=[]))
    with pytest.raises(BrokerError) as refused:
        service.release_empty_conflicted_lease(
            admin,
            "endpoint-a",
            lease_id,
            observation_not_before=barrier,
            idempotency_key="staged-manual-release",
        )

    assert refused.value.code == "lease_holder_recently_alive"
    assert refused.value.status_code == 409
    assert refused.value.details["required_seconds"] == service.inventory.idle_lease_alert_seconds
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] in {"HELD", "ACTIVE"}
    assert lease["gpu_ids"] == claimed["lease"]["gpu_ids"]


def test_manual_release_accepts_the_same_lease_once_the_holder_has_gone_quiet(
    service, admin
) -> None:
    """The refusal is a window, not a permanent block on the recovery path."""

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin, request_data("quiet-manual"), idempotency_key="quiet-manual-claim"
    )
    lease_id = claimed["lease"]["id"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )

    age_out_processes(service)
    barrier = utcnow()
    service.ingest_observation(observation(count=1, processes=[]))
    age_out_lease_holder(service, lease_id)
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="quiet-manual-release",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"


def test_snapshot_publishes_the_same_manual_release_answer_the_release_would_give(
    service, admin
) -> None:
    """A greyed out button and a 409 have to say the same word."""

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin, request_data("published"), idempotency_key="published-claim"
    )
    lease_id = claimed["lease"]["id"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )

    def verdict() -> dict:  # type: ignore[type-arg]
        payload = next(
            item for item in service.snapshot(admin)["data"]["leases"] if item["id"] == lease_id
        )
        return payload["manual_release"]

    assert verdict()["blocked_reason"] == "conflict_process_present"

    age_out_processes(service)
    barrier = utcnow()
    service.ingest_observation(observation(count=1, processes=[]))
    assert verdict()["allowed"] is False
    assert verdict()["blocked_reason"] == "lease_holder_recently_alive"
    with pytest.raises(BrokerError) as refused:
        service.release_empty_conflicted_lease(
            admin,
            "endpoint-a",
            lease_id,
            observation_not_before=barrier,
            idempotency_key="published-refused",
        )
    assert refused.value.code == verdict()["blocked_reason"]

    age_out_lease_holder(service, lease_id)
    assert verdict() == {
        "allowed": True,
        "blocked_reason": None,
        "message": None,
        "details": {},
    }
