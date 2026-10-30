from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from serverpilot import mcp_server
from serverpilot.client import BrokerClient
from serverpilot.database import Database
from serverpilot.schemas import RequestCreate
from tests.helpers import observation


def _request(task_ref: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task_ref,
            "purpose": "harness-neutral test",
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 1, "placement": "pack"},
        }
    )


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(map(_keys, value.values())), set())
    if isinstance(value, list):
        return set().union(*(map(_keys, value)), set())
    return set()


def test_client_emits_only_the_actor_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVERPILOT_ACTOR", raising=False)
    calls: list[dict[str, object]] = []

    def request(_method: str, _url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"schema_version": "v1", "data": {}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    client = BrokerClient.from_env()
    client.get("/api/v2/snapshot")

    assert client.actor == "agent"
    assert calls[0]["headers"] == {"X-ServerPilot-Actor": "agent"}


def test_mcp_process_namespace_prevents_request_id_reuse_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeContext:
        request_id = "1"

    monkeypatch.setattr(mcp_server, "_ROUTINE_MCP_INSTANCE_ID", "session-one")
    first = mcp_server._routine_request_key(FakeContext())  # type: ignore[arg-type]
    monkeypatch.setattr(mcp_server, "_ROUTINE_MCP_INSTANCE_ID", "session-two")
    second = mcp_server._routine_request_key(FakeContext())  # type: ignore[arg-type]

    assert first != second


def test_historical_contact_column_is_inert_and_not_projected(service, admin) -> None:
    actor = service.local_actor("legacy-contact-agent")
    with service.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE actors SET coordination_uri = 'legacy-contact' "
                "WHERE id = 'legacy-contact-agent'"
            )
        )

    assert service.local_actor("legacy-contact-agent") == actor
    with service.database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT coordination_uri FROM actors WHERE id = 'legacy-contact-agent'")
            ).scalar_one()
            == "legacy-contact"
        )

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(
        actor,
        _request("shared-task"),
        idempotency_key="legacy-contact-claim",
    )
    assert allocated["lease"] is not None
    assert "coordination_uri" not in _keys(allocated)
    assert "coordination_uri" not in _keys(service.snapshot(admin))


def test_busy_status_returns_task_without_a_contact_field() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [{"id": "server-a", "workspace_path": "/srv/server-a"}],
                "gpus": [
                    {
                        "endpoint_id": "server-a",
                        "gpu_uuid": "GPU-a",
                        "gpu_index": 0,
                        "name": "A",
                        "total_vram_mib": 80_000,
                        "state": "HELD",
                        "publicly_available": False,
                        "public_status": "运行中",
                        "keepalive": {"state": "OFF", "reason": None},
                        "lease": {"id": "lease-other", "task_ref": "训练任务"},
                        "telemetry": {"memory_used_mib": 70_000, "gpu_utilization_pct": 90},
                    }
                ],
            }
        },
        lease_id=None,
    )

    assert "gpus" not in status
    assert "servers" not in status
    assert status["ungrouped_servers"][0]["workspace"] == {
        "path": "/srv/server-a",
        "kind": "working_directory",
        "use_as_cwd": True,
        "code_location": "not_provided",
    }
    assert status["ungrouped_servers"][0]["gpus"] == [
        {
            "name": "A",
            "vram_mib": 80_000,
            "total_count": 1,
            "available_count": 0,
        }
    ]
    # Who holds a busy card is actionable; how hard their job works it is not,
    # so somebody else's telemetry never reaches this response.
    assert status["busy_gpus"] == [
        {
            "server_id": "server-a",
            "gpu_id": "GPU-a",
            "index": 0,
            "status": "held_idle",
            "task": "训练任务",
        }
    ]
    assert set(status["ungrouped_servers"][0]) == {
        "server_id",
        "workspace_path",
        "workspace",
        "gpus",
    }


def test_routine_status_reports_no_gpu_from_the_canonical_summary() -> None:
    status = mcp_server._routine_gpu_status(
        {"data": {"summary": {"total_gpus": 0}, "gpus": []}},
        lease_id=None,
    )

    assert status == {"message": "no GPUs are registered"}


def test_routine_status_reports_recognized_cpu_only_servers() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "summary": {"total_gpus": 0},
                "endpoints": [
                    {
                        "id": "server-cpu",
                        "resource_kind": "cpu_only",
                        "monitor": {"status": "ONLINE"},
                        "host_telemetry": {
                            "capacity": {
                                "cpu_scope": "container",
                                "cpu_cores": 30.0,
                                "cpu_available_cores": 12.5,
                                "memory_scope": "container",
                                "memory_total_mib": 249_856,
                                "memory_used_mib": 51_200,
                                "memory_available_mib": 198_656,
                            },
                        },
                    },
                    {
                        "id": "server-unknown",
                        "resource_kind": "unknown",
                        "monitor": {"status": "ONLINE"},
                    },
                ],
                "gpus": [],
            }
        },
        lease_id=None,
    )

    assert status == {
        "cpu_only_servers": [
            {
                "server_id": "server-cpu",
                "resource_kind": "cpu_only",
                "monitor_status": "ONLINE",
                "cpu_cores": 30.0,
                "cpu_available_cores": 12.5,
                "memory_total_mib": 249_856,
                "memory_available_mib": 198_656,
            }
        ],
        "message": "no GPUs are registered",
    }


def test_routine_status_projects_ungrouped_delegated_server_without_no_capacity() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "summary": {"total_gpus": 0},
                "endpoints": [
                    {
                        "id": "slurm-login-p22",
                        "workspace_path": "/home/alice/work",
                        "host": "login.example.test",
                        "port": 22,
                        "ssh_user": "alice",
                        "resource_kind": "cpu_only",
                        "scheduler_capacity": {
                            "free_gpu_count": 30,
                            "gpu_name": "NVIDIA A100-SXM4-80GB",
                            "vram_mib": 81920,
                        },
                    }
                ],
                "gpus": [],
            }
        },
        lease_id=None,
    )

    assert status == {
        "ungrouped_servers": [
            {
                "server_id": "slurm-login-p22",
                "workspace_path": "/home/alice/work",
                "workspace": {
                    "path": "/home/alice/work",
                    "kind": "working_directory",
                    "use_as_cwd": True,
                    "code_location": "not_provided",
                },
                "ssh": {"host": "login.example.test", "port": 22, "user": "alice"},
                "gpus": [
                    {
                        "name": "NVIDIA A100-SXM4-80GB",
                        "vram_mib": 81920,
                        "available_count": 30,
                    }
                ],
            }
        ],
    }


def test_routine_status_explains_when_all_gpus_are_unavailable() -> None:
    status = mcp_server._routine_gpu_status(
        {"data": {"summary": {"total_gpus": 4, "available_gpus": 0}, "gpus": []}},
        lease_id=None,
    )

    assert status == {
        "no_capacity": {
            "reason": "all_gpus_busy_or_unavailable",
            "message": "no GPU is allocatable right now; busy_gpus lists the tasks holding them.",
            "total_gpus": 4,
        },
    }


def test_historical_actor_contact_migration_is_additive(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'migration.sqlite3'}", root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.upgrade(config, "20260811_0016")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO actors "
                "(id, display_name, role, enabled, created_at, updated_at) "
                "VALUES ('legacy-agent', 'Legacy', 'allocator', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260812_0017")

    assert "coordination_uri" in {
        column["name"] for column in inspect(database.engine).get_columns("actors")
    }
    with database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT coordination_uri FROM actors WHERE id = 'legacy-agent'")
            ).scalar_one()
            is None
        )
