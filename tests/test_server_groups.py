from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from serverpilot import API_CAPABILITIES
from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig, ServerGroupConfig
from serverpilot.database import Database
from serverpilot.models import (
    AllocationRequest,
    Endpoint,
    IdempotencyRecord,
    Lease,
    LeaseResource,
    ServerGroup,
)
from serverpilot.schemas import (
    EndpointCreate,
    EndpointUpdate,
    RequestCreate,
    ServerGroupCreate,
    ServerGroupUpdate,
)
from serverpilot.service import (
    SYSTEM_ACTOR_ID,
    SYSTEM_PROJECT_ID,
    ActorContext,
    BrokerError,
    BrokerService,
)
from serverpilot.timeutil import utcnow
from tests.helpers import observation


def grouped_inventory() -> InventoryConfig:
    return InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A", weight=1)],
        server_groups=[
            ServerGroupConfig(
                id="group-small",
                display_name="Small pool",
                workspace_path="/data/small",
                environment_notes="rsync weights to /data/small/weights",
                description="two compact hosts",
            ),
            ServerGroupConfig(
                id="group-large",
                display_name="Large pool",
                workspace_path="/data/large",
            ),
        ],
        endpoints=[
            EndpointConfig(
                id="small-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
                server_group_id="group-small",
            ),
            EndpointConfig(
                id="small-b",
                host="127.0.0.1",
                port=2202,
                ssh_user="gpu",
                workspace_path="/data/small-override",
                server_group_id="group-small",
            ),
            EndpointConfig(
                id="large-a",
                host="127.0.0.1",
                port=2203,
                ssh_user="gpu",
                server_group_id="group-large",
            ),
            EndpointConfig(
                id="legacy-a",
                host="127.0.0.1",
                port=2204,
                ssh_user="gpu",
                workspace_path="/legacy/path",
                storage_group="old-nfs-tag",
            ),
        ],
    )


def grouped_service(tmp_path: Path) -> tuple[BrokerService, ActorContext]:
    broker = BrokerService(
        Database(f"sqlite:///{tmp_path / 'groups.sqlite3'}", Path(__file__).resolve().parents[1]),
        grouped_inventory(),
    )
    broker.initialize()
    broker.local_actor("test-admin")
    with broker.database.session() as session:
        from serverpilot.models import Actor

        actor = session.get(Actor, "test-admin")
        assert actor is not None
        actor.role = "admin"
        session.commit()
    admin = ActorContext(id="test-admin", role="admin", project_ids=frozenset({"project-a"}))
    return broker, admin


def claim(
    *,
    task_ref: str,
    gpu_count: int = 1,
    same_host: bool = True,
    placement: str = "pack",
    server_group_ids: list[str] | None = None,
    endpoint_ids: list[str] | None = None,
) -> RequestCreate:
    constraints: dict[str, object] = {
        "gpu_count": gpu_count,
        "placement": placement,
        "same_host": same_host,
    }
    if server_group_ids is not None:
        constraints["server_group_ids"] = server_group_ids
    if endpoint_ids is not None:
        constraints["endpoint_ids"] = endpoint_ids
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task_ref,
            "purpose": "group scheduling test",
            "duration_seconds": 3600,
            "constraints": constraints,
        }
    )


def seed_keepalive(service: BrokerService, *, endpoint_id: str, gpu_id: str) -> None:
    now = utcnow()
    with service.database.session() as session:
        session.add(
            AllocationRequest(
                id="ka-req",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=SYSTEM_PROJECT_ID,
                auto_activate=False,
                task_ref="keepalive",
                purpose="keepalive",
                constraints_json="{}",
                duration_seconds=3600,
                state="ALLOCATED",
                priority_class="normal",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Lease(
                id="ka-lease",
                request_id="ka-req",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=SYSTEM_PROJECT_ID,
                kind="keepalive",
                state="ACTIVE",
                issued_at=now,
                last_heartbeat_at=now,
                issued_revision=1,
            )
        )
        session.add(LeaseResource(lease_id="ka-lease", gpu_id=gpu_id, active=True))
        session.commit()


def test_inventory_rejects_unknown_group_reference() -> None:
    with pytest.raises(ValidationError, match="unknown server_group_id"):
        InventoryConfig(
            schema_version=1,
            endpoints=[
                EndpointConfig(
                    id="endpoint-a",
                    host="127.0.0.1",
                    port=22,
                    ssh_user="gpu",
                    server_group_id="missing-group",
                )
            ],
        )


def test_inventory_accepts_schema_version_1_with_groups() -> None:
    config = grouped_inventory()
    assert config.schema_version == 1
    assert {group.id for group in config.server_groups} == {"group-small", "group-large"}


def test_legacy_storage_group_is_not_promoted(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    snapshot = service.snapshot(admin)["data"]
    legacy = next(item for item in snapshot["endpoints"] if item["id"] == "legacy-a")
    assert legacy["storage_group"] == "old-nfs-tag"
    assert legacy["server_group_id"] is None
    assert {group["id"] for group in snapshot["server_groups"]} == {"group-small", "group-large"}


def test_workspace_inheritance_and_override(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    snapshot = {item["id"]: item for item in service.snapshot(admin)["data"]["endpoints"]}
    assert snapshot["small-a"]["workspace_path"] == "/data/small"
    assert snapshot["small-a"]["workspace_path_override"] is None
    assert snapshot["small-a"]["server_group_id"] == "group-small"
    assert snapshot["small-b"]["workspace_path"] == "/data/small-override"
    assert snapshot["small-b"]["workspace_path_override"] == "/data/small-override"
    collector = {item.id: item for item in service.collector_endpoints()}
    assert collector["small-a"].workspace_path == "/data/small"
    assert collector["small-b"].workspace_path == "/data/small-override"
    assert collector["small-a"].server_group_id == "group-small"


def test_create_rejects_endpoint_without_workspace_or_group(tmp_path: Path, service, admin) -> None:
    with pytest.raises(ValidationError, match="workspace_path or workspace_path_override"):
        EndpointCreate(
            id="orphan",
            host="127.0.0.1",
            port=2290,
            ssh_user="gpu",
        )
    created = service.create_server_group(
        admin,
        ServerGroupCreate(
            id="rest-group",
            display_name="REST",
            workspace_path="/data/rest",
        ),
        idempotency_key="create-rest-group",
    )
    assert created["server_group"]["id"] == "rest-group"
    inherited = service.create_endpoint(
        admin,
        EndpointCreate(
            id="rest-member",
            host="127.0.0.1",
            port=2291,
            ssh_user="gpu",
            server_group_id="rest-group",
        ),
        idempotency_key="create-rest-member",
    )
    assert inherited["endpoint"]["workspace_path"] == "/data/rest"
    assert inherited["endpoint"]["workspace_path_override"] is None


def test_server_group_crud_and_delete_protection(tmp_path: Path, service, admin) -> None:
    created = service.create_server_group(
        admin,
        ServerGroupCreate(
            id="crud-group",
            display_name="CRUD",
            workspace_path="/data/crud",
            environment_notes="plain notes",
        ),
        idempotency_key="crud-create",
    )
    assert created["server_group"]["environment_notes"] == "plain notes"
    listed = service.control_plane_state(admin)["data"]["current"]["server_groups"]
    assert any(item["id"] == "crud-group" for item in listed)
    fetched = next(item for item in listed if item["id"] == "crud-group")
    assert fetched["display_name"] == "CRUD"
    updated = service.update_server_group(
        admin,
        "crud-group",
        ServerGroupUpdate(display_name="CRUD renamed"),
        idempotency_key="crud-rename",
    )
    assert updated["server_group"]["display_name"] == "CRUD renamed"
    renamed = next(
        item
        for item in service.control_plane_state(admin)["data"]["current"]["server_groups"]
        if item["id"] == "crud-group"
    )
    assert renamed["display_name"] == "CRUD renamed"
    service.create_endpoint(
        admin,
        EndpointCreate(
            id="crud-member",
            host="127.0.0.1",
            port=2292,
            ssh_user="gpu",
            server_group_id="crud-group",
        ),
        idempotency_key="crud-member",
    )
    with pytest.raises(BrokerError) as error:
        service.delete_server_group(admin, "crud-group", idempotency_key="crud-delete-blocked")
    assert error.value.code == "server_group_has_members"
    service.update_endpoint(
        admin,
        "crud-member",
        EndpointUpdate(server_group_id=None, workspace_path="/srv/unbound"),
        idempotency_key="unbind-member",
    )
    deleted = service.delete_server_group(admin, "crud-group", idempotency_key="crud-delete")
    assert deleted["changed"] is True
    assert all(
        item["id"] != "crud-group"
        for item in service.control_plane_state(admin)["data"]["current"]["server_groups"]
    )
    events = service.list_events(admin)["data"]
    assert any(
        event["resource_type"] == "server_group" and event["action"] == "server_group.created"
        for event in events
    )
    assert all(event["resource_type"] != "cluster" for event in events if "server_group" in event["action"])


def test_stale_inventory_does_not_clear_rest_membership(tmp_path: Path, service, admin) -> None:
    service.create_server_group(
        admin,
        ServerGroupCreate(id="rest-bound", display_name="Bound", workspace_path="/data/bound"),
        idempotency_key="rest-bound",
    )
    service.update_endpoint(
        admin,
        "endpoint-a",
        EndpointUpdate(server_group_id="rest-bound"),
        idempotency_key="bind-a",
    )
    stale = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A", weight=1)],
        endpoints=[
            EndpointConfig(
                id="endpoint-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
                workspace_path="/srv/project-a",
            ),
            EndpointConfig(
                id="endpoint-b",
                host="127.0.0.1",
                port=2202,
                ssh_user="gpu",
                workspace_path="/srv/project-b",
            ),
        ],
    )
    service.inventory = stale
    service.initialize(sync_inventory=True)
    with service.database.session() as session:
        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        assert endpoint.server_group_id == "rest-bound"
        assert session.get(ServerGroup, "rest-bound") is not None


def test_explicit_inventory_group_assignment_updates_membership(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    updated = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A", weight=1)],
        server_groups=list(grouped_inventory().server_groups),
        endpoints=[
            EndpointConfig(
                id="legacy-a",
                host="127.0.0.1",
                port=2204,
                ssh_user="gpu",
                workspace_path="/legacy/path",
                server_group_id="group-large",
            ),
            *grouped_inventory().endpoints[:-1],
        ],
    )
    service.inventory = updated
    service.initialize(sync_inventory=True)
    snapshot = {item["id"]: item for item in service.snapshot(admin)["data"]["endpoints"]}
    assert snapshot["legacy-a"]["server_group_id"] == "group-large"
    assert snapshot["legacy-a"]["workspace_path"] == "/legacy/path"


def test_group_selection_required_and_does_not_create_request(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="small-a", count=2))
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            claim(task_ref="need-group", same_host=True),
            idempotency_key="need-group",
        )
    assert error.value.code == "group_selection_required"
    details = error.value.details
    assert {group["id"] for group in details["server_groups"]} == {"group-small", "group-large"}
    small = next(group for group in details["server_groups"] if group["id"] == "group-small")
    assert small["workspace_path"] == "/data/small"
    assert small["environment_notes"]
    assert small["servers"]
    member = next(server for server in small["servers"] if server["server_id"] == "small-a")
    assert member["gpus"]
    sku = member["gpus"][0]
    assert set(sku) == {"name", "vram_mib", "total_count", "available_count"}
    assert details["ungrouped_servers"]
    ungrouped = next(
        server for server in details["ungrouped_servers"] if server["server_id"] == "legacy-a"
    )
    assert "gpus" in ungrouped
    assert service.list_requests(admin)["data"] == []
    assert service.list_leases(admin)["data"] == []
    with service.database.session() as session:
        assert session.scalars(select(IdempotencyRecord)).all() == []


def test_grouped_direct_pin_cannot_bypass_group_selection(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="small-a", count=8))
    service.ingest_observation(observation(endpoint_id="small-b", count=2))
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            claim(task_ref="pin-grouped", same_host=True, endpoint_ids=["small-a"]),
            idempotency_key="pin-grouped",
        )
    assert error.value.code == "group_selection_required"
    claimed = service.create_request(
        admin,
        claim(
            task_ref="pin-ignored",
            same_host=True,
            server_group_ids=["group-small"],
            endpoint_ids=["small-a"],
            gpu_count=1,
        ),
        idempotency_key="pin-ignored",
    )
    assert claimed["lease"]["resources"][0]["endpoint"]["id"] == "small-b"


def test_no_cross_group_allocation_and_within_group_best_fit(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="small-a", count=2))
    service.ingest_observation(observation(endpoint_id="small-b", count=2))
    service.ingest_observation(observation(endpoint_id="large-a", count=8))
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            claim(task_ref="too-big", gpu_count=8, server_group_ids=["group-small"]),
            idempotency_key="too-big",
        )
    assert error.value.code == "no_capacity"
    fitted = service.create_request(
        admin,
        claim(task_ref="one-card", gpu_count=1, server_group_ids=["group-small"]),
        idempotency_key="one-card",
    )
    assert fitted["lease"]["resources"][0]["endpoint"]["id"] == "small-a"
    whole = service.create_request(
        admin,
        claim(task_ref="eight", gpu_count=8, server_group_ids=["group-large"]),
        idempotency_key="eight",
    )
    assert whole["lease"]["resources"][0]["endpoint"]["id"] == "large-a"
    assert len(whole["lease"]["gpu_ids"]) == 8
    snapshot = service.snapshot(admin)
    small = next(item for item in snapshot["data"]["server_groups"] if item["id"] == "group-small")
    large = next(item for item in snapshot["data"]["server_groups"] if item["id"] == "group-large")
    assert small["allocation"] == "direct"
    assert small["limits"]["lease_ends"] == "on_release"
    assert small["limits"]["max_lease_seconds"] is None
    assert small["limits"]["queues"] is False
    assert small["limits"]["max_gpus_per_lease"] == 2
    assert small["largest_allocatable_block"] == 2
    assert large["allocation"] == "direct"
    assert large["limits"]["max_gpus_per_lease"] == 8
    assert large["largest_allocatable_block"] == 0


def test_one_card_claim_on_eight_card_grouped_host_leaves_remaining_capacity(
    tmp_path: Path,
) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="large-a", count=8))
    claimed = service.create_request(
        admin,
        claim(task_ref="one-of-eight", gpu_count=1, server_group_ids=["group-large"]),
        idempotency_key="one-of-eight",
    )
    lease = claimed["lease"]
    assert len(lease["resources"]) == 1
    assert len(lease["gpu_ids"]) == 1
    assert lease["resources"][0]["endpoint"]["id"] == "large-a"
    host_gpus = [
        gpu for gpu in service.list_gpus(admin)["data"] if gpu["endpoint_id"] == "large-a"
    ]
    assert len(host_gpus) == 8
    available = [gpu for gpu in host_gpus if gpu["state"] == "AVAILABLE"]
    taken = [gpu for gpu in host_gpus if gpu["id"] == lease["gpu_ids"][0]]
    assert len(available) == 7
    assert taken[0]["state"] == "HELD"


def test_ungrouped_and_non_routine_paths_remain_compatible(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="legacy-a", count=1))
    service.ingest_observation(observation(endpoint_id="small-a", count=4))
    service.ingest_observation(observation(endpoint_id="large-a", count=4))
    ungrouped = service.create_request(
        admin,
        claim(task_ref="legacy-pin", same_host=True, endpoint_ids=["legacy-a"]),
        idempotency_key="legacy-pin",
    )
    assert ungrouped["lease"]["resources"][0]["endpoint"]["id"] == "legacy-a"
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            claim(task_ref="advanced-pack", gpu_count=8, same_host=False, placement="pack"),
            idempotency_key="advanced-pack",
        )
    assert error.value.code == "group_selection_required"
    assert all(
        item["task_ref"] != "advanced-pack" for item in service.list_requests(admin)["data"]
    )


def test_group_workspace_change_blocked_while_inheriting_keepalive(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="small-a", count=1))
    gpu_id = next(
        item["id"] for item in service.list_gpus(admin)["data"] if item["endpoint_id"] == "small-a"
    )
    seed_keepalive(service, endpoint_id="small-a", gpu_id=gpu_id)
    with pytest.raises(BrokerError) as error:
        service.update_server_group(
            admin,
            "group-small",
            ServerGroupUpdate(workspace_path="/data/small-moved"),
            idempotency_key="move-group",
        )
    assert error.value.code == "keepalive_endpoint_connection_in_use"
    service.update_endpoint(
        admin,
        "small-b",
        EndpointUpdate(workspace_path="/data/small-override-2"),
        idempotency_key="override-ok",
    )
    snapshot = {item["id"]: item for item in service.snapshot(admin)["data"]["endpoints"]}
    assert snapshot["small-b"]["workspace_path"] == "/data/small-override-2"


def test_environment_notes_are_plain_text_only() -> None:
    with pytest.raises(ValidationError, match="plain text"):
        ServerGroupCreate(
            id="bad-notes",
            display_name="Bad",
            workspace_path="/data/bad",
            environment_notes="export PATH=\x00/tmp",
        )


def test_server_group_rest_routes(build_app) -> None:
    app = build_app()
    from fastapi.testclient import TestClient

    client = TestClient(app)
    headers = {"Idempotency-Key": "api-group", "X-ServerPilot-Actor": "test-admin"}
    created = client.post(
        "/api/v1/server-groups",
        json={
            "id": "api-group",
            "display_name": "API",
            "workspace_path": "/data/api",
            "environment_notes": "notes",
        },
        headers=headers,
    )
    assert created.status_code == 200
    state = client.get("/api/v1/state", headers={"X-ServerPilot-Actor": "test-admin"})
    assert state.status_code == 200
    groups = state.json()["data"]["current"]["server_groups"]
    fetched = next(item for item in groups if item["id"] == "api-group")
    assert fetched["workspace_path"] == "/data/api"
    live = client.get("/health/live")
    assert live.json()["capabilities"] == list(API_CAPABILITIES)
    assert "server_group_crud" in live.json()["capabilities"]
    assert "server_groups" not in live.json()["capabilities"]
    snapshot = client.get("/api/v1/snapshot", headers={"X-ServerPilot-Actor": "test-admin"})
    assert "server_groups" in snapshot.json()["data"]



def test_workspace_override_mutation_contract(tmp_path: Path, service, admin) -> None:
    with pytest.raises(ValidationError, match="conflict"):
        EndpointCreate(
            id="conflict",
            host="127.0.0.1",
            port=2293,
            ssh_user="gpu",
            server_group_id="group-small",
            workspace_path="/data/a",
            workspace_path_override="/data/b",
        )
    created = service.create_server_group(
        admin,
        ServerGroupCreate(id="override-group", display_name="Override", workspace_path="/data/group"),
        idempotency_key="override-group",
    )
    assert created["server_group"]["id"] == "override-group"
    inherited = service.create_endpoint(
        admin,
        EndpointCreate(
            id="override-inherit",
            host="127.0.0.1",
            port=2294,
            ssh_user="gpu",
            server_group_id="override-group",
            workspace_path_override=None,
        ),
        idempotency_key="override-inherit",
    )
    assert inherited["endpoint"]["workspace_path"] == "/data/group"
    assert inherited["endpoint"]["workspace_path_override"] is None
    legacy = service.create_endpoint(
        admin,
        EndpointCreate(
            id="override-legacy",
            host="127.0.0.1",
            port=2295,
            ssh_user="gpu",
            server_group_id="override-group",
            workspace_path="/data/legacy-path",
        ),
        idempotency_key="override-legacy",
    )
    assert legacy["endpoint"]["workspace_path_override"] == "/data/legacy-path"
    explicit = service.create_endpoint(
        admin,
        EndpointCreate(
            id="override-explicit",
            host="127.0.0.1",
            port=2296,
            ssh_user="gpu",
            server_group_id="override-group",
            workspace_path_override="/data/explicit",
        ),
        idempotency_key="override-explicit",
    )
    assert explicit["endpoint"]["workspace_path"] == "/data/explicit"
    assert explicit["endpoint"]["workspace_path_override"] == "/data/explicit"
    cleared = service.update_endpoint(
        admin,
        "override-explicit",
        EndpointUpdate(workspace_path_override=None),
        idempotency_key="clear-override",
    )
    assert cleared["endpoint"]["workspace_path"] == "/data/group"
    assert cleared["endpoint"]["workspace_path_override"] is None
    with pytest.raises(BrokerError) as error:
        service.update_endpoint(
            admin,
            "override-inherit",
            EndpointUpdate(server_group_id=None),
            idempotency_key="clear-group-no-path",
        )
    assert error.value.code == "endpoint_workspace_required"


def test_group_reassignment_keepalive_protection(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="small-a", count=1))
    gpu_id = next(
        item["id"] for item in service.list_gpus(admin)["data"] if item["endpoint_id"] == "small-a"
    )
    seed_keepalive(service, endpoint_id="small-a", gpu_id=gpu_id)
    with pytest.raises(BrokerError) as error:
        service.update_endpoint(
            admin,
            "small-a",
            EndpointUpdate(server_group_id="group-large"),
            idempotency_key="reassign-group",
        )
    assert error.value.code == "keepalive_endpoint_connection_in_use"


def test_claims_map_group_and_ungrouped_pin(build_app) -> None:
    app = build_app("rest-claim", inventory_config=grouped_inventory())
    service = app.state.service
    service.ingest_observation(observation(endpoint_id="small-a", count=2))
    service.ingest_observation(observation(endpoint_id="small-b", count=2))
    service.ingest_observation(observation(endpoint_id="legacy-a", count=1))
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "test-agent"}
    grouped = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "grouped-claim",
            "purpose": "grouped claim",
            "constraints": {
                "gpu_count": 1,
                "placement": "pack",
                "server_group_ids": ["group-small"],
                "same_host": True,
            },
        },
        headers={**headers, "Idempotency-Key": "grouped-claim"},
    )
    assert grouped.status_code == 200, grouped.text
    actor = service.local_actor("test-agent")
    grouped_request = next(
        item for item in service.list_requests(actor)["data"] if item["task_ref"] == "grouped-claim"
    )
    assert grouped_request["constraints"]["server_group_ids"] == ["group-small"]
    assert grouped_request["constraints"]["endpoint_ids"] == []
    assert grouped_request["constraints"]["same_host"] is True
    assert grouped_request["state"] == "LEASED"

    ungrouped = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "ungrouped-claim",
            "purpose": "ungrouped claim",
            "constraints": {
                "gpu_count": 1,
                "placement": "pack",
                "endpoint_ids": ["legacy-a"],
                "same_host": True,
            },
        },
        headers={**headers, "Idempotency-Key": "ungrouped-claim"},
    )
    assert ungrouped.status_code == 200, ungrouped.text
    ungrouped_request = next(
        item
        for item in service.list_requests(actor)["data"]
        if item["task_ref"] == "ungrouped-claim"
    )
    assert ungrouped_request["constraints"]["server_group_ids"] == []
    assert ungrouped_request["constraints"]["endpoint_ids"] == ["legacy-a"]
    assert ungrouped_request["constraints"]["same_host"] is True
    assert ungrouped_request["state"] == "LEASED"
    ungrouped_lease = next(
        item for item in service.list_leases(actor)["data"] if item["request_id"] == ungrouped_request["id"]
    )
    assert ungrouped_lease["resources"][0]["endpoint"]["id"] == "legacy-a"


def test_endpoint_create_inherits_and_overrides_group_workspace(build_app) -> None:
    app = build_app("rest-endpoints", inventory_config=grouped_inventory())
    client = TestClient(app)
    service = app.state.service
    actor_headers = {"X-ServerPilot-Actor": "test-admin"}
    inherited = client.post(
        "/api/v1/endpoints",
        json={
            "id": "form-inherit",
            "host": "127.0.0.8",
            "port": 2210,
            "ssh_user": "gpu",
            "server_group_id": "group-small",
            "owner_project_id": "project-a",
        },
        headers={**actor_headers, "Idempotency-Key": "form-inherit"},
    )
    assert inherited.status_code == 200, inherited.text
    form_endpoint = inherited.json()["endpoint"]
    assert form_endpoint["server_group_id"] == "group-small"
    assert form_endpoint["workspace_path"] == "/data/small"
    assert form_endpoint["workspace_path_override"] is None

    ssh_inherit = client.post(
        "/api/v1/endpoints",
        json={
            "id": "ssh-inherit-host",
            "host": "ssh-inherit-host",
            "port": 22,
            "ssh_user": "gpu",
            "server_group_id": "group-small",
            "owner_project_id": "project-a",
        },
        headers={**actor_headers, "Idempotency-Key": "ssh-inherit"},
    )
    assert ssh_inherit.status_code == 200, ssh_inherit.text
    inherited_ssh = ssh_inherit.json()["endpoint"]
    assert inherited_ssh["server_group_id"] == "group-small"
    assert inherited_ssh["workspace_path"] == "/data/small"
    assert inherited_ssh["workspace_path_override"] is None

    ssh_override = client.post(
        "/api/v1/endpoints",
        json={
            "id": "ssh-override-host",
            "host": "ssh-override-host",
            "port": 22,
            "ssh_user": "gpu",
            "server_group_id": "group-small",
            "workspace_path_override": "/data/ssh-override",
            "owner_project_id": "project-a",
        },
        headers={**actor_headers, "Idempotency-Key": "ssh-override"},
    )
    assert ssh_override.status_code == 200, ssh_override.text
    overridden = ssh_override.json()["endpoint"]
    assert overridden["workspace_path"] == "/data/ssh-override"
    assert overridden["workspace_path_override"] == "/data/ssh-override"

    ssh_legacy = client.post(
        "/api/v1/endpoints",
        json={
            "id": "ssh-legacy-host",
            "host": "ssh-legacy-host",
            "port": 22,
            "ssh_user": "gpu",
            "workspace_path": "/srv/ssh-legacy",
            "owner_project_id": "project-a",
        },
        headers={**actor_headers, "Idempotency-Key": "ssh-legacy"},
    )
    assert ssh_legacy.status_code == 200, ssh_legacy.text
    legacy = ssh_legacy.json()["endpoint"]
    assert legacy["server_group_id"] is None
    assert legacy["workspace_path"] == "/srv/ssh-legacy"
    assert legacy["workspace_path_override"] == "/srv/ssh-legacy"
    ids = {item["id"] for item in service.list_endpoints(service.local_actor("test-admin"))["data"]}
    assert {"form-inherit", "ssh-inherit-host", "ssh-override-host", "ssh-legacy-host"} <= ids


def _no_idempotency(service: BrokerService) -> None:
    with service.database.session() as session:
        assert session.scalars(select(IdempotencyRecord)).all() == []







def test_unknown_server_group_id_fails_before_persistence(build_app) -> None:
    app = build_app("unknown-group", inventory_config=grouped_inventory())
    service = app.state.service
    service.ingest_observation(observation(endpoint_id="small-a", count=2))
    actor = service.local_actor("test-agent")
    refused = TestClient(app).post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "missing-group",
            "purpose": "unknown group must not persist",
            "constraints": {
                "gpu_count": 1,
                "same_host": True,
                "server_group_ids": ["does-not-exist"],
            },
        },
        headers={
            "X-ServerPilot-Actor": "test-agent",
            "Idempotency-Key": "missing-group",
        },
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "server_group_not_found"
    assert service.list_requests(actor)["data"] == []
    assert service.list_leases(actor)["data"] == []
    _no_idempotency(service)


def test_routine_claims_canonicalize_omitted_and_false_same_host(build_app) -> None:
    app = build_app("routine-same-host")
    service = app.state.service
    service.ingest_observation(observation(count=2))
    client = TestClient(app)
    for label, constraints in (
        ("omitted", {"gpu_count": 1, "placement": "pack"}),
        ("false", {"gpu_count": 1, "same_host": False, "placement": "pack"}),
    ):
        claimed = client.post(
            "/api/v1/routine/claims",
            json={
                "project_id": "project-a",
                "task_ref": f"routine-{label}",
                "purpose": "legacy one host",
                "constraints": constraints,
            },
            headers={
                "X-ServerPilot-Actor": "test-agent",
                "Idempotency-Key": f"routine-{label}",
            },
        )
        assert claimed.status_code == 200, claimed.text
        body = claimed.json()
        assert body["request"]["constraints"]["same_host"] is True
        hosts = {resource["endpoint"]["id"] for resource in body["lease"]["resources"]}
        assert hosts == {"endpoint-a"}


def test_routine_claims_grouped_omitted_same_host_requires_group(build_app) -> None:
    app = build_app("routine-grouped", inventory_config=grouped_inventory())
    service = app.state.service
    service.ingest_observation(observation(endpoint_id="small-a", count=2))
    client = TestClient(app)
    actor = service.local_actor("test-agent")
    for label, constraints in (
        ("omitted", {"gpu_count": 1, "placement": "pack"}),
        ("false", {"gpu_count": 1, "same_host": False, "placement": "pack"}),
    ):
        refused = client.post(
            "/api/v1/routine/claims",
            json={
                "project_id": "project-a",
                "task_ref": f"need-group-{label}",
                "purpose": "must pick a group",
                "constraints": constraints,
            },
            headers={
                "X-ServerPilot-Actor": "test-agent",
                "Idempotency-Key": f"need-group-{label}",
            },
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "group_selection_required"
        assert service.list_requests(actor)["data"] == []
        _no_idempotency(service)


def test_generic_claims_same_host_false_spans_inside_one_group_or_ungrouped_fleet(
    build_app,
) -> None:
    grouped = build_app("claims-span", inventory_config=grouped_inventory())
    grouped.state.service.ingest_observation(observation(endpoint_id="small-a", count=4))
    grouped.state.service.ingest_observation(observation(endpoint_id="small-b", count=4))
    grouped.state.service.ingest_observation(observation(endpoint_id="large-a", count=4))
    client = TestClient(grouped)
    headers = {"X-ServerPilot-Actor": "test-agent"}
    refused = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "cross-group",
            "purpose": "must not mix groups",
            "constraints": {"gpu_count": 8, "same_host": False, "placement": "pack"},
        },
        headers={**headers, "Idempotency-Key": "cross-group"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "group_selection_required"
    actor = grouped.state.service.local_actor("test-agent")
    assert grouped.state.service.list_requests(actor)["data"] == []
    _no_idempotency(grouped.state.service)
    intra = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "intra-group",
            "purpose": "span inside one group",
            "constraints": {
                "gpu_count": 8,
                "same_host": False,
                "placement": "pack",
                "server_group_ids": ["group-small"],
            },
        },
        headers={**headers, "Idempotency-Key": "intra-group"},
    )
    assert intra.status_code == 200, intra.text
    assert {
        resource["endpoint"]["id"] for resource in intra.json()["lease"]["resources"]
    } == {"small-a", "small-b"}

    bare = build_app("claims-ungrouped")
    bare.state.service.ingest_observation(observation(endpoint_id="endpoint-a", count=4))
    bare.state.service.ingest_observation(observation(endpoint_id="endpoint-b", count=4))
    spanning = TestClient(bare).post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "ungrouped-span",
            "purpose": "span a fleet with no groups",
            "constraints": {"gpu_count": 8, "same_host": False, "placement": "pack"},
        },
        headers={**headers, "Idempotency-Key": "ungrouped-span"},
    )
    assert spanning.status_code == 200, spanning.text
    assert {
        resource["endpoint"]["id"] for resource in spanning.json()["lease"]["resources"]
    } == {"endpoint-a", "endpoint-b"}


def test_grouped_multi_host_request_does_not_mix_groups(tmp_path: Path) -> None:
    service, admin = grouped_service(tmp_path)
    service.ingest_observation(observation(endpoint_id="small-a", count=4))
    service.ingest_observation(observation(endpoint_id="small-b", count=4))
    service.ingest_observation(observation(endpoint_id="large-a", count=4))
    mixed = service.create_request(
        admin,
        claim(
            task_ref="both-groups",
            gpu_count=8,
            same_host=False,
            server_group_ids=["group-small", "group-large"],
        ),
        idempotency_key="both-groups",
    )
    hosts = {resource["endpoint"]["id"] for resource in mixed["lease"]["resources"]}
    named = {"group-small", "group-large"}
    endpoints = {
        item["id"]: item for item in service.snapshot(admin)["data"]["endpoints"]
    }
    resource_groups = {endpoints[host]["server_group_id"] for host in hosts}
    assert resource_groups <= named
    assert len(resource_groups) == 1
    assert hosts == {"small-a", "small-b"}


def test_routine_nodes_greater_than_one_is_validation_error(build_app) -> None:
    app = build_app("routine-nodes")
    app.state.service.ingest_observation(observation(count=8))
    refused = TestClient(app).post(
        "/api/v1/routine/claims",
        json={
            "project_id": "project-a",
            "task_ref": "routine-nodes",
            "purpose": "contradictory topology",
            "constraints": {"gpu_count": 8, "nodes": 2, "gpus_per_node": 4},
        },
        headers={"X-ServerPilot-Actor": "test-agent", "Idempotency-Key": "routine-nodes"},
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "validation_error"
    actor = app.state.service.local_actor("test-agent")
    assert app.state.service.list_requests(actor)["data"] == []
    _no_idempotency(app.state.service)


def test_routine_exact_grouped_pin_requires_group(build_app) -> None:
    app = build_app("routine-exact", inventory_config=grouped_inventory())
    service = app.state.service
    service.ingest_observation(observation(endpoint_id="small-a", count=2))
    actor = service.local_actor("test-agent")
    gpu_id = next(
        item["id"] for item in service.list_gpus(actor)["data"] if item["endpoint_id"] == "small-a"
    )
    refused = TestClient(app).post(
        "/api/v1/routine/claims",
        json={
            "project_id": "project-a",
            "task_ref": "routine-exact",
            "purpose": "exact grouped pin",
            "constraints": {
                "gpu_count": 1,
                "placement": "exact",
                "gpu_ids": [gpu_id],
                "endpoint_ids": ["small-a"],
            },
        },
        headers={"X-ServerPilot-Actor": "test-agent", "Idempotency-Key": "routine-exact"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "group_selection_required"
    assert service.list_requests(actor)["data"] == []
    _no_idempotency(service)


def test_ungrouped_create_requires_workspace_and_typed_path_is_override(build_app) -> None:
    app = build_app("stock-workspace", inventory_config=grouped_inventory())
    client = TestClient(app)
    service = app.state.service
    headers = {"X-ServerPilot-Actor": "test-admin"}
    ungrouped_blank = client.post(
        "/api/v1/endpoints",
        json={
            "id": "blank-ungrouped",
            "host": "127.0.0.9",
            "port": 2211,
            "ssh_user": "gpu",
            "owner_project_id": "project-a",
        },
        headers={**headers, "Idempotency-Key": "blank-ungrouped"},
    )
    assert ungrouped_blank.status_code == 422, ungrouped_blank.text
    assert ungrouped_blank.json()["error"]["code"] in {"endpoint_workspace_required", "validation_error"}
    assert all(
        item["id"] != "blank-ungrouped"
        for item in service.list_endpoints(service.local_actor("test-admin"))["data"]
    )
    stock = client.post(
        "/api/v1/endpoints",
        json={
            "id": "typed-stock",
            "host": "127.0.0.10",
            "port": 2212,
            "ssh_user": "gpu",
            "server_group_id": "group-small",
            "workspace_path": "/srv/serverpilot-workspace",
            "owner_project_id": "project-a",
        },
        headers={**headers, "Idempotency-Key": "typed-stock"},
    )
    assert stock.status_code == 200, stock.text
    typed = stock.json()["endpoint"]
    assert typed["workspace_path_override"] == "/srv/serverpilot-workspace"
    assert typed["workspace_path"] == "/srv/serverpilot-workspace"
