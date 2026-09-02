from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import (
    Endpoint,
    KeepaliveCurrent,
    Lease,
    LeaseResource,
    ProcessObservation,
    TelemetryCurrent,
)
from serverpilot.schemas import EndpointUpdate, LeaseObservedBind, RequestCreate
from serverpilot.service import BrokerError, BrokerService
from serverpilot.timeutil import utcnow
from tests.helpers import (
    age_out_lease_holder,
    age_out_processes,
    keepalive_start_candidates,
    observation,
    process_for_gpu,
)


def _configure_idle_policy(service, admin, *, count: int = 2) -> None:  # noqa: ANN001
    with service.database.session() as session:
        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        endpoint.keepalive_adapter_id = "server-script-v1"
        session.commit()
    service.ingest_observation(observation(count=count))
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="policy-idle",
    )


def _begin(service, admin, index: int = 0) -> dict[str, object]:  # noqa: ANN001
    started = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu(f"GPU-endpoint-a-{index}", pid=4321 + index)],
        )
    )
    # The live batch entry, given one card. A single-GPU wrapper existed beside
    # it that only tests ever called, so the tests use the batch path the
    # product actually takes.
    result = service.activate_keepalives(
        admin,
        "endpoint-a",
        [f"endpoint-a:GPU-endpoint-a-{index}"],
        observation_not_before=started,
        idempotency_key=f"activate-{index}",
    )
    # The batch entry answers with a list. These tests start one card at a
    # time, so name that one result the way the assertions below read it.
    return {**result, "keepalive": result["keepalives"][0]}


def _confirm(service, admin, begun: dict[str, object], index: int = 0) -> dict[str, object]:  # noqa: ANN001
    return begun


def _endpoint(snapshot: dict[str, object]) -> dict[str, object]:
    endpoints = snapshot["data"]
    assert isinstance(endpoints, dict)
    value = next(item for item in endpoints["endpoints"] if item["id"] == "endpoint-a")
    assert isinstance(value, dict)
    return value


def _gpus(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    data = snapshot["data"]
    assert isinstance(data, dict)
    return {item["id"]: item for item in data["gpus"] if item["endpoint_id"] == "endpoint-a"}


def test_policy_is_persisted_and_candidates_are_independent_per_gpu(service, admin) -> None:
    _configure_idle_policy(service, admin)

    assert set(keepalive_start_candidates(service, "endpoint-a")) == {
        "endpoint-a:GPU-endpoint-a-0",
        "endpoint-a:GPU-endpoint-a-1",
    }

    begun = _begin(service, admin, 0)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    with service.database.session() as session:
        lease = session.get(Lease, keepalive["lease_id"])
        assert lease is not None and lease.kind == "keepalive"
        assert [
            resource.gpu_id
            for resource in session.scalars(
                select(LeaseResource).where(LeaseResource.lease_id == lease.id)
            )
        ] == ["endpoint-a:GPU-endpoint-a-0"]

    assert keepalive_start_candidates(service, "endpoint-a") == [
        "endpoint-a:GPU-endpoint-a-1",
    ]
    summary = service.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]
    assert summary["policy"] == "idle_keepalive"
    assert summary["desired"] == "ON"
    assert summary["actual"] == "ON"
    assert summary["error_gpu_count"] == 0
    assert summary["eligible_idle_gpu_count"] == 1


def test_one_gpu_keepalive_does_not_block_sibling_gpu_or_hide_public_state(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _confirm(service, admin, _begin(service, admin, 0), 0)

    snapshot = service.snapshot(admin)
    endpoint = _endpoint(snapshot)
    gpus = _gpus(snapshot)
    first = gpus["endpoint-a:GPU-endpoint-a-0"]
    second = gpus["endpoint-a:GPU-endpoint-a-1"]
    assert first["state"] == "KEEPALIVE"
    assert first["keepalive"]["desired"] == "ON"
    assert first["keepalive"]["actual"] == "ON"
    assert second["state"] == "AVAILABLE"
    assert second["keepalive"]["desired"] == "ON"
    assert second["keepalive"]["actual"] == "OFF"
    assert second["keepalive"]["reason"] is None
    assert second["publicly_available"] is True
    assert second["public_status"] == "可用 · 占卡未运行"
    assert endpoint["keepalive"] == {
        "configured": True,
        "policy": "idle_keepalive",
        "desired": "ON",
        "actual": "ON",
        "state": "ON",
        "active_gpu_count": 1,
        "error_gpu_count": 0,
        "eligible_idle_gpu_count": 1,
        "reasons": [],
    }
    assert all(item["kind"] != "keepalive" for item in snapshot["data"]["leases"])
    assert keepalive_start_candidates(service, "endpoint-a") == ["endpoint-a:GPU-endpoint-a-1"]


def test_public_gpu_status_reports_connection_failure_from_canonical_monitor_state() -> None:
    projection = BrokerService._gpu_public_projection(
        {
            "state": "UNKNOWN_STALE",
            "lease": None,
            "keepalive": {"state": "OFF", "reason": None},
        },
        monitor_status="ERROR",
    )

    assert projection == {
        "publicly_available": False,
        "public_status": "连接失败",
    }


def test_workload_turnover_on_one_gpu_does_not_block_sibling_keepalive_candidate(
    service, admin
) -> None:
    """A normal workload worker replacement leaves sibling capacity unchanged."""

    _configure_idle_policy(service, admin)
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "conflict-on-one-gpu",
                "purpose": "test independent keepalive placement",
                "duration_seconds": 600,
                "constraints": {"gpu_count": 1, "placement": "pack"},
            }
        ),
        idempotency_key="conflict-on-one-gpu-claim",
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    started_at = utcnow() - timedelta(minutes=3)
    initial = process_for_gpu(gpu_uuid).model_copy(update={"process_started_at": started_at})
    service.ingest_observation(observation(count=2, processes=[initial]))
    service.bind_observed_workload(
        admin,
        lease_id,
        LeaseObservedBind(run_id="conflict-on-one-gpu-run"),
        idempotency_key="conflict-on-one-gpu-bind",
    )
    replacement = initial.model_copy(
        update={"process_started_at": started_at + timedelta(seconds=10)}
    )
    service.ingest_observation(observation(count=2, processes=[replacement]))
    service.ingest_observation(observation(count=2, processes=[replacement]))

    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "RUNNING_MANAGED"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "AVAILABLE"
    assert keepalive_start_candidates(service, "endpoint-a") == ["endpoint-a:GPU-endpoint-a-1"]


def test_confirm_rejects_additional_process_on_keepalive_gpu(service, admin) -> None:
    _configure_idle_policy(service, admin)
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu("GPU-endpoint-a-0", pid=4321),
                process_for_gpu("GPU-endpoint-a-0", pid=9999),
            ],
        )
    )
    with pytest.raises(BrokerError) as conflicted:
        service.activate_keepalives(
            admin,
            "endpoint-a",
            ["endpoint-a:GPU-endpoint-a-0"],
            observation_not_before=barrier,
            idempotency_key="activate-current-process",
        )
    assert conflicted.value.code == "keepalive_process_conflict"


def test_foreign_replacement_never_becomes_public_keepalive_capacity(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    # The keeper this endpoint started is gone and a different process now
    # holds the card. A process is retired by age, so "gone" is expressed by
    # ageing its last sighting, not by one observation that omits it.
    age_out_processes(service)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )

    gpu = _gpus(service.snapshot(admin))["endpoint-a:GPU-endpoint-a-0"]
    assert gpu["state"] == "CONFLICT"
    assert gpu["keepalive"]["desired"] == "ON"
    assert gpu["keepalive"]["actual"] == "ERROR"
    assert gpu["keepalive"]["reason"] == "检测到不属于占卡程序的进程"
    assert gpu["publicly_available"] is False
    assert gpu["public_status"] == "占卡校验失败，暂不可申请"


def test_live_identity_mismatch_plans_attested_recovery_without_admission(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    # The keeper this endpoint started is gone and a different process now
    # holds the card. A process is retired by age, so "gone" is expressed by
    # ageing its last sighting, not by one observation that omits it.
    age_out_processes(service)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )

    transitions = service.list_keepalive_transitions("endpoint-a")["transitions"]

    assert transitions == [
        {
            "action": "recover",
            "endpoint_id": "endpoint-a",
            "gpu_id": "endpoint-a:GPU-endpoint-a-0",
            "gpu_uuid": "GPU-endpoint-a-0",
            "reason": "requires sealed helper attestation",
        },
        {
            "action": "start",
            "endpoint_id": "endpoint-a",
            "gpu_id": "endpoint-a:GPU-endpoint-a-1",
            "gpu_uuid": "GPU-endpoint-a-1",
        },
    ]
    gpu = _gpus(service.snapshot(admin))["endpoint-a:GPU-endpoint-a-0"]
    assert gpu["state"] == "CONFLICT"
    assert gpu["publicly_available"] is False


def test_attested_confirmation_rebinds_replaced_keepalive_worker(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    barrier = utcnow()
    # The keeper this endpoint started is gone and a different process now
    # holds the card. A process is retired by age, so "gone" is expressed by
    # ageing its last sighting, not by one observation that omits it.
    age_out_processes(service)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    confirmed = service.confirm_keepalive_workers(
        admin,
        "endpoint-a",
        [gpu_id],
        confirmed_worker_identities={gpu_id: (8801, "boot-endpoint-a")},
        observation_not_before=barrier,
        idempotency_key="confirm-replaced-keeper",
    )

    assert confirmed["keepalives"][0]["state"] == "ACTIVE"
    with service.database.session() as session:
        current = session.get(KeepaliveCurrent, gpu_id)
        observed = session.scalar(
            select(ProcessObservation).where(
                ProcessObservation.gpu_id == gpu_id,
                ProcessObservation.pid == 8801,
                ProcessObservation.active.is_(True),
            )
        )
        assert current is not None
        assert observed is not None
        assert current.actual == "ON"
        assert current.error_reason is None
        assert current.expected_pid == observed.pid
        assert current.expected_boot_id == observed.boot_id
        assert current.expected_process_started_at == observed.process_started_at
    gpu = _gpus(service.snapshot(admin))[gpu_id]
    assert gpu["state"] == "KEEPALIVE"
    assert gpu["publicly_available"] is True


def test_attested_confirmation_starts_and_binds_new_keepalive_worker(service, admin) -> None:
    _configure_idle_policy(service, admin)
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=7722)],
        )
    )
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    confirmed = service.confirm_keepalive_workers(
        admin,
        "endpoint-a",
        [gpu_id],
        confirmed_worker_identities={gpu_id: (7722, "boot-endpoint-a")},
        observation_not_before=barrier,
        idempotency_key="confirm-new-keeper",
    )

    assert confirmed["keepalives"][0]["state"] == "ACTIVE"
    gpu = _gpus(service.snapshot(admin))[gpu_id]
    assert gpu["state"] == "KEEPALIVE"
    assert gpu["keepalive"]["actual"] == "ON"


def test_attested_confirmation_rejects_unmatched_identity_and_keeps_conflict(
    service, admin
) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    barrier = utcnow()
    # The keeper this endpoint started is gone and a different process now
    # holds the card. A process is retired by age, so "gone" is expressed by
    # ageing its last sighting, not by one observation that omits it.
    age_out_processes(service)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    with pytest.raises(BrokerError) as rejected:
        service.confirm_keepalive_workers(
            admin,
            "endpoint-a",
            [gpu_id],
            confirmed_worker_identities={gpu_id: (4321, "boot-endpoint-a")},
            observation_not_before=barrier,
            idempotency_key="reject-foreign-replacement",
        )

    assert rejected.value.code == "keepalive_confirmation_mismatch"
    gpu = _gpus(service.snapshot(admin))[gpu_id]
    assert gpu["state"] == "CONFLICT"
    assert gpu["keepalive"]["actual"] == "ERROR"


def test_reclaim_plan_selects_only_complete_verified_per_gpu_keepalive_set(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin, 0)
    _confirm(service, admin, begun, 0)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    # The target keeper's current free VRAM is deliberately below the request
    # floor. It becomes eligible only because the planner can prove that this
    # exact worker will be stopped and fresh telemetry will be checked again.
    with service.database.session() as session:
        current = session.get(TelemetryCurrent, "endpoint-a:GPU-endpoint-a-0")
        assert current is not None
        current.memory_free_mib = 50_000
        session.commit()
    request = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "two-gpu-workload",
            "purpose": "needs both test GPUs",
            "duration_seconds": 600,
            "constraints": {"gpu_count": 2, "min_free_vram_mib": 70_000},
        }
    )
    plan = service.plan_keepalive_reclaim(request)
    assert plan["complete"] is True
    assert plan["transitions"] == [
        {
            "action": "reclaim",
            "endpoint_id": "endpoint-a",
            "gpu_id": "endpoint-a:GPU-endpoint-a-0",
            "gpu_uuid": "GPU-endpoint-a-0",
            "lease_id": keepalive["lease_id"],
        }
    ]


def test_stale_keeper_is_not_reclaimable_capacity_and_reports_error(service, admin) -> None:
    """A host that stopped answering must not be chosen for a claim.

    Reclaiming a keeper needs that host to accept an adapter stop and then
    answer a fresh observation. When its telemetry has gone stale we can no
    longer assert the keeper is running, so the card leaves the candidate set
    instead of sending the claim into an SSH timeout.
    """

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin, 0)
    _confirm(service, admin, begun, 0)

    gpu_id = "endpoint-a:GPU-endpoint-a-0"
    assert _gpus(service.snapshot(admin))[gpu_id]["keepalive"]["actual"] == "ON"

    stale = utcnow() - timedelta(seconds=service.collector.stale_after_seconds + 60)
    with service.database.session() as session:
        current = session.get(TelemetryCurrent, gpu_id)
        assert current is not None
        current.observed_at = stale
        session.commit()

    assert _gpus(service.snapshot(admin))[gpu_id]["keepalive"]["actual"] == "ERROR"

    request = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "two-gpu-workload",
            "purpose": "needs both test GPUs",
            "duration_seconds": 600,
            "constraints": {"gpu_count": 2},
        }
    )
    plan = service.plan_keepalive_reclaim(request)
    assert plan["complete"] is False
    assert plan["transitions"] == []
    assert plan["excluded"].get("unknown_stale") == 1


def test_stop_is_per_gpu_and_requires_fresh_empty_target_observation(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="policy-disabled",
    )
    plan = service.list_keepalive_transitions("endpoint-a")
    assert [item["action"] for item in plan["transitions"]] == ["stop"]
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)

    barrier = utcnow()
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )
    with pytest.raises(BrokerError) as running:
        service.finalize_keepalive_stop(
            admin,
            "endpoint-a",
            str(keepalive["lease_id"]),
            observation_not_before=barrier,
            idempotency_key="stop-running",
        )
    assert running.value.code == "keepalive_process_still_running"

    barrier = utcnow()
    service.ingest_observation(observation(count=2))
    stopped = service.finalize_keepalive_stop(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        observation_not_before=barrier,
        idempotency_key="stop-empty",
    )
    assert stopped["keepalive"]["state"] == "RELEASED"
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "AVAILABLE"


def test_validated_keepalive_stop_clears_old_expected_process_identity(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="disable-before-identity-clear",
    )
    barrier = utcnow()
    service.ingest_observation(observation(count=2))

    service.finalize_keepalive_stop(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        observation_not_before=barrier,
        idempotency_key="validated-stop-clears-identity",
    )

    with service.database.session() as session:
        current = session.get(KeepaliveCurrent, "endpoint-a:GPU-endpoint-a-0")
        assert current is not None
        assert current.actual == "OFF"
        assert current.expected_pid is None
        assert current.expected_boot_id is None
        assert current.expected_process_started_at is None


def test_complete_observation_releases_keepalive_lease_on_absent_gpu(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.ingest_observation(observation(gpu_uuids=["GPU-new-0", "GPU-new-1"]))

    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == "RELEASED"
        assert lease.release_reason == "gpu absent from endpoint inventory"
        resources = session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease_id)
        ).all()
        assert resources
        assert all(not resource.active for resource in resources)
        assert session.get(KeepaliveCurrent, "endpoint-a:GPU-endpoint-a-0") is None

    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "after-absent-keepalive",
                "purpose": "new GPU should be allocatable",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="after-absent-keepalive",
    )
    assert claimed["lease"] is not None
    assert claimed["lease"]["gpu_ids"] == ["endpoint-a:GPU-new-0"]


def test_absent_workload_lease_is_not_auto_released_but_can_be_cleared(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "absent-workload",
                "purpose": "hold old GPU",
                "duration_seconds": 3600,
                "constraints": {
                    "gpu_count": 1,
                    "placement": "exact",
                    "gpu_ids": ["endpoint-a:GPU-old"],
                },
            }
        ),
        idempotency_key="absent-workload",
    )
    lease_id = claimed["lease"]["id"]

    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state in {"HELD", "ACTIVE"}
        assert lease.kind == "workload"

    barrier = utcnow()
    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    age_out_lease_holder(service, lease_id)
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="absent-workload-release-empty",
    )
    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"


def test_incomplete_observation_does_not_release_absent_keepalive_lease(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.ingest_observation(observation(gpu_uuids=["GPU-new-0"], observation_complete=False))

    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == "ACTIVE"
        resources = session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
            )
        ).all()
        assert [resource.gpu_id for resource in resources] == ["endpoint-a:GPU-endpoint-a-0"]


def test_endpoint_operator_can_clear_stale_per_gpu_keepalive_lease(service, admin) -> None:
    """A failed stop leaves a recoverable internal lease, not a permanent wedge."""

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="stale-keepalive-policy-disabled",
    )
    barrier = utcnow()
    # The stop failed, so nothing proved this worker ended; the card is only
    # recoverable once its last sighting has aged out too.
    age_out_processes(service)
    service.ingest_observation(observation(count=2, processes=[]))

    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="stale-keepalive-release-empty",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"


def test_a_keepalive_lease_whose_worker_still_runs_is_not_clearable(service, admin) -> None:
    """Skipping the liveness window never means skipping the emptiness proof.

    A keepalive lease waives the "has the holder gone quiet" wait, because the
    person clicking *is* the holder. It does not waive "this card shows no
    process": clearing the lease that owns a running 80%-of-VRAM worker would
    leave that worker on the card with nothing pointing at it, and the stop
    plan cannot reach it any more either.
    """

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    barrier = utcnow()
    # A fresh, complete observation that still lists the keepalive worker.
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )

    with pytest.raises(BrokerError) as error:
        service.release_empty_conflicted_lease(
            admin,
            "endpoint-a",
            lease_id,
            observation_not_before=barrier,
            idempotency_key="running-keepalive-release-empty",
        )
    assert error.value.code == "conflict_process_present"
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["keepalive"]["lease_id"] == lease_id


def test_probe_failure_after_a_complete_observation_still_activates_keepalive(
    service, admin
) -> None:
    """A later failed attempt does not un-observe the observation that already landed."""

    _configure_idle_policy(service, admin)
    started = utcnow()
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )
    service.record_provider_failure("endpoint-a", "TimeoutError: SSH observation timed out")

    begun = service.activate_keepalives(
        admin,
        "endpoint-a",
        ["endpoint-a:GPU-endpoint-a-0"],
        observation_not_before=started,
        idempotency_key="activate-after-probe-failure",
    )

    keepalive = begun["keepalives"][0]
    assert isinstance(keepalive, dict)
    assert keepalive["state"] == "ACTIVE"
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "KEEPALIVE"


def test_probe_failure_after_an_empty_observation_still_clears_the_lease(service, admin) -> None:
    """The proof is one empty observation after the barrier, not an unbroken streak."""

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="probe-failure-policy-disabled",
    )
    barrier = utcnow()
    age_out_processes(service)
    service.ingest_observation(observation(count=2, processes=[]))
    service.record_provider_failure("endpoint-a", "TimeoutError: SSH observation timed out")

    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="probe-failure-release-empty",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    assert (
        released["lease"]["release_reason"] == "empty fresh observation cleared endpoint ownership"
    )
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"


def test_active_keepalive_adapter_cannot_be_removed(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin)

    with pytest.raises(BrokerError) as blocked:
        service.update_endpoint(
            admin,
            "endpoint-a",
            EndpointUpdate.model_validate({"keepalive_adapter_id": None}),
            idempotency_key="remove-active-keepalive-adapter",
        )
    assert blocked.value.code == "keepalive_endpoint_connection_in_use"


def test_active_keepalive_workspace_cannot_change(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin)

    with pytest.raises(BrokerError) as blocked:
        service.update_endpoint(
            admin,
            "endpoint-a",
            EndpointUpdate.model_validate({"workspace_path": "/srv/project-a-next"}),
            idempotency_key="change-active-keepalive-workspace",
        )
    assert blocked.value.code == "keepalive_endpoint_connection_in_use"
    assert blocked.value.details == {"fields": ["workspace_path"]}


def test_lease_telemetry_never_reports_a_previous_holders_load(service, admin) -> None:
    """A holder may only be shown its own load.

    The plain ten-minute window starts before the claim, so a card taken a
    moment ago used to report whatever ran on it just before — a previous job,
    or ServerPilot's own keepalive hold — as the caller's. An agent sizing a
    batch from that sizes it against somebody else's work.
    """

    from serverpilot.models import TelemetrySnapshot

    service.ingest_observation(observation(count=1))
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    # Somebody else's heavy run, well inside the ten-minute window.
    earlier = utcnow() - timedelta(minutes=5)
    with service.database.session() as session:
        for offset in range(3):
            session.add(
                TelemetrySnapshot(
                    gpu_id=gpu_id,
                    observed_at=earlier + timedelta(seconds=offset),
                    collected_at=earlier + timedelta(seconds=offset),
                    memory_used_mib=70_000,
                    memory_free_mib=11_920,
                    gpu_utilization_pct=99,
                    memory_utilization_pct=90,
                    temperature_c=80,
                    power_watts=400,
                    pstate="P0",
                    health="OK",
                    provider="raw-ssh",
                )
            )
        session.commit()

    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "fresh-claim",
                "purpose": "a brand new hold",
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="fresh-claim",
    )
    assert claimed["lease"] is not None

    gpu = _gpus(service.snapshot(admin))[gpu_id]
    telemetry = gpu["telemetry"]
    # The unclamped average still describes the card and is still pulled up by
    # the earlier run; only what the holder is shown is clamped, and there is
    # nothing yet to show for a hold that just started.
    assert telemetry["recent_average"]["gpu_utilization_pct"] > 50
    assert telemetry["lease_recent_average"] is None


def test_a_lease_publishes_when_it_last_had_a_process(service, admin) -> None:
    """Clearing an "empty" lease must be able to tell a gap from an ending.

    A job between two batches is observationally identical to one that
    finished, and clearing the first wedges its cards: the work comes back to
    a card that no longer belongs to anyone. This field is read-only and never
    gates the release — gating here would wedge the very leases that path
    exists to recover.
    """

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "batched-job",
                "purpose": "runs in bursts",
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="batched-job",
    )
    lease_id = claimed["lease"]["id"]

    def lease_payload() -> dict:
        data = service.snapshot(admin)["data"]
        return next(item for item in data["leases"] if item["id"] == lease_id)

    assert lease_payload()["last_process_observed_at"] is None

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0", pid=9931)])
    )
    while_running = lease_payload()["last_process_observed_at"]
    assert while_running is not None

    # The burst ends. The lease looks empty, but it is not finished — and the
    # timestamp still says how recently it was working.
    service.ingest_observation(observation(count=1))
    assert lease_payload()["last_process_observed_at"] == while_running


def test_a_new_lease_never_inherits_the_previous_holders_process_time(service, admin) -> None:
    """The card is re-let seconds after the last job stopped; the clock resets.

    Whoever is about to clear a lease reads this to tell a burst gap from an
    ending. Inheriting the previous holder's timestamp says "this was working
    moments ago" about a lease that has never run anything, which is the
    strongest possible signal to leave it alone -- exactly backwards.
    """

    service.ingest_observation(observation(count=1))
    gpu_uuid = "GPU-endpoint-a-0"

    def claim(task: str) -> str:
        claimed = service.create_request(
            admin,
            RequestCreate.model_validate(
                {
                    "project_id": "project-a",
                    "task_ref": task,
                    "purpose": task,
                    "constraints": {"gpu_count": 1},
                }
            ),
            idempotency_key=task,
        )
        assert claimed["lease"] is not None
        return claimed["lease"]["id"]

    def last_process(lease_id: str) -> str | None:
        data = service.snapshot(admin)["data"]
        payload = next(item for item in data["leases"] if item["id"] == lease_id)
        return payload["last_process_observed_at"]

    first = claim("first-holder")
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4001)])
    )
    assert last_process(first) is not None

    # The job stops and the lease is handed back.
    age_out_processes(service)
    service.ingest_observation(observation(count=1))
    service.release_lease(admin, first, reason="done", idempotency_key="first-release")

    second = claim("second-holder")
    assert last_process(second) is None, "a brand new hold has run nothing yet"

    # Its own work does show up.
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4002)])
    )
    assert last_process(second) is not None


def _backdate_workload_release(service, lease_id: str, seconds: int) -> None:  # noqa: ANN001
    """Age a released workload lease's resource rows, as real time would."""

    def write(session):  # type: ignore[no-untyped-def]
        stamp = utcnow() - timedelta(seconds=seconds)
        for resource in session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease_id)
        ).all():
            resource.released_at = stamp

    service._write(write)


def test_keepalive_start_is_blocked_during_workload_release_cooldown(service, admin) -> None:
    """A shard-boundary gap must not look like the previous holder giving up.

    p8908: a workload lease released at 17:19:52 had a keepalive worker
    claiming 80% of its VRAM by 17:20:00 -- eight seconds later, well inside
    that task's real gap between two batches of shards.
    """

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "shard-batch-one",
                "purpose": "test keepalive start cooldown",
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="cooldown-claim",
    )
    lease_id = claimed["lease"]["id"]
    service.release_lease(admin, lease_id, reason="done", idempotency_key="cooldown-release")

    _configure_idle_policy(service, admin, count=1)

    gpu_id = "endpoint-a:GPU-endpoint-a-0"
    entry = next(
        item
        for item in service.list_keepalive_transitions("endpoint-a")["transitions"]
        if item["gpu_id"] == gpu_id
    )
    assert entry["action"] == "ineligible"
    assert "cooldown" in entry["reason"]

    _backdate_workload_release(
        service, lease_id, service.inventory.keepalive_start_cooldown_seconds + 1
    )
    settled = next(
        item
        for item in service.list_keepalive_transitions("endpoint-a")["transitions"]
        if item["gpu_id"] == gpu_id
    )
    assert settled["action"] == "start"


def test_keepalive_does_not_start_on_a_card_that_ran_a_process_within_the_grace(
    service, admin
) -> None:
    """Start eligibility reads the same "is anything on this card" answer.

    A card whose last compute process is still a current fact is not free
    capacity, so occupancy does not plan a worker onto it.
    """

    _configure_idle_policy(service, admin, count=1)
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0", pid=9101)])
    )
    service.ingest_observation(observation(count=1, processes=[]))

    gpu_id = "endpoint-a:GPU-endpoint-a-0"
    assert gpu_id not in set(keepalive_start_candidates(service, "endpoint-a"))

    age_out_processes(service)
    service.ingest_observation(observation(count=1, processes=[]))
    assert gpu_id in set(keepalive_start_candidates(service, "endpoint-a"))


def test_stop_refuses_while_a_foreign_process_is_still_within_the_absence_grace(
    service, admin
) -> None:
    """One empty listing is not proof that a foreign process ended.

    Seven occupancy workers were reported stopped with "target GPU observed
    empty" while the agent's own processes were still running on those cards.
    """

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin, 1)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="foreign-process-policy-disabled",
    )

    # Somebody else's process arrives on the occupancy card.
    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu("GPU-endpoint-a-1", pid=4322),
                process_for_gpu("GPU-endpoint-a-1", pid=804753),
            ],
        )
    )

    barrier = utcnow()
    service.ingest_observation(observation(count=2, processes=[]))
    with pytest.raises(BrokerError) as blocked:
        service.finalize_keepalive_stop(
            admin,
            "endpoint-a",
            str(keepalive["lease_id"]),
            observation_not_before=barrier,
            idempotency_key="stop-with-foreign-process",
        )
    assert blocked.value.code == "keepalive_process_still_running"

    age_out_processes(service)
    barrier = utcnow()
    service.ingest_observation(observation(count=2, processes=[]))
    stopped = service.finalize_keepalive_stop(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        observation_not_before=barrier,
        idempotency_key="stop-once-foreign-process-is-gone",
    )
    assert stopped["keepalive"]["state"] == "RELEASED"


def test_keepalive_lease_is_cleared_immediately_without_a_liveness_window(service, admin) -> None:
    """ServerPilot's own hold is the operator's to take back at once.

    The liveness window protects somebody else's claim from being cleared while
    they are still working. A keepalive lease has no such holder -- the person
    clicking is the instance that placed it -- and making them wait is what
    wedges a card nobody else can free. A workload lease in exactly this shape
    is refused with ``lease_holder_recently_alive`` (see
    ``test_manual_release_refuses_a_workload_lease_whose_holder_was_just_alive``);
    this lease is issued seconds ago and cleared anyway.

    Only that window is waived. The card still has to show no process, which
    here means the worker's last sighting has aged out as well.
    """

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="immediate-clear-policy-disabled",
    )

    age_out_processes(service)
    barrier = utcnow()
    service.ingest_observation(observation(count=2, processes=[]))
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        observation_not_before=barrier,
        idempotency_key="immediate-keepalive-clear",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
