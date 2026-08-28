from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from serverpilot import API_CAPABILITIES, mcp_server
from serverpilot import cli as cli_module
from serverpilot.api import RateLimiter, RequestBodyLimitMiddleware
from serverpilot.cli import app as cli_app
from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig
from serverpilot.mcp_server import ROUTINE_MCP_TOOL_NAMES, mcp
from serverpilot.models import GPUDevice
from serverpilot.schemas import RequestCreate
from serverpilot.service import BrokerError
from serverpilot.timeutil import utcnow
from tests.helpers import observation, process_for_gpu, tools


def test_rate_limiter_serializes_concurrent_checks() -> None:
    limiter = RateLimiter(1)

    def check() -> bool:
        try:
            limiter.check("same-actor")
        except BrokerError as exc:
            assert exc.code == "rate_limited"
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda _index: check(), range(32)))

    assert results.count(True) == 1


def test_actual_request_body_limit_rejects_stream_without_content_length(build_app) -> None:
    app = build_app("body-limit", request_body_limit_bytes=64)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/routine/claims",
        content=iter([b'{"project_id":"project-a",', b'"padding":"' + b"x" * 128 + b'"}']),
        headers={
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
            "X-ServerPilot-Actor": "body-agent",
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_request_body_limit_forwards_disconnect_after_replaying_body() -> None:
    observed: list[dict[str, object]] = []
    incoming = iter(
        [
            {"type": "http.request", "body": b"payload", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict[str, object]:
        return next(incoming)

    async def downstream(scope, replay_receive, send) -> None:  # type: ignore[no-untyped-def]
        observed.append(await replay_receive())
        observed.append(await replay_receive())

    async def send(_message: dict[str, object]) -> None:
        return None

    asyncio.run(
        RequestBodyLimitMiddleware(downstream, max_bytes=64)(
            {"type": "http", "headers": []}, receive, send
        )
    )

    assert observed == [
        {"type": "http.request", "body": b"payload", "more_body": False},
        {"type": "http.disconnect"},
    ]


def test_api_gui_and_idempotency(build_app) -> None:
    app = build_app("api")
    service = app.state.service
    service.ingest_observation(observation(count=1))
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "test-agent", "Idempotency-Key": "api-key"}
    payload = {
        "project_id": "project-a",
        "task_ref": "api-request",
        "purpose": "API test",
        "constraints": {
            "gpu_count": 1,
            "min_available_cpu_cores": 16,
            "min_available_memory_mib": 64 * 1024,
            "min_free_vram_mib": 60 * 1024,
            "min_total_vram_mib": 80 * 1024,
        },
    }
    first = client.post("/api/v1/claims", json=payload, headers=headers)
    second = client.post("/api/v1/claims", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["request"]["duration_seconds"] == 8 * 60 * 60
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "test-agent"})
    assert snapshot.status_code == 200
    assert snapshot.json()["data"]["gpus"][0]["state"] == "HELD"
    capabilities = client.get("/health/live").json()["capabilities"]
    assert capabilities[: len(API_CAPABILITIES)] == list(API_CAPABILITIES)
    assert "endpoint_deletion" not in capabilities
    assert "server_deletion" not in capabilities
    assert {"endpoint_update", "endpoint_keepalive", "telemetry_recent_averages"}.issubset(capabilities)
    compact = client.get(
        "/api/v1/gpus?compact=true",
        headers={"X-ServerPilot-Actor": "test-agent"},
    )
    assert compact.status_code == 200
    assert "processes" not in compact.json()["data"][0]
    assert compact.json()["data"][0]["owner"] == "test-agent"
    endpoint_history = client.get(
        "/api/v1/endpoints/endpoint-a/history?window_seconds=3600&points=120",
        headers={"X-ServerPilot-Actor": "test-agent"},
    )
    assert endpoint_history.status_code == 200
    assert endpoint_history.json()["data"]["point_count"] <= 120
    invalid_endpoint_history = client.get(
        "/api/v1/endpoints/endpoint-a/history?window_seconds=300",
        headers={"X-ServerPilot-Actor": "test-agent"},
    )
    assert invalid_endpoint_history.status_code == 422
    assert client.get("/").status_code == 404
    assert client.get("/ui/requests").status_code == 404


def test_endpoint_history_omits_absent_and_empty_gpu_series(service, admin) -> None:
    start = utcnow() - timedelta(minutes=10)
    service.ingest_observation(
        observation(
            count=2,
            gpu_uuids=["GPU-old-0", "GPU-old-1"],
            observed_at=start,
        )
    )
    service.ingest_observation(
        observation(
            count=2,
            gpu_uuids=["GPU-new-0", "GPU-new-1"],
            observed_at=start + timedelta(seconds=61),
        )
    )

    def add_present_without_samples(session):  # type: ignore[no-untyped-def]
        now = utcnow()
        session.add(
            GPUDevice(
                id="endpoint-a:GPU-empty",
                endpoint_id="endpoint-a",
                gpu_uuid="GPU-empty",
                gpu_index=9,
                cuda_ordinal=9,
                name="Empty GPU",
                total_vram_mib=80_000,
                labels_json="[]",
                health="OK",
                enabled=True,
                present=True,
                first_seen_at=now,
                last_seen_at=now,
                absent_at=None,
            )
        )

    service._write(add_present_without_samples)
    history = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)
    series = history["data"]["gpu_series"]
    assert [item["gpu_uuid"] for item in series] == ["GPU-new-0", "GPU-new-1"]
    assert [item["label"] for item in series] == ["GPU 0", "GPU 1"]
    assert all(item["points"] for item in series)

    def collide_present_indexes(session):  # type: ignore[no-untyped-def]
        gpu = session.get(GPUDevice, "endpoint-a:GPU-new-1")
        assert gpu is not None
        gpu.gpu_index = 0

    service._write(collide_present_indexes)
    collided = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)
    labels = [item["label"] for item in collided["data"]["gpu_series"]]
    assert labels == ["GPU 0 (GPU-new-0)", "GPU 0 (GPU-new-1)"]


def test_operator_release_can_correct_another_agents_lease_but_generic_release_cannot(
    build_app
) -> None:
    app = build_app("operator-release")
    service = app.state.service
    service.ingest_observation(observation(count=1))
    owner = service.local_actor("lease-owner")
    claimed = service.create_request(
        owner,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "orphaned-app-visible-task",
                "purpose": "orphaned-app-visible-task",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 1, "placement": "pack"},
            }
        ),
        idempotency_key="operator-release-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "human", "Idempotency-Key": "operator-release"}

    forbidden = client.post(
        f"/api/v1/leases/{lease_id}/release",
        headers=headers,
        json={"reason": "desktop release"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "lease_forbidden"

    unauthorized_operator_route = client.post(
        f"/api/v1/operator/leases/{lease_id}/release",
        headers=headers,
        json={"reason": "desktop release"},
    )
    assert unauthorized_operator_route.status_code == 403
    assert unauthorized_operator_route.json()["error"]["code"] == "operator_client_required"

    released = client.post(
        f"/api/v1/operator/leases/{lease_id}/release",
        headers={**headers, "X-ServerPilot-Client": "desktop-app"},
        json={"reason": "desktop release"},
    )
    assert released.status_code == 200
    assert released.json()["lease"]["state"] == "RELEASED"


def test_operator_reassignment_uses_a_separate_app_correction_route(
    build_app
) -> None:
    app = build_app("operator-reassign")
    service = app.state.service
    service.ingest_observation(observation(count=2))
    owner = service.local_actor("reassign-owner")
    claimed = service.create_request(
        owner,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "operator-reassign",
                "purpose": "operator-reassign",
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="operator-reassign-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    target_gpu = next(
        gpu["id"]
        for gpu in service.list_gpus(owner)["data"]
        if gpu["id"] not in lease["gpu_ids"]
    )
    client = TestClient(app)
    headers = {
        "X-ServerPilot-Actor": "other-agent",
        "Idempotency-Key": "operator-reassign",
    }

    missing_app_marker = client.patch(
        f"/api/v1/operator/leases/{lease['id']}/gpus",
        headers=headers,
        json={"gpu_ids": [target_gpu]},
    )
    assert missing_app_marker.status_code == 403
    assert missing_app_marker.json()["error"]["code"] == "operator_client_required"
    moved = client.patch(
        f"/api/v1/operator/leases/{lease['id']}/gpus",
        headers={**headers, "X-ServerPilot-Client": "desktop-app"},
        json={"gpu_ids": [target_gpu]},
    )
    assert moved.status_code == 200
    assert moved.json()["lease"]["actor_id"] == owner.id
    assert moved.json()["lease"]["gpu_ids"] == [target_gpu]


def test_snapshot_api_uses_latest_complete_gpu_set(build_app) -> None:
    app = build_app("latest")
    service = app.state.service
    service.ingest_observation(observation(gpu_uuids=["GPU-old-0", "GPU-old-1", "GPU-stays"]))
    service.ingest_observation(observation(gpu_uuids=["GPU-new-0", "GPU-stays"]))

    client = TestClient(app)
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "test-agent"})

    assert snapshot.status_code == 200
    data = snapshot.json()["data"]
    assert [gpu["id"] for gpu in data["gpus"]] == [
        "endpoint-a:GPU-new-0",
        "endpoint-a:GPU-stays",
    ]
    assert data["summary"]["total_gpus"] == 2
    assert data["endpoints"][0]["monitor"]["gpu_count"] == 2


def test_control_plane_state_api_exposes_current_and_history_contract(
    build_app
) -> None:
    app = build_app("state-contract")
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)

    response = client.get("/api/v1/state", headers={"X-ServerPilot-Actor": "test-agent"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "v1"
    assert isinstance(payload["snapshot_revision"], int)
    assert payload["server_time"]
    assert set(payload["data"]) == {"current", "history"}
    current = payload["data"]["current"]
    history = payload["data"]["history"]
    assert {
        "summary",
        "endpoints",
        "gpus",
        "leases",
        "requests",
        "reservations",
        "host_capacity",
    }.issubset(current)
    assert history == {}
    assert "resource_providers" not in current
    assert "workload_profiles" not in current
    assert "scheduler_targets" not in current
    assert current["gpus"][0]["state"] == "AVAILABLE"


def test_lease_api_suppresses_executable_resources_when_claimed_gpu_absent(
    build_app
) -> None:
    app = build_app("lease-presence")
    service = app.state.service
    actor = service.local_actor("test-agent")
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    service.create_request(
        actor,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "api-two-gpu",
                "purpose": "API resource suppression test",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 2, "placement": "pack"},
            }
        ),
        idempotency_key="api-two-gpu",
    )
    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))

    client = TestClient(app)
    leases = client.get("/api/v1/leases", headers={"X-ServerPilot-Actor": "test-agent"})

    assert leases.status_code == 200
    lease = leases.json()["data"][0]
    assert set(lease["gpu_ids"]) == {"endpoint-a:GPU-old", "endpoint-a:GPU-new"}
    assert lease["absent_gpu_ids"] == ["endpoint-a:GPU-old"]
    assert lease["resources"] == []


def test_api_claim_starts_held_without_a_duration_estimate(build_app) -> None:
    app = build_app("claim")
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "s",
            "task_ref": "api-claim",
            "purpose": "api-claim",
            "constraints": {"gpu_count": 1},
        },
        headers={"X-ServerPilot-Actor": "claim-agent", "Idempotency-Key": "api-claim"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["request"]["state"] == "LEASED"
    assert claimed.json()["lease"]["state"] == "HELD"
    assert claimed.json()["lease"]["project_id"] == "s"
    assert claimed.json()["request"]["duration_seconds"] == 8 * 60 * 60

    request_route = client.post(
        "/api/v1/requests",
        json={
            "project_id": "s",
            "task_ref": "request-route-no-capacity",
            "purpose": "request-route-no-capacity",
            "constraints": {"gpu_count": 1},
        },
        headers={"X-ServerPilot-Actor": "claim-agent", "Idempotency-Key": "request-route"},
    )
    assert request_route.status_code == 409
    assert request_route.json()["error"]["code"] == "no_capacity"

    cancel_route = client.post(
        "/api/v1/requests/missing-request/cancel",
        headers={"X-ServerPilot-Actor": "claim-agent", "Idempotency-Key": "cancel-route"},
    )
    assert cancel_route.status_code == 404
    assert cancel_route.json()["error"]["code"] == "request_not_found"


def test_api_claim_bootstraps_an_empty_project_registry(build_app) -> None:
    inventory = InventoryConfig(
        schema_version=1,
        endpoints=[
            EndpointConfig(
                id="endpoint-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
                workspace_path="/srv/project-a",
            )
        ],
    )
    app = build_app("empty-projects", inventory_config=inventory)
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)

    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "x",
            "task_ref": "unregistered-project",
            "purpose": "unregistered-project",
            "constraints": {"gpu_count": 1},
        },
        headers={"X-ServerPilot-Actor": "claim-agent", "Idempotency-Key": "claim-empty-projects"},
    )

    assert claimed.status_code == 200
    assert claimed.json()["lease"]["project_id"] == "x"


def test_coordination_api_and_observed_binding(build_app) -> None:
    app = build_app("coordination")
    service = app.state.service
    service.ingest_observation(observation(count=1))
    client = TestClient(app)
    claim_headers = {
        "X-ServerPilot-Actor": "coordination-agent",
        "Idempotency-Key": "coordination-claim",
    }
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "coordination-api-run",
            "purpose": "coordination-api-run",
            "constraints": {"gpu_count": 1},
        },
        headers=claim_headers,
    )
    assert claimed.status_code == 200
    lease_id = claimed.json()["lease"]["id"]
    gpu = service.list_gpus(service.local_actor("coordination-agent"))["data"][0]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu["gpu_uuid"])]))

    bound = client.post(
        f"/api/v1/leases/{lease_id}/bind-observed-workload",
        json={},
        headers={
            "X-ServerPilot-Actor": "coordination-agent",
            "Idempotency-Key": "coordination-bind",
        },
    )
    assert bound.status_code == 200
    assert bound.json()["lease"]["workloads"][0]["run_id"] == f"explicit:lease:{lease_id}"
    current = client.get(
        "/api/v1/state",
        headers={"X-ServerPilot-Actor": "coordination-agent"},
    ).json()["data"]["current"]
    host = current["host_capacity"][0]["capacity"]
    assert host["observed_available_cpu_cores"] == 60.0
    assert host["observed_available_memory_mib"] == 196_608
    assert current["gpus"][0]["total_vram_mib"] == 100_000
    assert current["gpus"][0]["state"] == "RUNNING_MANAGED"
    assert current["leases"][0]["state"] == "ACTIVE"


def test_endpoint_project_grant_route_is_not_exposed(build_app) -> None:
    app = build_app("endpoint-project")
    client = TestClient(app)
    response = client.post(
        "/api/v1/endpoints/endpoint-a/projects",
        json={"project_id": "storyboard"},
        headers={"X-ServerPilot-Actor": "endpoint-admin", "Idempotency-Key": "unused"},
    )
    assert response.status_code == 404


def test_collector_observation_ingestion_is_not_a_public_actor_route(
    build_app
) -> None:
    app = build_app("collector-private")
    client = TestClient(app)
    response = client.post(
        "/api/v1/internal/observations",
        json=observation(count=1).model_dump(mode="json"),
        headers={"X-ServerPilot-Actor": "arbitrary-actor"},
    )
    assert response.status_code == 404


def test_endpoint_delete_rest_route_removes_idle_endpoint_and_rejects_active_leases(
    build_app
) -> None:
    app = build_app("endpoint-delete")
    client = TestClient(app)
    actor = {"X-ServerPilot-Actor": "endpoint-admin"}
    created = client.post(
        "/api/v1/endpoints",
        json={
            "id": "endpoint-delete-me",
            "host": "127.0.0.1",
            "port": 2297,
            "ssh_user": "gpu",
            "workspace_path": "/srv/endpoint-delete-me",
        },
        headers={**actor, "Idempotency-Key": "endpoint-delete-create"},
    )
    assert created.status_code == 200
    deleted = client.delete(
        "/api/v1/endpoints/endpoint-delete-me",
        headers={**actor, "Idempotency-Key": "endpoint-delete"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["changed"] is True
    assert deleted.json()["endpoint_id"] == "endpoint-delete-me"
    listed = client.get("/api/v1/endpoints", headers=actor)
    endpoints = {endpoint["id"]: endpoint for endpoint in listed.json()["data"]}
    assert "endpoint-delete-me" not in endpoints
    assert "endpoint-b" in endpoints

    service = app.state.service
    service.ingest_observation(observation(endpoint_id="endpoint-a", count=1))
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "keep-endpoint-a",
            "purpose": "block delete while leased",
            "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-a"]},
        },
        headers={**actor, "Idempotency-Key": "endpoint-a-claim"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["lease"] is not None
    blocked = client.delete(
        "/api/v1/endpoints/endpoint-a",
        headers={**actor, "Idempotency-Key": "endpoint-a-delete-blocked"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "endpoint_has_active_leases"
    still_listed = client.get("/api/v1/endpoints", headers=actor)
    assert "endpoint-a" in {endpoint["id"] for endpoint in still_listed.json()["data"]}


def test_endpoint_rest_uses_explicit_create_and_update_without_delete(
    build_app
) -> None:
    app = build_app("endpoint-lifecycle")
    client = TestClient(app)
    actor = {"X-ServerPilot-Actor": "endpoint-admin"}
    endpoint = {
        "id": "endpoint-lifecycle",
        "host": "127.0.0.1",
        "port": 2399,
        "ssh_user": "gpu",
        "workspace_path": "/srv/endpoint-lifecycle",
    }
    created = client.post(
        "/api/v1/endpoints", json=endpoint, headers={**actor, "Idempotency-Key": "endpoint-create"}
    )
    assert created.status_code == 200
    assert created.json()["endpoint"]["lifecycle_state"] == "active"
    assert created.json()["endpoint"]["observation_profile"] == "linux-nvidia"
    assert created.json()["endpoint"]["workspace_path"] == "/srv/endpoint-lifecycle"
    duplicate = client.post(
        "/api/v1/endpoints",
        json=endpoint,
        headers={**actor, "Idempotency-Key": "endpoint-create-new"},
    )
    assert duplicate.status_code == 409
    identity_patch = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"host": "127.0.0.2"},
        headers={**actor, "Idempotency-Key": "endpoint-host-change"},
    )
    assert identity_patch.status_code == 422
    updated = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"ssh_alias": "lab-script", "labels": ["lab"]},
        headers={**actor, "Idempotency-Key": "endpoint-update"},
    )
    assert updated.status_code == 200
    assert updated.json()["endpoint"]["ssh_alias"] == "lab-script"
    workspace_updated = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"workspace_path": "/srv/endpoint-lifecycle-updated"},
        headers={**actor, "Idempotency-Key": "endpoint-workspace-update"},
    )
    assert workspace_updated.status_code == 200
    assert (
        workspace_updated.json()["endpoint"]["workspace_path"] == "/srv/endpoint-lifecycle-updated"
    )
    later_update = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"ssh_user": "other"},
        headers={**actor, "Idempotency-Key": "endpoint-update-later"},
    )
    assert later_update.status_code == 200


def test_removed_maintenance_and_delete_routes(build_app) -> None:
    app = build_app("endpoint-delete-error")
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "human", "Idempotency-Key": "endpoint-maintenance"}
    created = client.post(
        "/api/v1/maintenance",
        json={
            "endpoint_id": "endpoint-b",
            "start_at": "2026-07-20T00:00:00+00:00",
            "end_at": "2026-07-20T01:00:00+00:00",
            "reason": "hardware inspection",
        },
        headers=headers,
    )
    assert created.status_code == 404


def test_project_creation_route_and_gui_are_not_exposed(build_app) -> None:
    app = build_app("no-project-admin")
    client = TestClient(app)

    response = client.post(
        "/api/v1/projects",
        json={"id": "storyboard", "display_name": "Storyboard"},
        headers={"X-ServerPilot-Actor": "project-admin", "Idempotency-Key": "unused"},
    )
    assert response.status_code == 404
    assert client.get("/ui/projects").status_code == 404
    assert client.get("/ui/identities").status_code == 404




def test_mcp_exposes_required_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    names = set(by_name)
    assert names == {
        "gpu_status",
        "gpu_apply",
        "gpu_release",
        "gpu_add_server",
        "gpu_update_server",
    }
    assert "gpu_grant_server_project" not in names
    assert "gpu_scheduler_upload" not in names
    assert "gpu_scheduler_transfer_status" not in names
    for retired_routine_tool in (
        "control_plane_state",
        "gpu_coordination",
        "gpu_list",
        "gpu_who",
        "gpu_activate_lease",
        "gpu_renew_lease",
        "gpu_release_lease",
        "gpu_bind_workload",
        "gpu_bind_observed_workload",
        "gpu_history",
        "gpu_list_observation_profiles",
        "gpu_set_keepalive",
        "gpu_list_profiles",
        "gpu_claim",
        "gpu_claim_profile",
    ):
        assert retired_routine_tool not in names
    apply_schema = by_name["gpu_apply"].inputSchema
    assert "required" not in apply_schema
    assert {"server_group_id", "server_id", "gpu_count", "task"} == set(apply_schema["properties"])
    assert apply_schema["properties"]["gpu_count"]["default"] == 1
    assert "project_id" not in apply_schema["properties"]
    assert "idempotency_key" not in apply_schema["properties"]
    assert "profile_id" not in apply_schema["properties"]
    assert "gpu_ids" not in apply_schema["properties"]
    assert by_name["gpu_release"].inputSchema["required"] == ["lease_id"]
    assert set(by_name["gpu_release"].inputSchema["properties"]) == {"lease_id"}
    status_schema = by_name["gpu_status"].inputSchema
    assert set(status_schema["properties"]) == {"server_id", "lease_id"}
    assert status_schema["properties"]["lease_id"]["default"] is None
    assert "required" not in status_schema
    assert by_name["gpu_status"].description == (
        "List grouped allocatable GPU capacity, busy_gpus and who holds them, "
        "CPU-only servers, and scheduler clusters you can request on demand; "
        "pass lease_id for per-card telemetry on cards you hold."
    )
    add_schema = by_name["gpu_add_server"].inputSchema
    # A server registered without saying what it is is a GPU host. The old
    # default named the one profile that cannot work on a plain NVIDIA box --
    # the host-carries-its-own-script contract -- so a caller that omitted the
    # field registered a machine that connects and reports no GPUs.
    assert add_schema["properties"]["observation_profile"]["default"] == "linux-nvidia"
    add_profile = add_schema["properties"]["observation_profile"]["description"]
    assert "linux-nvidia" in add_profile
    assert "linux-host" in add_profile
    assert "server-script-v1" in add_profile
    assert "plugin" in add_profile.lower()
    # Both tools must be able to place a host in a group. The domain and REST
    # have carried server_group_id since grouping existed; MCP exposed it on
    # neither tool, so a server registered by an agent could never be selected
    # by gpu_apply(server_group_id=...) -- the tool could create a server, but
    # never a usable one.
    for tool_name in ("gpu_add_server", "gpu_update_server"):
        group_property = by_name[tool_name].inputSchema["properties"]["server_group_id"]
        assert "gpu_apply" in group_property["description"], tool_name
    assert "server_group_id" in by_name["gpu_add_server"].description
    # The two administration tools must be callable exactly as the instructions
    # describe them. They used to demand agent_name / approval_ref /
    # idempotency_key, none of which the instructions teach — and two of which
    # the agent policy asserts must never be taught — so an agent following the
    # documented contract could only ever get a TypeError.
    assert by_name["gpu_add_server"].inputSchema["required"] == [
        "project_id",
        "host",
        "workspace_path",
    ]
    assert by_name["gpu_update_server"].inputSchema["required"] == ["server_id"]
    for name in ("gpu_add_server", "gpu_update_server"):
        properties = by_name[name].inputSchema["properties"]
        for withdrawn in ("agent_name", "approval_ref", "idempotency_key"):
            assert withdrawn not in properties, (name, withdrawn)


def test_default_stdio_mcp_uses_intent_first_routine_surface() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    names = set(by_name)

    assert names == set(ROUTINE_MCP_TOOL_NAMES)
    assert "control_plane_state" not in names
    assert "resource_claim" not in names
    assert "gpu_claim" not in names
    assert "gpu_list" not in names
    assert "gpu_list_observation_profiles" not in names
    assert not any(name.startswith("gpu_scheduler_") for name in names)
    assert names == {
        "gpu_status",
        "gpu_apply",
        "gpu_release",
        "gpu_add_server",
        "gpu_update_server",
    }
    assert by_name["gpu_status"].description == (
        "List grouped allocatable GPU capacity, busy_gpus and who holds them, "
        "CPU-only servers, and scheduler clusters you can request on demand; "
        "pass lease_id for per-card telemetry on cards you hold."
    )


def test_mcp_endpoint_administration_uses_rest_with_its_own_replay_key(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    class FakeClient:
        def post(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("POST", path, body, idempotency_key))
            return {"endpoint": {"id": "server-a"}}

        def patch(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("PATCH", path, body, idempotency_key))
            return {"endpoint": {"id": "server-a"}}

        def delete(self, path, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("DELETE", path, None, idempotency_key))
            return {"endpoint_id": "server-a"}

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())

    created = tools.gpu_add_server(
        "project-a",
        "10.0.0.8",
        "/srv/server-a",
        server_id="server-a",
    )
    assert created["endpoint"]["id"] == "server-a"
    assert calls[-1] == (
        "POST",
        "/api/v1/endpoints",
        {
            "id": "server-a",
            "host": "10.0.0.8",
            "port": 22,
            "ssh_user": "root",
            "ssh_alias": None,
            "workspace_path": "/srv/server-a",
            "server_group_id": None,
            "observation_profile": "linux-nvidia",
            "labels": [],
            "storage_group": None,
            "expected_gpu_count": None,
            "expected_gpu_total_vram_mib": None,
            "owner_project_id": "project-a",
        },
        calls[-1][3],
    )
    assert calls[-1][3], "the tool supplies its own replay key"
    tools.gpu_update_server(
        "server-a",
        ssh_user="gpu",
        workspace_path="/srv/server-a-updated",
    )
    assert calls[1][0:3] == (
        "PATCH",
        "/api/v1/endpoints/server-a",
        {"ssh_user": "gpu", "workspace_path": "/srv/server-a-updated"},
    )
    assert calls[1][3]
    assert calls[1][3] != calls[0][3], "each call carries its own key"


def test_mcp_common_tools_do_not_preflight_health(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    class FakeClient:
        def post(self, path, body=None, *, idempotency_key=None):  # type: ignore[no-untyped-def]
            calls.append(("POST", path, body))
            return {
                "lease": {
                    "id": "lease-a",
                    "resources": [
                        {
                            "endpoint": {"id": "server-a", "workspace_path": "/srv/server-a"},
                            "gpus": [
                                {"gpu_uuid": "GPU-a", "gpu_index": 7, "cuda_ordinal": 0},
                                {"gpu_uuid": "GPU-b", "gpu_index": 3, "cuda_ordinal": 1},
                            ],
                            "cuda_visible_devices": "0,1",
                            "cuda_device_order": "PCI_BUS_ID",
                        }
                    ],
                }
            }

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())
    monkeypatch.setattr(
        mcp_server,
        "_routine_client",
        lambda: FakeClient(),
    )
    result = tools.gpu_apply(server_id="server-a", gpu_count=2, task="训练")
    workspace = {
        "path": "/srv/server-a",
        "kind": "working_directory",
        "use_as_cwd": True,
        "code_location": "not_provided",
    }
    server_projection = {
        "server_id": "server-a",
        "workspace_path": "/srv/server-a",
        "workspace": workspace,
        "cuda_visible_devices": "0,1",
        "cuda_device_order": "PCI_BUS_ID",
    }
    assert result == {
        "lease_id": "lease-a",
        "servers": [server_projection],
        "server_id": "server-a",
        "workspace_path": "/srv/server-a",
        "workspace": workspace,
        "cuda_visible_devices": "0,1",
        "cuda_device_order": "PCI_BUS_ID",
        "gpus": [
            {
                "server_id": "server-a",
                "gpu_id": "GPU-a",
                "gpu_index": 7,
                "cuda_ordinal": 0,
                "gpu_cuda_visible_devices": "0",
            },
            {
                "server_id": "server-a",
                "gpu_id": "GPU-b",
                "gpu_index": 3,
                "cuda_ordinal": 1,
                "gpu_cuda_visible_devices": "1",
            },
        ],
    }
    assert [call[:2] for call in calls] == [("POST", "/api/v1/routine/claims")]
    _method, _path, body = calls[0]
    assert body["constraints"] == {
        "gpu_count": 2,
        "placement": "pack",
        "same_host": True,
        "endpoint_ids": ["server-a"],
    }
    assert body["project_id"] == "agent"
    assert body["purpose"] == "训练"
    assert body["task_ref"] == "训练"


def test_routine_gpu_allocation_projects_single_gpu_and_multiple_endpoints() -> None:
    workspace_a = {
        "path": "/srv/server-a",
        "kind": "working_directory",
        "use_as_cwd": True,
        "code_location": "not_provided",
    }
    single = mcp_server._routine_gpu_allocation(
        {
            "lease": {
                "id": "lease-single",
                "resources": [
                    {
                        "endpoint": {
                            "id": "server-a",
                            "workspace_path": "/srv/server-a",
                        },
                        "gpus": [
                            {"gpu_uuid": "GPU-a", "gpu_index": 7, "cuda_ordinal": 0}
                        ],
                        "cuda_visible_devices": "0",
                        "cuda_device_order": "PCI_BUS_ID",
                    }
                ],
            }
        }
    )
    server_a = {
        "server_id": "server-a",
        "workspace_path": "/srv/server-a",
        "workspace": workspace_a,
        "cuda_visible_devices": "0",
        "cuda_device_order": "PCI_BUS_ID",
    }
    assert single == {
        "lease_id": "lease-single",
        "servers": [server_a],
        "server_id": "server-a",
        "workspace_path": "/srv/server-a",
        "workspace": workspace_a,
        "cuda_visible_devices": "0",
        "cuda_device_order": "PCI_BUS_ID",
        "gpus": [
            {
                "server_id": "server-a",
                "gpu_id": "GPU-a",
                "gpu_index": 7,
                "cuda_ordinal": 0,
                "gpu_cuda_visible_devices": "0",
            }
        ],
    }

    multiple_endpoints = mcp_server._routine_gpu_allocation(
        {
            "lease": {
                "id": "lease-spread",
                "resources": [
                    {
                        "endpoint": {
                            "id": "server-a",
                            "workspace_path": "/srv/server-a",
                        },
                        "gpus": [
                            {"gpu_uuid": "GPU-a", "gpu_index": 7, "cuda_ordinal": 0}
                        ],
                        "cuda_visible_devices": "0",
                        "cuda_device_order": "PCI_BUS_ID",
                    },
                    {
                        "endpoint": {
                            "id": "server-b",
                            "workspace_path": "/srv/server-b",
                        },
                        "gpus": [
                            {"gpu_uuid": "GPU-b", "gpu_index": 3, "cuda_ordinal": 0}
                        ],
                        "cuda_visible_devices": "0",
                        "cuda_device_order": "PCI_BUS_ID",
                    },
                ],
            }
        }
    )
    # A spread lease keeps every server reachable through servers[]; it must not
    # publish an ambiguous top-level server, SSH, workspace or CUDA selector.
    assert multiple_endpoints == {
        "lease_id": "lease-spread",
        "servers": [
            server_a,
            {
                "server_id": "server-b",
                "workspace_path": "/srv/server-b",
                "workspace": {
                    "path": "/srv/server-b",
                    "kind": "working_directory",
                    "use_as_cwd": True,
                    "code_location": "not_provided",
                },
                "cuda_visible_devices": "0",
                "cuda_device_order": "PCI_BUS_ID",
            },
        ],
        "gpus": [
            {
                "server_id": "server-a",
                "gpu_id": "GPU-a",
                "gpu_index": 7,
                "cuda_ordinal": 0,
                "gpu_cuda_visible_devices": "0",
            },
            {
                "server_id": "server-b",
                "gpu_id": "GPU-b",
                "gpu_index": 3,
                "cuda_ordinal": 0,
                "gpu_cuda_visible_devices": "0",
            },
        ],
    }
    assert "ssh" not in multiple_endpoints
    assert "cuda_visible_devices" not in multiple_endpoints


def test_routine_projects_registered_ssh_connection_without_a_shell_command() -> None:
    endpoint = {
        "id": "server-a",
        "host": "gpu.example.test",
        "port": 2201,
        "ssh_user": "gpu",
        "workspace_path": "/srv/server-a",
    }
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [endpoint],
                "gpus": [
                    {
                        "endpoint_id": "server-a",
                        "gpu_uuid": "GPU-a",
                        "gpu_index": 0,
                        "name": "A800",
                        "total_vram_mib": 80_000,
                        "state": "AVAILABLE",
                        "publicly_available": True,
                        "public_status": "可用 · 未开启占卡",
                    }
                ],
            }
        },
        lease_id=None,
    )
    # Connection and workspace belong to the server, not to each GPU.
    assert status["ungrouped_servers"] == [
        {
            "server_id": "server-a",
            "workspace_path": "/srv/server-a",
            "workspace": {
                "path": "/srv/server-a",
                "kind": "working_directory",
                "use_as_cwd": True,
                "code_location": "not_provided",
            },
            "ssh": {"host": "gpu.example.test", "port": 2201, "user": "gpu"},
            "gpus": [
                {
                    "name": "A800",
                    "vram_mib": 80_000,
                    "total_count": 1,
                    "available_count": 1,
                }
            ],
        }
    ]
    assert "gpus" not in status
    for duplicated in ("ssh", "workspace", "workspace_path"):
        assert duplicated not in status["ungrouped_servers"][0]["gpus"][0]
    assert "ssh_command" not in status["ungrouped_servers"][0]

    allocation = mcp_server._routine_gpu_allocation(
        {
            "lease": {
                "id": "lease-a",
                "resources": [
                    {
                        "endpoint": endpoint,
                        "gpus": [
                            {"gpu_uuid": "GPU-a", "gpu_index": 7, "cuda_ordinal": 0}
                        ],
                        "cuda_visible_devices": "0",
                        "cuda_device_order": "PCI_BUS_ID",
                    }
                ],
            }
        }
    )
    expected_ssh = {"host": "gpu.example.test", "port": 2201, "user": "gpu"}
    assert allocation["ssh"] == expected_ssh
    assert allocation["servers"][0]["ssh"] == expected_ssh
    assert "ssh" not in allocation["gpus"][0]
    assert "ssh_command" not in allocation


def test_mcp_default_task_is_harness_neutral() -> None:
    assert mcp_server._routine_task(None) == "unnamed task"


def test_mcp_status_separates_capacity_from_the_callers_own_telemetry(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        def snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
            # Busy cards are filtered in the projection so the default response
            # can name their tasks; the broker is never asked to pre-filter.
            assert kwargs == {
                "compact": False,
                "endpoint_id": None,
                "only_available": False,
            }
            return {
                "schema_version": "v1",
                "snapshot_revision": 9,
                "server_time": "2026-08-12T00:00:00Z",
                "data": {
                    "summary": {
                        "total_gpus": 2,
                        "available_gpus": 1,
                        "claimed_gpus": 1,
                        "workload_claimed_gpus": 1,
                        "keepalive_owned_gpus": 1,
                        "verified_keepalive_gpus": 1,
                    },
                    "data_age_seconds": 1.2,
                    "freshness_seconds": 30,
                    "endpoints": [
                        {
                            "id": "server-a",
                            "workspace_path": "/srv/serverpilot-workspace",
                            "monitor": {"status": "ONLINE", "last_error": None},
                            "private_detail": "must not cross MCP boundary",
                        }
                    ],
                    "gpus": [
                        {
                            "id": "gpu-a",
                            "endpoint_id": "server-a",
                            "gpu_uuid": "GPU-a",
                            "gpu_index": 0,
                            "name": "A",
                            "total_vram_mib": 80_000,
                            "state": "KEEPALIVE",
                            "lease": None,
                            "publicly_available": True,
                            "public_status": "可用 · 空闲占卡",
                            "keepalive": {"state": "ON", "reason": None, "lease_id": "ka-1"},
                            # ServerPilot's own hold: 80% of the card, released
                            # before allocation.  It must not reach the caller.
                            "telemetry": {
                                "observed_at": "2026-08-12T00:00:00Z",
                                "memory_used_mib": 64_000,
                                "memory_free_mib": 16_000,
                                "gpu_utilization_pct": 66,
                                "memory_utilization_pct": 11,
                                "temperature_c": 58,
                            },
                            "processes": ["drop"],
                        },
                        {
                            "id": "gpu-b",
                            "endpoint_id": "server-a",
                            "gpu_uuid": "GPU-b",
                            "gpu_index": 1,
                            "name": "A",
                            "total_vram_mib": 80_000,
                            "state": "RUNNING_MANAGED",
                            "lease": {"id": "lease-mine", "task_ref": "训练"},
                            "publicly_available": False,
                            "public_status": "任务占用",
                            "keepalive": {"state": "OFF", "reason": None, "lease_id": None},
                            "telemetry": {
                                "observed_at": "2026-08-12T00:00:00Z",
                                "memory_used_mib": 25_000,
                                "memory_free_mib": 55_000,
                                "gpu_utilization_pct": 93,
                                "memory_utilization_pct": 27,
                                "temperature_c": 75,
                                "private": "drop",
                                "lease_recent_average": {
                                    "window_seconds": 600,
                                    "sample_count": 10,
                                    "first_observed_at": "2026-08-11T23:50:00Z",
                                    "last_observed_at": "2026-08-12T00:00:00Z",
                                    "memory_used_mib": 24_000,
                                    "memory_free_mib": 56_000,
                                    "memory_used_pct": 30.0,
                                    "gpu_utilization_pct": 91.0,
                                    "memory_utilization_pct": 26.0,
                                    "temperature_c": 74.0,
                                },
                            },
                            "processes": ["drop"],
                        },
                    ],
                    "resource_claims": [{"id": "drop"}],
                    "scheduler_jobs": [{"id": "drop"}],
                },
            }

    monkeypatch.setattr(
        mcp_server,
        "_routine_client",
        lambda: FakeClient(),
    )
    server_projection = {
        "server_id": "server-a",
        "workspace_path": "/srv/serverpilot-workspace",
        "workspace": {
            "path": "/srv/serverpilot-workspace",
            "kind": "working_directory",
            "use_as_cwd": True,
            "code_location": "not_provided",
        },
        "gpus": [
            {
                "name": "A",
                "vram_mib": 80_000,
                "total_count": 2,
                "available_count": 1,
            }
        ],
    }

    status = tools.gpu_status()
    # Allocatable capacity is a per-server SKU summary, never one free row per
    # card.  The load observed on an unclaimed card is ServerPilot's own
    # keepalive hold, and publishing that would turn a free card into a card
    # that reads as full.
    assert status == {
        "ungrouped_servers": [server_projection],
        "busy_gpus": [
            {
                "server_id": "server-a",
                "gpu_id": "GPU-b",
                "index": 1,
                "status": "running",
                "task": "训练",
            }
        ],
    }
    assert "gpus" not in status
    assert "64000" not in json.dumps(status)

    # The caller's own lease is the one place occupancy provably belongs to the
    # reader, so it is the one place telemetry is published.
    mine = tools.gpu_status(lease_id="lease-mine")
    assert "busy_gpus" not in mine
    assert mine["lease"] == {
        "lease_id": "lease-mine",
        "gpu_count": 1,
        "task": "训练",
        "telemetry_window": {
            "window_seconds": 600,
            "sample_count": 10,
            "first_observed_at": "2026-08-11T23:50:00Z",
            "last_observed_at": "2026-08-12T00:00:00Z",
        },
        "telemetry_gpu_count": 1,
        "recent_average": {
            "gpu_utilization_pct": 91.0,
            "memory_used_pct": 30.0,
            "memory_utilization_pct": 26.0,
            "min_memory_free_mib": 56_000,
        },
    }
    assert mine["leased_gpus"] == [
        {
            "server_id": "server-a",
            "gpu_id": "GPU-b",
            "index": 1,
            "name": "A",
            "vram_mib": 80_000,
            "recent_average": {
                "memory_used_mib": 24_000,
                "memory_free_mib": 56_000,
                "memory_used_pct": 30.0,
                "gpu_utilization_pct": 91.0,
                "memory_utilization_pct": 26.0,
                "temperature_c": 74.0,
            },
            "current": {
                "observed_at": "2026-08-12T00:00:00Z",
                "memory_used_mib": 25_000,
                "memory_free_mib": 55_000,
                "memory_used_pct": 31.25,
                "gpu_utilization_pct": 93,
                "memory_utilization_pct": 27,
                "temperature_c": 75,
            },
        }
    ]
    # Free capacity stays aggregated even when the caller names a lease.
    assert mine["ungrouped_servers"] == status["ungrouped_servers"]

    unknown = tools.gpu_status(lease_id="lease-gone")
    assert "leased_gpus" not in unknown
    assert unknown["no_leased_gpus"]["reason"] == "lease_holds_no_visible_gpu"
    # Somebody else's lease is still only named, never measured.
    assert unknown["busy_gpus"] == status["busy_gpus"]


def test_routine_status_projects_per_gpu_recent_telemetry_average() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [{"id": "server-a", "workspace_path": "/srv/server-a"}],
                "gpus": [
                    {
                        "endpoint_id": "server-a",
                        "gpu_uuid": "GPU-a",
                        "gpu_index": 0,
                        "name": "A800",
                        "total_vram_mib": 80_000,
                        "state": "RUNNING_MANAGED",
                        "publicly_available": False,
                        "public_status": "任务占用",
                        "lease": {"id": "lease-mine", "task_ref": "训练"},
                        "telemetry": {
                            "memory_used_mib": 4_000,
                            "lease_recent_average": {
                                "window_seconds": 3_600,
                                "sample_count": 3,
                                "first_observed_at": "2026-08-15T00:00:00Z",
                                "last_observed_at": "2026-08-15T00:02:00Z",
                                "memory_used_mib": 12_000.5,
                                "memory_free_mib": 67_999.5,
                                "memory_used_pct": 15.0,
                                "gpu_utilization_pct": 47.33,
                                "memory_utilization_pct": 21.0,
                                "temperature_c": 54.67,
                                "private": "drop",
                            },
                        },
                    }
                ],
            }
        },
        lease_id="lease-mine",
    )

    # Measurements stay per GPU; the window descriptor they all share is
    # published once on the lease that owns them.
    assert status["leased_gpus"][0]["recent_average"] == {
        "memory_used_mib": 12_000.5,
        "memory_free_mib": 67_999.5,
        "memory_used_pct": 15.0,
        "gpu_utilization_pct": 47.33,
        "memory_utilization_pct": 21.0,
        "temperature_c": 54.67,
    }
    assert status["lease"]["telemetry_window"] == {
        "window_seconds": 3_600,
        "sample_count": 3,
        "first_observed_at": "2026-08-15T00:00:00Z",
        "last_observed_at": "2026-08-15T00:02:00Z",
    }
    assert "window_override" not in status["leased_gpus"][0]
    # A single-GPU lease has no laggard to name.
    assert "slowest_gpu" not in status["lease"]


def test_routine_status_names_the_gpu_holding_a_multi_gpu_lease_back() -> None:
    """Averaging the spread away would hide the case worth acting on."""

    def gpu(uuid: str, index: int, utilization: float, memory_free_mib: int) -> dict[str, object]:
        return {
            "endpoint_id": "server-a",
            "gpu_uuid": uuid,
            "gpu_index": index,
            "name": "A800",
            "total_vram_mib": 80_000,
            "state": "RUNNING_MANAGED",
            "publicly_available": False,
            "public_status": "任务占用",
            "lease": {"id": "lease-mine", "task_ref": "训练"},
            "telemetry": {
                "observed_at": "2026-08-15T00:02:00Z",
                "lease_recent_average": {
                    "window_seconds": 600,
                    "sample_count": 10,
                    "first_observed_at": "2026-08-15T00:00:00Z",
                    "last_observed_at": "2026-08-15T00:02:00Z",
                    "gpu_utilization_pct": utilization,
                    "memory_free_mib": memory_free_mib,
                },
            },
        }

    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [{"id": "server-a", "workspace_path": "/srv/server-a"}],
                "gpus": [
                    gpu("GPU-a", 0, 92.0, 40_000),
                    gpu("GPU-b", 1, 31.0, 12_000),
                    gpu("GPU-c", 2, 90.0, 38_000),
                ],
            }
        },
        lease_id="lease-mine",
    )

    lease = status["lease"]
    assert lease["gpu_count"] == 3
    assert lease["telemetry_gpu_count"] == 3
    assert lease["gpu_utilization_spread_pct"] == 61.0
    assert lease["slowest_gpu"] == {
        "gpu_id": "GPU-b",
        "index": 1,
        "gpu_utilization_pct": 31.0,
    }
    # The free memory that bounds a larger batch is the smallest one, not the
    # average across the lease.
    assert lease["recent_average"]["min_memory_free_mib"] == 12_000


def test_routine_status_keeps_a_disagreeing_telemetry_window_on_its_own_gpu() -> None:
    """A partially failing collector cycle must not read as one shared window."""

    def gpu(uuid: str, index: int, sample_count: int) -> dict[str, object]:
        return {
            "endpoint_id": "server-a",
            "gpu_uuid": uuid,
            "gpu_index": index,
            "name": "A800",
            "total_vram_mib": 80_000,
            "state": "RUNNING_MANAGED",
            "publicly_available": False,
            "public_status": "任务占用",
            "lease": {"id": "lease-mine", "task_ref": "训练"},
            "telemetry": {
                "observed_at": "2026-08-15T00:02:00Z",
                "lease_recent_average": {
                    "window_seconds": 600,
                    "sample_count": sample_count,
                    "first_observed_at": "2026-08-15T00:00:00Z",
                    "last_observed_at": "2026-08-15T00:02:00Z",
                    "memory_used_mib": 1_000,
                },
            },
        }

    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [{"id": "server-a", "workspace_path": "/srv/server-a"}],
                "gpus": [gpu("GPU-a", 0, 10), gpu("GPU-b", 1, 4)],
            }
        },
        lease_id="lease-mine",
    )

    assert "telemetry_window" not in status["lease"]
    assert status["leased_gpus"][0]["window_override"]["sample_count"] == 10
    assert status["leased_gpus"][1]["window_override"]["sample_count"] == 4


def test_mcp_reads_distinguish_internal_keepalive_from_available_capacity(
    build_app, inventory, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = False
    configured.endpoints[0].keepalive_adapter_id = "server-script-v1"
    configured.endpoints[0].expected_gpu_count = 2
    app = build_app("mcp-keepalive", inventory_config=configured)
    service = app.state.service
    service.ingest_observation(observation(count=2))
    actor = service.local_actor("agent-a")
    service.configure_keepalive_policy(
        actor,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="mcp-keepalive-policy",
    )
    observation_not_before = datetime.now(UTC)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=4_001)],
            observed_at=datetime.now(UTC),
        )
    )
    service.activate_keepalive(
        actor,
        "endpoint-a",
        "endpoint-a:GPU-endpoint-a-0",
        observation_not_before=observation_not_before,
        idempotency_key="mcp-keepalive-begin",
    )
    rest = TestClient(app)
    headers = {"X-ServerPilot-Actor": "agent-a"}

    class RestBackedClient:
        def snapshot(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs == {
                "compact": False,
                "endpoint_id": None,
                "only_available": False,
            }
            # Mirror BrokerClient.snapshot: an unset endpoint_id is dropped
            # rather than sent as an empty value the broker would filter on.
            params = {key: value for key, value in kwargs.items() if value is not None}
            response = rest.get("/api/v1/snapshot", params=params, headers=headers)
            assert response.status_code == 200
            return response.json()

    monkeypatch.setattr(
        mcp_server,
        "_routine_client",
        lambda: RestBackedClient(),
    )

    status = tools.gpu_status()
    assert "gpus" not in status
    assert len(status["ungrouped_servers"]) == 1
    assert status["ungrouped_servers"][0]["gpus"] == [
        {
            "name": "Test GPU",
            "vram_mib": 100_000,
            "total_count": 2,
            "available_count": 2,
        }
    ]
    sku = status["ungrouped_servers"][0]["gpus"][0]
    assert set(sku) == {"name", "vram_mib", "total_count", "available_count"}
    # One card is held by a running keepalive helper and the other is not, but
    # that is ServerPilot's own bookkeeping.  A routine caller can act only on
    # whether the card can be claimed, and both can, so the mechanism stays
    # inside: no keepalive field, no telemetry carrying its hold.
    rendered = json.dumps(status)
    assert "keepalive" not in rendered
    assert "GPU-endpoint-a-0" not in rendered
    # The GUI still sees the distinction on its own path.
    detail = rest.get("/api/v1/snapshot", headers=headers).json()
    assert {gpu["public_status"] for gpu in detail["data"]["gpus"]} == {
        "可用 · 空闲占卡",
        "可用 · 占卡未运行",
    }





def test_app_starts_with_projects_and_no_endpoints(build_app) -> None:
    inventory = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A")],
        endpoints=[],
    )
    app = build_app("empty", inventory_config=inventory)
    client = TestClient(app)
    assert client.get("/").status_code == 404
    response = client.get("/api/v1/endpoints", headers={"X-ServerPilot-Actor": "agent"})
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_cli_help_is_available() -> None:
    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout


def test_cli_state_uses_canonical_client_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeClient:
        def control_plane_state(
            self,
            *,
            minimum_snapshot_revision=None,
            timeout_seconds=0,
            poll_interval_seconds=0.25,
        ):  # type: ignore[no-untyped-def]
            calls.append((minimum_snapshot_revision, timeout_seconds, poll_interval_seconds))
            return {
                "schema_version": "v1",
                "snapshot_revision": 12,
                "server_time": "2026-08-06T00:00:00Z",
                "data": {"current": {"gpus": []}, "history": {}},
            }

    monkeypatch.setattr(cli_module, "_client", lambda url, actor: FakeClient())

    result = CliRunner().invoke(
        cli_app,
        [
            "state",
            "--minimum-snapshot-revision",
            "12",
            "--timeout-seconds",
            "2",
            "--poll-interval-seconds",
            "0.5",
        ],
    )

    assert result.exit_code == 0
    assert '"snapshot_revision": 12' in result.stdout
    assert calls == [(12, 2.0, 0.5)]


def _routine_status_fixture(gpu_count: int) -> dict[str, object]:
    """One server holding ``gpu_count`` fully observed GPUs."""

    return {
        "data": {
            "summary": {"total_gpus": gpu_count},
            "endpoints": [
                {
                    "id": "server-10-20-0-10-p4482",
                    "workspace_path": "/srv/example-workspace/user",
                    "host": "10.20.0.10",
                    "port": 4482,
                    "ssh_user": "root",
                }
            ],
            "gpus": [
                {
                    "endpoint_id": "server-10-20-0-10-p4482",
                    "gpu_uuid": f"GPU-a6c03f47-e30a-61f9-e1b9-ff1e3156e1{index:02d}",
                    "gpu_index": index,
                    "name": "NVIDIA H100 80GB HBM3",
                    "total_vram_mib": 97_887,
                    "state": "KEEPALIVE",
                    "publicly_available": True,
                    "public_status": "可用 · 空闲占卡",
                    "keepalive": {"desired": "ON", "actual": "ON"},
                    "telemetry": {
                        "observed_at": "2026-08-21T11:50:42.163448+00:00",
                        "memory_used_mib": 78_411,
                        "memory_free_mib": 18_840,
                        "gpu_utilization_pct": 68,
                        "memory_utilization_pct": 0,
                        "temperature_c": 60,
                        "recent_average": {
                            "window_seconds": 600,
                            "sample_count": 10,
                            "first_observed_at": "2026-08-21T11:41:43.064003+00:00",
                            "last_observed_at": "2026-08-21T11:50:42.163448+00:00",
                            "memory_used_mib": 78_411,
                            "memory_free_mib": 18_840,
                            "memory_used_pct": 80.1,
                            "gpu_utilization_pct": 68,
                            "memory_utilization_pct": 0,
                            "temperature_c": 62.7,
                        },
                    },
                }
                for index in range(gpu_count)
            ],
        }
    }


def test_routine_projections_do_not_repeat_server_facts_per_gpu() -> None:
    """Guard the response budget these projections were reshaped to protect.

    A measured 8-GPU status response spent about a quarter of its bytes on
    endpoint fields copied onto every GPU row, and a 4-GPU lease spent about
    half.  Both are published once per server.  Dropping telemetry from cards
    nobody holds took the same response from 5,957 to 1,749 bytes, because the
    only thing it described was ServerPilot's own hold.  This gate fails if
    either regression returns.
    """

    status = mcp_server._routine_gpu_status(_routine_status_fixture(8), lease_id=None)

    assert "gpus" not in status
    assert len(status["ungrouped_servers"]) == 1
    server = status["ungrouped_servers"][0]
    assert server["gpus"] == [
        {
            "name": "NVIDIA H100 80GB HBM3",
            "vram_mib": 97_887,
            "total_count": 8,
            "available_count": 8,
        }
    ]
    for server_fact in ("ssh", "workspace", "workspace_path"):
        assert server_fact not in server["gpus"][0]
    assert "telemetry_window" not in server
    # The fixture's cards each read as 78,411 MiB used at 68% — every byte of
    # it ServerPilot's own keepalive hold, released before allocation.  None of
    # it may reach a caller deciding whether to claim them.
    rendered = json.dumps(status, ensure_ascii=False)
    for held in ("78411", "18840", "80.1", "62.7", "keepalive", "gpu_id"):
        assert held not in rendered
    status_size = len(rendered)
    assert status_size < 2_000, status_size

    allocation = mcp_server._routine_gpu_allocation(
        {
            "lease": {
                "id": "lease-a",
                "resources": [
                    {
                        "endpoint": {
                            "id": "server-10-20-0-10-p4482",
                            "workspace_path": "/srv/example-workspace/user",
                            "host": "10.20.0.10",
                            "port": 4482,
                            "ssh_user": "root",
                        },
                        "gpus": [
                            {
                                "gpu_uuid": f"GPU-a6c03f47-e30a-61f9-e1b9-ff1e3156e1{index:02d}",
                                "gpu_index": index,
                                "cuda_ordinal": index,
                            }
                            for index in range(4)
                        ],
                        "cuda_visible_devices": "0,1,2,3",
                        "cuda_device_order": "PCI_BUS_ID",
                    }
                ],
            }
        }
    )

    for row in allocation["gpus"]:
        for server_fact in ("ssh", "workspace", "workspace_path", "cuda_visible_devices"):
            assert server_fact not in row
    allocation_size = len(json.dumps(allocation, ensure_ascii=False))
    assert allocation_size < 1_600, allocation_size


class _RegistrationCollector:
    """Stand in for the one collection a registration now performs."""

    def __init__(self, *, error: str | None) -> None:
        self.error = error
        self.collected: list[list[str]] = []

    async def collect_once(
        self,
        service,  # noqa: ANN001
        *,
        endpoints=None,  # noqa: ANN001
        stagger_seconds: float = 0.0,
    ) -> dict[str, object]:
        self.collected.append([endpoint.id for endpoint in endpoints or []])
        for endpoint in endpoints or []:
            if self.error is None:
                await service.in_domain(
                    service.ingest_observation, observation(endpoint.id, count=2)
                )
            else:
                await service.in_domain(
                    service.record_provider_failure, endpoint.id, self.error
                )
        return {}


def _register(client: TestClient, server_id: str, **extra: object):  # noqa: ANN201
    body = {
        "id": server_id,
        "host": "10.0.0.9",
        "port": 7770,
        "ssh_user": "root",
        "workspace_path": "/srv/new-server",
        **extra,
    }
    return client.post(
        "/api/v1/endpoints",
        json=body,
        headers={"X-ServerPilot-Actor": "operator", "Idempotency-Key": f"create-{server_id}"},
    )


def test_registering_a_host_reports_that_it_could_not_be_reached(build_app, inventory) -> None:
    # A registration used to answer "created" without ever having connected,
    # so a host whose SSH can never succeed -- an unknown host key, a withdrawn
    # port -- was reported as a new server and then sat as a red row nobody was
    # told about. The answer now carries the observed result.
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = False
    collector = _RegistrationCollector(
        error="CollectionError: Host key verification failed."
    )
    app = build_app("register-unreachable", inventory_config=configured, collector=collector)
    client = TestClient(app)

    created = _register(client, "server-unreachable")

    assert created.status_code == 200
    # Exactly the new endpoint is observed, not every host in the inventory.
    assert collector.collected == [["server-unreachable"]]
    result = created.json()["observation"]
    assert result["observed"] is False
    assert result["gpu_count"] == 0
    assert "Host key verification failed" in result["error"]
    # The endpoint is kept: a host key is the operator's to fix, and the next
    # cycle picks the machine up with no second registration.
    listed = client.get("/api/v1/endpoints", headers={"X-ServerPilot-Actor": "operator"})
    assert "server-unreachable" in {item["id"] for item in listed.json()["data"]}


def test_registering_a_reachable_host_reports_the_gpus_it_found(build_app, inventory) -> None:
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = False
    collector = _RegistrationCollector(error=None)
    app = build_app("register-reachable", inventory_config=configured, collector=collector)
    client = TestClient(app)

    created = _register(client, "server-reachable")

    assert created.status_code == 200
    result = created.json()["observation"]
    assert result["observed"] is True
    assert result["observed_at"] is not None
    assert result["gpu_count"] == 2
    assert result["error"] is None


def test_a_registered_host_can_join_a_group_without_the_desktop_app(build_app, inventory) -> None:
    # The domain and REST have always carried server_group_id; an ungrouped
    # host is one no grouped gpu_apply can select, so a registration that
    # cannot name a group cannot produce a usable server.
    configured = inventory.model_copy(deep=True)
    configured.collector.enabled = False
    app = build_app(
        "register-grouped",
        inventory_config=configured,
        collector=_RegistrationCollector(error=None),
    )
    client = TestClient(app)
    actor = {"X-ServerPilot-Actor": "operator"}
    client.post(
        "/api/v1/server-groups",
        json={
            "id": "baidu-baige",
            "display_name": "Baidu Baige",
            "workspace_path": "/srv/baige",
        },
        headers={**actor, "Idempotency-Key": "group-create"},
    )

    created = _register(client, "server-grouped", server_group_id="baidu-baige")

    assert created.status_code == 200
    assert created.json()["endpoint"]["server_group_id"] == "baidu-baige"
