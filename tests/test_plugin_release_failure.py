"""Failed remote release retains ownership for both explicit and automatic paths."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from serverpilot.models import Lease, ResourceAllocation
from serverpilot.plugins import PluginError
from serverpilot.service import BrokerError
from tests.test_provider_allocations import (
    allocation_fact,
    backdate_idle,
    claim,
    ingest,
    record,
    request,
)
from tests.test_provider_allocations import provider as provider


def refuse(*args, **kwargs):
    raise PluginError("scancel refused")


def test_idle_release_failure_keeps_remote_reference(service, admin, provider, monkeypatch):
    lease = claim(service, admin)["lease"]
    backdate_idle(service, lease["id"])
    monkeypatch.setattr("serverpilot.plugins.release_plugin", refuse)
    ingest(service, [allocation_fact(record(service).id)])
    saved = record(service)
    assert saved.state == "release_pending"
    assert saved.allocation_ref == "901"
    assert "scancel refused" in saved.last_error
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "HELD"


def test_explicit_release_failure_keeps_remote_reference(service, admin, provider, monkeypatch):
    lease = claim(service, admin)["lease"]
    monkeypatch.setattr("serverpilot.plugins.release_plugin", refuse)
    with pytest.raises(BrokerError):
        service.release_lease(admin, lease["id"], reason="done", idempotency_key="release")
    assert record(service).state == "release_pending"
    assert service._read(lambda session: session.get(Lease, lease["id"]).state) == "HELD"


def test_one_failed_idle_release_does_not_block_another(service, admin, provider, monkeypatch):
    first = claim(service, admin)["lease"]
    def second_apply(*args, **kwargs):
        fact = allocation_fact(kwargs["request_id"])
        fact["allocation_ref"] = "902"
        return fact
    monkeypatch.setattr("serverpilot.plugins.apply_plugin", second_apply)
    second = claim(service, admin, key="second", data=request(task_ref="second"))["lease"]
    backdate_idle(service, first["id"])
    backdate_idle(service, second["id"])
    attempted = []
    def release(plugin_id, *, allocation_ref):
        attempted.append(allocation_ref)
        if allocation_ref == "901":
            raise PluginError("refused")
        return {"state": "released"}
    monkeypatch.setattr("serverpilot.plugins.release_plugin", release)
    saved = service._read(lambda session: session.scalars(select(ResourceAllocation).where(ResourceAllocation.provider_id != "builtin")).all())
    facts = []
    for allocation in saved:
        fact = allocation_fact(allocation.id)
        fact["allocation_ref"] = allocation.allocation_ref
        facts.append(fact)
    ingest(service, facts)
    assert set(attempted) == {"901", "902"}
    assert service._read(lambda session: session.get(Lease, first["id"]).state) == "HELD"
    assert service._read(lambda session: session.get(Lease, second["id"]).state) == "RELEASED"
