from __future__ import annotations

from fastapi.testclient import TestClient

from serverpilot import API_CAPABILITIES
from serverpilot.schemas import RequestCreate
from tests.helpers import observation


def _request(task_ref: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task_ref,
            "purpose": "state contract regression",
            "constraints": {"gpu_count": 1},
        }
    )


def test_control_plane_state_route_groups_current_and_history(build_app) -> None:
    app = build_app("state")
    service = app.state.service
    actor = service.local_actor("state-agent")
    service.ingest_observation(observation(count=1))
    service.create_request(actor, _request("state-lease"), idempotency_key="state-lease")

    response = TestClient(app).get(
        "/api/v1/state",
        headers={"X-ServerPilot-Actor": "state-agent"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "control_plane_state" in API_CAPABILITIES
    assert payload["schema_version"] == "v1"
    assert isinstance(payload["snapshot_revision"], int)
    assert payload["data"].keys() == {"current", "history"}
    current = payload["data"]["current"]
    history = payload["data"]["history"]
    assert {
        "summary",
        "data_age_seconds",
        "freshness_seconds",
        "endpoints",
        "gpus",
        "absent_gpu_ids",
        "leases",
        "alerts",
        "host_capacity",
        "admission_boundary",
    } <= set(current)
    assert current["host_capacity"]
    assert {endpoint["id"] for endpoint in current["endpoints"]} == {
        "endpoint-a",
        "endpoint-b",
    }
    assert "retired_endpoints" not in history
    assert "resource_providers" not in current
    assert "workload_profiles" not in current
    assert "scheduler_targets" not in current
    assert history == {}
    assert "audit" not in current
    assert "telemetry" not in current


def test_idempotent_mutation_replay_retains_committed_revision(service, admin) -> None:
    service.ingest_observation(observation(count=1))

    first = service.create_request(admin, _request("committed-replay"), idempotency_key="commit-key")
    second = service.create_request(admin, _request("committed-replay"), idempotency_key="commit-key")

    assert first == second
    assert first["committed"] == {"snapshot_revision": first["snapshot_revision"]}


def test_control_plane_state_has_no_include_advanced_switch(build_app) -> None:
    app = build_app("state-narrow")
    service = app.state.service
    actor = service.local_actor("state-agent")
    service.ingest_observation(observation(count=1))
    service.create_request(actor, _request("state-lease"), idempotency_key="state-lease")
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "state-agent"}

    payload = client.get("/api/v1/state", headers=headers).json()["data"]
    assert payload["history"] == {}
    parameters = (
        app.openapi()
        .get("paths", {})
        .get("/api/v1/state", {})
        .get("get", {})
        .get("parameters", [])
    )
    assert all(item.get("name") != "include_advanced" for item in parameters)
