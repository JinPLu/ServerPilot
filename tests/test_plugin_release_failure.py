"""A cluster allocation the automatic path cannot release must not vanish silently.

An explicit release refuses with ``plugin_release_failed`` and keeps the lease,
so the caller learns about it. Idle reclaim during observation has no caller to
answer and cannot refuse: it drops the last local reference to a job still
running against the user's cluster quota. There is no logger in the service, so
the audit trail is the only place a human or an agent can find that leak.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from serverpilot.models import AuditEvent
from serverpilot.plugins import PluginError
from tests.helpers import observation
from tests.test_plugin_own_allocation import PLUGIN_OVERLAY, _use_plugin_profile
from tests.test_service import _backdate_idle_since, _make_persistent, request_data


def _release_failures(service):  # type: ignore[no-untyped-def]
    def read(session):  # type: ignore[no-untyped-def]
        return [
            (event.resource_id, event.result, event.summary_json)
            for event in session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "plugin.release_failed")
                .order_by(AuditEvent.id)
            ).all()
        ]

    return service._read(read)


def _reclaim_an_idle_plugin_lease(service, admin, task: str) -> None:
    _use_plugin_profile(service)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))
    allocated = service.create_request(
        admin,
        request_data(task),
        idempotency_key=task,
        plugin_allocation=PLUGIN_OVERLAY,
    )
    lease_id = allocated["lease"]["id"]
    _make_persistent(service, lease_id)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))
    _backdate_idle_since(service, lease_id, service.inventory.idle_lease_reclaim_seconds + 5)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))


def test_a_refused_release_during_idle_reclaim_is_recorded(
    service, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "serverpilot.plugins.release_plugin",
        lambda *args, **kwargs: (_ for _ in ()).throw(PluginError("scancel refused: quota locked")),
    )

    _reclaim_an_idle_plugin_lease(service, admin, "leaky-reclaim")

    recorded = _release_failures(service)
    assert len(recorded) == 1
    resource_id, result, summary = recorded[0]
    assert resource_id == PLUGIN_OVERLAY["allocation_ref"]
    assert result == "failure"
    assert "quota locked" in summary
    assert "slurm-immediate" in summary


def test_an_explicit_release_still_refuses_instead_of_recording(
    service, admin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclaim records the leak; an explicit release must keep failing closed."""

    from serverpilot.service import BrokerError

    _use_plugin_profile(service)
    service.ingest_observation(observation(count=1, gpu_uuids=["GPU-real"]))
    monkeypatch.setattr(
        "serverpilot.plugins.release_plugin",
        lambda *args, **kwargs: (_ for _ in ()).throw(PluginError("scancel refused")),
    )
    allocated = service.create_request(
        admin,
        request_data("explicit-refusal"),
        idempotency_key="explicit-refusal",
        plugin_allocation=PLUGIN_OVERLAY,
    )

    with pytest.raises(BrokerError) as caught:
        service.release_lease(
            admin,
            allocated["lease"]["id"],
            reason="done",
            idempotency_key="explicit-refusal-out",
        )

    assert caught.value.code == "plugin_release_failed"


def test_one_uncooperative_plugin_does_not_keep_the_others_allocated(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every entry is attempted, so one refusal cannot strand the rest."""

    attempted: list[str] = []

    def flaky_release(plugin_id: str, *, allocation_ref: str) -> dict[str, str]:
        attempted.append(allocation_ref)
        if allocation_ref == "1134001":
            raise PluginError("scancel refused")
        return {"state": "released"}

    monkeypatch.setattr("serverpilot.plugins.release_plugin", flaky_release)

    service._release_plugin_allocations(
        [
            {"plugin_id": "slurm-immediate", "allocation_ref": "1134001"},
            {"plugin_id": "slurm-immediate", "allocation_ref": "1134002"},
        ]
    )

    assert attempted == ["1134001", "1134002"]
    assert [entry[0] for entry in _release_failures(service)] == ["1134001"]
