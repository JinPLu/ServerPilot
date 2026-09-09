from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from serverpilot import mcp_server
from serverpilot.models import Endpoint, GPUDevice, Lease, ResourceAllocation
from serverpilot.plugins import PluginError, PluginInfo
from serverpilot.schemas import RequestCreate
from serverpilot.service import BrokerError
from serverpilot.timeutil import utcnow


def allocation_fact(request_id: str, *, running: int | None = 0) -> dict:
    now = utcnow()
    return {
        "allocation_ref": "901", "request_id": request_id, "state": "active",
        "locations": [{"node": "node07", "gpu_count": 2, "running_process_count": running,
                       "devices": [{"gpu_uuid": "GPU-a", "physical_index": 4, "device_selector": "0", "running_process_count": running},
                                   {"gpu_uuid": "GPU-b", "physical_index": 7, "device_selector": "1", "running_process_count": 0}]}],
        "entry": {"kind": "allocation", "host": "login.example", "port": 22, "user": "alice",
                  "workspace_path": "/work", "argv": ["provider-enter", "901"],
                  "instructions": "Enter the existing allocation.", "verified": False},
        "expires_at": (now + timedelta(hours=4)).isoformat(), "observed_at": now.isoformat(),
        "workload_observed_at": now.isoformat(),
    }


def provider_snapshot(facts: list[dict]) -> str:
    from tests.helpers import probe_bundle
    nodes = []
    for fact in facts:
        for location in fact["locations"]:
            devices = location["devices"]
            source = {"identity": {"hostname": location["node"], "boot_id": "boot-a"},
                "observed_at": fact.get("workload_observed_at") or utcnow().isoformat(),
                "host": {"cpu_count": 64, "load_1m": 0, "cpu_total_ticks": 100, "cpu_idle_ticks": 90,
                         "memory_total_mib": 100000, "memory_available_mib": 90000},
                "gpus": [{"gpu_uuid": device["gpu_uuid"], "gpu_index": device["physical_index"],
                          "name": "GPU model", "total_vram_mib": 81920, "memory_used_mib": 0,
                          "memory_free_mib": 81920, "gpu_utilization_pct": 0} for device in devices],
                "processes": [{"gpu_uuid": device["gpu_uuid"], "pid": 101 + index, "used_memory_mib": 100,
                              "executable": "python", "username": "alice"} for index, device in enumerate(devices)
                              if device.get("running_process_count")],
            }
            node = probe_bundle(source)["nodes"][0]
            node["node_id"] = location["node"]
            if location["running_process_count"] is None:
                node.update(output=None, error_code="process_query_failed", message="Process sample missing")
            nodes.append(node)
    return json.dumps({"schema_version": 3, "nodes": list({node["node_id"]: node for node in nodes}.values()),
        "scheduler": {"observed_at": utcnow().isoformat(), "free_gpu_count": 4, "gpu_name": "GPU model", "largest_free_block": 4,
                      "max_gpus_per_lease": 8, "vram_mib": 81920,
                      "nodes": [{"name": "node07", "state": "mix", "total_gpus": 8, "allocated_gpus": 4, "schedulable": True}]},
        "allocations": facts})


def ingest(service, facts):
    from serverpilot.collector import parse_server_script_snapshot
    observed = parse_server_script_snapshot(provider_snapshot(facts), endpoint_id="endpoint-a", observed_at=utcnow())
    return service.ingest_observation(observed)


@pytest.fixture
def provider(service, monkeypatch):
    plugin = PluginInfo(plugin_id="sample-provider", display_name="Provider", schema_version=5,
                        capabilities=("observe", "apply", "release", "connect", "disconnect"),
                        path=Path("/not-executed"), source="local", release_granularity="allocation", entry_kind="allocation",
                        limits={"lease_ends": "hard_kill_at_time_limit", "max_lease_seconds": 86400,
                                "apply_max_seconds": 40, "queues": False})
    monkeypatch.setattr("serverpilot.plugins.get_plugin", lambda name, **_: plugin if name == plugin.plugin_id else None)
    service._write(lambda session: setattr(session.get(Endpoint, "endpoint-a"), "observation_profile", plugin.plugin_id))
    calls = []
    def apply(plugin_id, **kwargs):
        calls.append(kwargs)
        return allocation_fact(kwargs["request_id"])
    monkeypatch.setattr("serverpilot.plugins.apply_plugin", apply)
    monkeypatch.setattr("serverpilot.plugins.release_plugin", lambda *args, **kwargs: {"state": "released"})
    return calls


def request(**changes):
    return RequestCreate.model_validate({"project_id": "project-a", "task_ref": "training", "purpose": "training",
                                         "duration_seconds": 14400, "constraints": {"endpoint_ids": ["endpoint-a"], "gpu_count": 2},
                                         **changes})


def claim(service, admin, key="application-1", data=None):
    return asyncio.run(service.claim_provider(admin, data or request(), idempotency_key=key, persistent_lease=True))


def record(service):
    return service._read(lambda session: session.scalar(select(ResourceAllocation).where(ResourceAllocation.provider_id != "builtin")))


def test_apply_has_location_without_inventing_inventory_and_is_visible(service, admin, provider):
    result = claim(service, admin)
    allocation = result["lease"]["allocations"][0]
    assert allocation["locations"][0]["devices"][0]["physical_index"] == 4
    assert allocation["locations"][0]["devices"][0]["device_selector"] == "0"
    assert provider[0]["duration_seconds"] == 14400
    assert provider[0]["requirements"]["gpu_count"] == 2
    assert service._read(lambda session: session.scalars(select(GPUDevice)).all()) == []
    snapshot = service.snapshot(admin)["data"]
    assert snapshot["leases"][0]["id"] == result["lease"]["id"]
    projected = mcp_server._routine_gpu_allocation(result)
    assert projected["entry"]["kind"] == "allocation"
    assert "cuda_visible_devices" not in projected
    assert projected["gpus"][0]["node"] == "node07"
    status = mcp_server._routine_gpu_status({"data": snapshot}, lease_id=result["lease"]["id"])
    assert status["open_leases"][0]["gpu_count"] == 2
    assert status["leased_gpus"][0]["index"] == 4
    assert "no_leased_gpus" not in status


def test_repeated_request_and_repeated_task_return_same_allocation(service, admin, provider):
    first = claim(service, admin)
    again = claim(service, admin)
    later = claim(service, admin, key="different-transport-key")
    assert first["lease"]["id"] == again["lease"]["id"] == later["lease"]["id"]
    assert len(provider) == 1


def test_same_identity_cannot_change_requirements(service, admin, provider):
    claim(service, admin)
    with pytest.raises(BrokerError) as failure:
        claim(service, admin, key="different-key", data=request(duration_seconds=7200))
    assert failure.value.code == "idempotency_conflict"
    assert len(provider) == 1


def test_uncertain_apply_resolves_original_without_resubmitting(service, admin, provider, monkeypatch):
    seen = []
    def lost_reply(plugin_id, **kwargs):
        seen.append(kwargs["request_id"])
        raise PluginError("response lost")
    monkeypatch.setattr("serverpilot.plugins.apply_plugin", lost_reply)
    with pytest.raises(BrokerError, match="response lost"):
        claim(service, admin)
    assert record(service).state == "unconfirmed"
    assert service.snapshot(admin)["data"]["pending_allocations"][0]["id"] == seen[0]
    monkeypatch.setattr("serverpilot.plugins.observe_plugin", lambda _: provider_snapshot([allocation_fact(seen[0])]))
    recovered = claim(service, admin, key="new-http-key")
    assert recovered["lease"]["allocations"][0]["id"] == seen[0]
    assert len(seen) == 1


def test_local_lease_write_failure_keeps_remote_result(service, admin, provider, monkeypatch):
    original = service._write
    failed = False
    def fail_finish(operation):
        nonlocal failed
        if operation.__name__ == "finish" and not failed:
            failed = True
            raise OSError("simulated database write failure")
        return original(operation)
    monkeypatch.setattr(service, "_write", fail_finish)
    with pytest.raises(OSError):
        claim(service, admin)
    saved = record(service)
    assert saved.state == "active" and saved.lease_id is None and saved.allocation_ref == "901"
    monkeypatch.setattr("serverpilot.plugins.observe_plugin", lambda _: provider_snapshot([allocation_fact(saved.id)]))
    assert claim(service, admin)["lease"]["allocations"][0]["id"] == saved.id
    assert len(provider) == 1


def test_release_failure_keeps_lease_and_remote_handle(service, admin, provider, monkeypatch):
    lease = claim(service, admin)["lease"]
    def fail(*args, **kwargs):
        raise PluginError("transport failed")
    monkeypatch.setattr("serverpilot.plugins.release_plugin", fail)
    with pytest.raises(BrokerError) as error:
        service.release_lease(admin, lease["id"], reason="task finished", idempotency_key="release")
    assert error.value.code == "plugin_release_failed"
    assert record(service).state == "release_pending"
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "HELD"
    fact = allocation_fact(record(service).id)
    fact["state"] = "released"
    ingest(service, [fact])
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "RELEASED"


def test_provider_deadline_cannot_be_extended_locally(service, admin, provider):
    lease = claim(service, admin)["lease"]
    with pytest.raises(BrokerError) as error:
        service.renew_lease(admin, lease["id"], idempotency_key="renew")
    assert error.value.code == "provider_deadline_not_extendable"
    assert service.record_lease_heartbeat(admin, lease["id"])["recorded"]
    assert record(service).expires_at is not None


def backdate_idle(service, lease_id, *, heartbeat_old=True):
    def edit(session):
        saved = session.scalar(select(ResourceAllocation).where(ResourceAllocation.lease_id == lease_id))
        saved.idle_since = utcnow() - timedelta(seconds=service.inventory.idle_lease_reclaim_seconds + 10)
        lease = session.get(Lease, lease_id)
        if heartbeat_old:
            lease.last_heartbeat_at = saved.idle_since
    service._write(edit)


@pytest.mark.parametrize("running,stale,heartbeat_old", [(1, False, True), (0, True, True), (0, False, False), (None, False, True)])
def test_whole_allocation_is_not_reclaimed_without_full_evidence(service, admin, provider, monkeypatch, running, stale, heartbeat_old):
    lease = claim(service, admin)["lease"]
    backdate_idle(service, lease["id"], heartbeat_old=heartbeat_old)
    fact = allocation_fact(record(service).id, running=running)
    if stale:
        fact["workload_observed_at"] = (utcnow() - timedelta(hours=2)).isoformat()
    releases = []
    monkeypatch.setattr("serverpilot.plugins.release_plugin", lambda *args, **kwargs: releases.append(kwargs))
    ingest(service, [fact])
    assert not releases
    assert record(service).state == "active"
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "HELD"


def test_whole_allocation_reclaims_only_after_lost_heartbeat_and_confirmed_idle(service, admin, provider, monkeypatch):
    lease = claim(service, admin)["lease"]
    backdate_idle(service, lease["id"])
    releases = []
    monkeypatch.setattr("serverpilot.plugins.release_plugin", lambda *args, **kwargs: releases.append(kwargs) or {"state": "released"})
    ingest(service, [allocation_fact(record(service).id)])
    assert releases == [{"allocation_ref": "901"}]
    assert record(service).state == "released"
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "RELEASED"


def test_disconnection_breaks_idle_evidence_and_prevents_endpoint_deletion(service, admin, provider):
    lease = claim(service, admin)["lease"]
    backdate_idle(service, lease["id"])
    service.record_provider_failure("endpoint-a", "session missing", "session_missing")
    assert record(service).idle_since is None
    with pytest.raises(BrokerError) as error:
        service.delete_endpoint(admin, "endpoint-a", idempotency_key="delete")
    assert error.value.code == "endpoint_has_active_allocations"


def test_external_allocation_is_visible_but_never_claimed(service, admin, provider):
    fact = allocation_fact("external")
    fact["request_id"] = None
    fact["entry"] = None
    ingest(service, [fact])
    assert service.snapshot(admin)["data"]["leases"] == []
    endpoint = service.snapshot(admin)["data"]["endpoints"][0]
    assert endpoint["external_allocations"][0]["allocation_ref"] == "901"
    assert service._read(lambda session: session.scalars(select(ResourceAllocation)).all()) == []


def test_connection_requires_explicit_intent_then_recovers_one_session(service, admin, provider, monkeypatch):
    connections = []
    def connect(plugin_id, *, enabled):
        connections.append(enabled)
        return {"state": "connected" if enabled else "disconnected", "error_code": None, "message": "ok"}
    monkeypatch.setattr("serverpilot.plugins.set_plugin_connection", connect)
    asyncio.run(service.maintain_connections())
    assert connections == []
    asyncio.run(service.set_connection(admin, "endpoint-a", enabled=True))
    assert connections == [True]
    service.record_provider_failure("endpoint-a", "session gone", "session_missing")
    asyncio.run(service.maintain_connections())
    assert connections == [True, True]
    asyncio.run(service.maintain_connections())
    assert connections == [True, True]
    asyncio.run(service.set_connection(admin, "endpoint-a", enabled=False))
    service.record_provider_failure("endpoint-a", "session gone", "session_missing")
    asyncio.run(service.maintain_connections())
    assert connections == [True, True, False]


def test_auth_failure_stops_recovery_and_preserves_allocation(service, admin, provider, monkeypatch):
    lease = claim(service, admin)["lease"]
    calls = []
    def rejected(plugin_id, *, enabled):
        calls.append(enabled)
        return {"state": "action_required", "error_code": "auth_failed", "message": "authentication rejected"}
    monkeypatch.setattr("serverpilot.plugins.set_plugin_connection", rejected)
    asyncio.run(service.set_connection(admin, "endpoint-a", enabled=True))
    asyncio.run(service.maintain_connections())
    asyncio.run(service.maintain_connections())
    assert calls == [True]
    assert record(service).lease_id == lease["id"]
    assert record(service).state == "active"
    # Saved intent and failure state survive a daemon service instance.
    from serverpilot.service import BrokerService
    restarted = BrokerService(service.database, service.inventory)
    try:
        asyncio.run(restarted.maintain_connections())
        assert calls == [True]
        assert restarted.connection_status(admin, "endpoint-a")["data"]["enabled"]
    finally:
        restarted.shutdown()


def test_old_sample_does_not_become_fresh_by_reading_it_again(service, admin, provider):
    lease = claim(service, admin)["lease"]
    fact = allocation_fact(record(service).id)
    fact["workload_observed_at"] = (utcnow() - timedelta(hours=1)).isoformat()
    ingest(service, [fact])
    projected = service.snapshot(admin)["data"]["leases"][0]["allocations"][0]
    assert projected["fresh"] is True
    assert projected["workload_fresh"] is False
    status = mcp_server._routine_gpu_status(service.snapshot(admin), lease_id=lease["id"])
    assert status["open_leases"][0]["running_gpu_count"] is None
    assert "gpu_utilization_pct" not in status["leased_gpus"][0]


def test_old_capacity_does_not_become_fresh_on_new_read(service, admin, provider):
    from serverpilot.collector import parse_server_script_snapshot
    raw = json.loads(provider_snapshot([]))
    raw["scheduler"]["observed_at"] = (utcnow() - timedelta(hours=2)).isoformat()
    service.ingest_observation(parse_server_script_snapshot(json.dumps(raw), endpoint_id="endpoint-a", observed_at=utcnow()))
    endpoint = next(item for item in service.snapshot(admin)["data"]["endpoints"] if item["id"] == "endpoint-a")
    assert endpoint["scheduler_capacity"] is None
    assert endpoint["nodes"][0]["name"] == "node07"


def test_unknown_remote_result_preserves_known_location(service, admin, provider):
    claim(service, admin)
    saved = record(service)
    fact = allocation_fact(saved.id)
    fact.update(state="unknown", locations=[], entry=None, workload_observed_at=None)
    ingest(service, [fact])
    result = record(service)
    assert result.state == "unconfirmed"
    assert json.loads(result.locations_json)[0]["node"] == "node07"


def test_provider_inventory_is_complete_without_login_node_gpu_probe(service, admin, provider):
    ingest(service, [])
    endpoint = next(item for item in service.snapshot(admin)["data"]["endpoints"] if item["id"] == "endpoint-a")
    assert endpoint["monitor"]["status"] == "ONLINE"
    assert endpoint["scheduler_capacity"]["free_gpu_count"] == 4


@pytest.mark.parametrize("changed", ["partial", "contradiction", "unknown"])
def test_reclaim_requires_complete_consistent_active_allocation(service, admin, provider, monkeypatch, changed):
    lease = claim(service, admin)["lease"]
    backdate_idle(service, lease["id"])
    fact = allocation_fact(record(service).id)
    if changed == "partial":
        fact["locations"][0]["gpu_count"] = 1
        fact["locations"][0]["devices"].pop()
    elif changed == "contradiction":
        fact["locations"][0]["devices"][0]["running_process_count"] = 1
    else:
        fact["state"] = "unknown"
    releases = []
    monkeypatch.setattr("serverpilot.plugins.release_plugin", lambda *args, **kwargs: releases.append(kwargs))
    ingest(service, [fact])
    assert not releases
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "HELD"
