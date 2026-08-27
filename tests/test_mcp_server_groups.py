"""Routine MCP grouped capacity, apply schema, and group-selection outcomes."""

from __future__ import annotations

import asyncio

import pytest

from serverpilot import mcp_server
from serverpilot.client import BrokerClientError
from serverpilot.mcp_server import ROUTINE_GPU_COUNT_DESCRIPTION, mcp
from tests.helpers import tools


def _gpu(
    server_id: str,
    uuid: str,
    index: int,
    *,
    available: bool = True,
    name: str = "NVIDIA A100",
    vram_mib: int = 81_920,
) -> dict[str, object]:
    return {
        "endpoint_id": server_id,
        "gpu_uuid": uuid,
        "gpu_index": index,
        "name": name,
        "total_vram_mib": vram_mib,
        "state": "AVAILABLE" if available else "HELD",
        "publicly_available": available,
        "public_status": "可用",
        "lease": None if available else {"id": "lease-other", "task_ref": "训练"},
    }


def _endpoint(server_id: str, *, group_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": server_id,
        "workspace_path": f"/srv/{server_id}",
        "host": "10.0.0.1",
        "port": 22,
        "ssh_user": "gpu",
    }
    if group_id is not None:
        payload["server_group_id"] = group_id
    return payload


def test_gpu_apply_schema_locks_count_source_range_and_group_id() -> None:
    tools_list = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools_list}
    apply_schema = by_name["gpu_apply"].inputSchema
    gpu_count = apply_schema["properties"]["gpu_count"]

    assert set(apply_schema["properties"]) == {
        "server_group_id",
        "server_id",
        "gpu_count",
        "task",
    }
    assert "required" not in apply_schema
    assert gpu_count["default"] == 1
    assert gpu_count["minimum"] == 1
    assert gpu_count["maximum"] == 1024
    assert gpu_count["description"] == ROUTINE_GPU_COUNT_DESCRIPTION
    assert "devices" in gpu_count["description"]
    assert "--nproc_per_node" in gpu_count["description"]
    assert "num_processes" in gpu_count["description"]
    assert "--gres" in gpu_count["description"]
    assert "never server/free capacity" in gpu_count["description"]
    assert apply_schema["properties"]["server_group_id"]["default"] is None


def test_grouped_status_keeps_per_server_sku_counts_so_four_plus_four_is_not_eight() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "summary": {"total_gpus": 8},
                "server_groups": [
                    {
                        "id": "group-a",
                        "display_name": "Shared A100",
                        "workspace_path": "/srv/shared",
                        "environment_notes": "weights at /data/weights",
                        "description": "training rack",
                    }
                ],
                "endpoints": [
                    _endpoint("host-1", group_id="group-a"),
                    _endpoint("host-2", group_id="group-a"),
                ],
                "gpus": [
                    *[_gpu("host-1", f"GPU-1-{index}", index) for index in range(4)],
                    *[_gpu("host-2", f"GPU-2-{index}", index) for index in range(4)],
                ],
            }
        },
        lease_id=None,
    )

    assert "gpus" not in status
    assert "servers" not in status
    assert "ungrouped_servers" not in status
    group = status["server_groups"][0]
    assert group["id"] == "group-a"
    assert group["display_name"] == "Shared A100"
    assert group["workspace_path"] == "/srv/shared"
    assert group["environment_notes"] == "weights at /data/weights"
    assert group["description"] == "training rack"
    assert [server["server_id"] for server in group["servers"]] == ["host-1", "host-2"]
    sku = {
        "name": "NVIDIA A100",
        "vram_mib": 81_920,
        "total_count": 4,
        "available_count": 4,
    }
    assert group["servers"][0]["gpus"] == [sku]
    assert group["servers"][1]["gpus"] == [sku]
    assert all(server["gpus"][0]["total_count"] == 4 for server in group["servers"])
    assert sum(server["gpus"][0]["total_count"] for server in group["servers"]) == 8
    rendered = str(status)
    assert "GPU-1-0" not in rendered
    assert "gpu_id" not in rendered


def test_ungrouped_status_is_aggregated_not_a_flat_free_card_menu() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [_endpoint("host-1")],
                "gpus": [
                    _gpu("host-1", "GPU-a", 0, name="H100", vram_mib=80_000),
                    _gpu("host-1", "GPU-b", 1, name="H100", vram_mib=80_000),
                    _gpu("host-1", "GPU-c", 2, name="A6000", vram_mib=48_000),
                ],
            }
        },
        lease_id=None,
    )

    assert "gpus" not in status
    assert status["ungrouped_servers"][0]["gpus"] == [
        {"name": "H100", "vram_mib": 80_000, "total_count": 2, "available_count": 2},
        {"name": "A6000", "vram_mib": 48_000, "total_count": 1, "available_count": 1},
    ]


def test_single_card_allocation_still_returns_one_cuda_row() -> None:
    allocation = mcp_server._routine_gpu_allocation(
        {
            "lease": {
                "id": "lease-one",
                "resources": [
                    {
                        "endpoint": {
                            "id": "host-1",
                            "workspace_path": "/srv/host-1",
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

    assert allocation["lease_id"] == "lease-one"
    assert len(allocation["gpus"]) == 1
    assert allocation["gpus"] == [
        {
            "server_id": "host-1",
            "gpu_id": "GPU-a",
            "gpu_index": 7,
            "cuda_ordinal": 0,
            "gpu_cuda_visible_devices": "0",
        }
    ]
    assert allocation["cuda_visible_devices"] == "0"
    assert allocation["cuda_device_order"] == "PCI_BUS_ID"


def test_gpu_apply_passes_one_server_group_ids_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def post(self, path: str, body: dict[str, object] | None = None, **_kwargs: object):
            captured["path"] = path
            captured["body"] = body
            return {
                "lease": {
                    "id": "lease-a",
                    "resources": [
                        {
                            "endpoint": {"id": "host-1", "workspace_path": "/srv/host-1"},
                            "gpus": [
                                {"gpu_uuid": "GPU-a", "gpu_index": 0, "cuda_ordinal": 0}
                            ],
                            "cuda_visible_devices": "0",
                            "cuda_device_order": "PCI_BUS_ID",
                        }
                    ],
                }
            }

    monkeypatch.setattr(mcp_server, "_routine_client", lambda: FakeClient())

    result = tools.gpu_apply(server_group_id="group-a", task="训练")

    assert captured["path"] == "/api/v1/routine/claims"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["constraints"] == {
        "gpu_count": 1,
        "placement": "pack",
        "same_host": True,
        "server_group_ids": ["group-a"],
    }
    assert result["gpus"][0]["gpu_cuda_visible_devices"] == "0"


def test_group_selection_required_is_structured_data_not_a_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = BrokerClientError(
        "broker HTTP 409: group_selection_required: choose a server group",
        code="group_selection_required",
        status_code=409,
    )
    error.details = {
        "server_groups": [
            {
                "id": "group-a",
                "display_name": "Shared A100",
                "workspace_path": "/srv/shared",
                "environment_notes": "weights at /data/weights",
                "description": "training rack",
            }
        ]
    }

    class Refusing:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls += 1
            raise error

    client = Refusing()
    monkeypatch.setattr(mcp_server, "_routine_client", lambda: client)

    result = tools.gpu_apply(gpu_count=2, task="probe")

    assert set(result) == {"group_selection_required"}
    payload = result["group_selection_required"]
    assert payload["reason"] == "direct_grouped_hosts_require_server_group_id"
    assert payload["gpu_count"] == 2
    assert payload["server_id"] is None
    assert payload["server_group_id"] is None
    assert payload["server_groups"] == error.details["server_groups"]
    assert "choose a server group" in payload["message"]
    assert client.calls == 1


def test_group_selection_required_on_retry_is_still_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = BrokerClientError(
        "broker HTTP 409: group_selection_required: choose a server group",
        code="group_selection_required",
        status_code=409,
    )
    selection.details = {"server_groups": [{"id": "group-a"}]}
    errors = [
        BrokerClientError("broker request failed: ReadTimeout"),
        selection,
    ]

    class Retrying:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls += 1
            raise errors[self.calls - 1]

    client = Retrying()
    monkeypatch.setattr(mcp_server, "_routine_client", lambda: client)

    result = tools.gpu_apply(gpu_count=1, task="probe")

    assert set(result) == {"group_selection_required"}
    assert result["group_selection_required"]["server_groups"] == [{"id": "group-a"}]
    assert client.calls == 2


def test_blank_server_group_id_is_rejected_before_contacting_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "_routine_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not contact the broker")),
    )

    with pytest.raises(ValueError, match="server_group_id must not be empty when it is given"):
        tools.gpu_apply(server_group_id="   ", task="训练")


def test_scheduler_and_cpu_only_projections_stay_outside_server_groups() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "summary": {"total_gpus": 0},
                "server_groups": [
                    {
                        "id": "group-a",
                        "display_name": "Unused",
                        "workspace_path": "/srv/shared",
                        "environment_notes": None,
                        "description": None,
                    }
                ],
                "endpoints": [
                    {
                        "id": "slurm-login-p22",
                        "server_group_id": "group-a",
                        "resource_kind": "cpu_only",
                        "scheduler_capacity": {
                            "free_gpu_count": 30,
                            "gpu_name": "NVIDIA A100-SXM4-80GB",
                        },
                    },
                    {
                        "id": "server-cpu",
                        "resource_kind": "cpu_only",
                        "monitor": {"status": "ONLINE"},
                        "host_telemetry": {
                            "cpu_count": 104,
                            "memory_available_mib": 985_798,
                        },
                    },
                ],
                "gpus": [],
            }
        },
        lease_id=None,
    )

    assert "server_groups" not in status
    assert "ungrouped_servers" not in status
    assert "gpus" not in status
    assert status["scheduler_servers"] == [
        {
            "server_id": "slurm-login-p22",
            "free_gpu_count": 30,
            "gpu_name": "NVIDIA A100-SXM4-80GB",
            "note": "request on demand; nothing is queued",
        }
    ]
    assert status["cpu_only_servers"] == [
        {
            "server_id": "server-cpu",
            "resource_kind": "cpu_only",
            "monitor_status": "ONLINE",
            "cpu_count": 104,
            "memory_available_mib": 985_798,
        }
    ]
    assert "message" not in status
    assert "no_capacity" not in status
