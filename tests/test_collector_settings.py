from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot.models import RuntimeSetting
from serverpilot.schemas import CollectorSettingsUpdate
from serverpilot.service import BrokerService
from tests.helpers import observation


def test_collector_setting_is_persisted_and_restored(service, admin, inventory, tmp_path: Path) -> None:
    initial = service.collector_settings(admin)["data"]
    assert initial == {
        "interval_seconds": 10,
        "stale_after_seconds": 30,
        "allowed_intervals": [5, 10, 30],
    }

    changed = service.update_collector_settings(
        admin,
        CollectorSettingsUpdate(interval_seconds=5),
        idempotency_key="collector-interval-5",
    )
    assert changed["settings"]["interval_seconds"] == 5
    assert changed["settings"]["stale_after_seconds"] == 15
    assert service.collector_interval_seconds() == 5

    replay = service.update_collector_settings(
        admin,
        CollectorSettingsUpdate(interval_seconds=5),
        idempotency_key="collector-interval-5",
    )
    assert replay["event_id"] == changed["event_id"]

    with service.database.session() as session:
        setting = session.scalar(select(RuntimeSetting))
        assert setting is not None
        assert setting.value == "5"

    restarted_inventory = inventory.model_copy(deep=True)
    restarted_inventory.collector.interval_seconds = 10
    restarted_inventory.collector.stale_after_seconds = 30
    restarted = BrokerService(service.database, restarted_inventory)
    restarted.initialize()
    assert restarted.collector_interval_seconds() == 5
    assert restarted.inventory.collector.stale_after_seconds == 15


def test_collector_setting_commit_failure_does_not_mutate_runtime(
    service, admin, monkeypatch
) -> None:
    session_type = service.database.Session.class_

    def fail_commit(_session) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("forced commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(session_type, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="forced commit failure"):
            service.update_collector_settings(
                admin,
                CollectorSettingsUpdate(interval_seconds=5),
                idempotency_key="collector-interval-commit-failure",
            )

    assert service.collector_interval_seconds() == 10
    assert service.inventory.collector.stale_after_seconds == 30
    with service.database.session() as session:
        assert session.get(RuntimeSetting, "collector_interval_seconds") is None


def test_collector_settings_api_requires_supported_value_and_idempotency(build_app) -> None:
    app = build_app(
        "api",
        project_root=Path(__file__).resolve().parents[1],
    )
    headers = {"X-ServerPilot-Actor": "bootstrap-admin"}
    with TestClient(app) as client:
        response = client.get("/api/v1/settings/collector", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["interval_seconds"] == 10

        response = client.patch(
            "/api/v1/settings/collector",
            headers=headers,
            json={"interval_seconds": 5},
        )
        assert response.status_code == 422

        response = client.patch(
            "/api/v1/settings/collector",
            headers={**headers, "Idempotency-Key": "settings-5"},
            json={"interval_seconds": 7},
        )
        assert response.status_code == 422

        response = client.patch(
            "/api/v1/settings/collector",
            headers={**headers, "Idempotency-Key": "settings-5"},
            json={"interval_seconds": 5},
        )
        assert response.status_code == 200
        assert response.json()["settings"]["stale_after_seconds"] == 15


def test_failing_endpoint_is_backed_off_but_healthy_ones_stay_every_cycle(
    service, admin
) -> None:
    """A host that stopped answering must not cost a connect timeout per cycle.

    Backoff changes only the probe rhythm. Admission is already fail-closed
    through stale telemetry, so the endpoint stays in the inventory every other
    caller sees, and it returns to the regular cycle on its first success.
    """

    from datetime import timedelta

    from sqlalchemy import select

    from serverpilot.models import ProviderState
    from serverpilot.service import DEGRADED_ENDPOINT_PROBE_SECONDS
    from serverpilot.timeutil import utcnow

    service.ingest_observation(observation("endpoint-a", count=1))
    service.ingest_observation(observation("endpoint-b", count=1))
    assert [item.id for item in service.collector_endpoints_due()] == [
        "endpoint-a",
        "endpoint-b",
    ]

    service.record_provider_failure("endpoint-b", "CollectionError: timed out")
    with service.database.session() as session:
        state = session.scalar(
            select(ProviderState).where(ProviderState.endpoint_id == "endpoint-b")
        )
        assert state is not None
        stale = utcnow() - timedelta(
            seconds=service.inventory.collector.stale_after_seconds + 60
        )
        state.last_success_at = stale
        state.last_attempt_at = utcnow()
        session.commit()

    assert [item.id for item in service.collector_endpoints_due()] == ["endpoint-a"]
    # Every other caller still sees the full inventory: backoff is a rhythm,
    # not a lifecycle change.
    assert [item.id for item in service.collector_endpoints()] == [
        "endpoint-a",
        "endpoint-b",
    ]

    with service.database.session() as session:
        state = session.scalar(
            select(ProviderState).where(ProviderState.endpoint_id == "endpoint-b")
        )
        assert state is not None
        state.last_attempt_at = utcnow() - timedelta(
            seconds=DEGRADED_ENDPOINT_PROBE_SECONDS + 1
        )
        session.commit()

    assert [item.id for item in service.collector_endpoints_due()] == [
        "endpoint-a",
        "endpoint-b",
    ]
