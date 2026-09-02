from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot.models import RuntimeSetting
from serverpilot.schemas import CollectorSettingsUpdate
from serverpilot.service import BrokerService


def test_collector_setting_is_persisted_and_restored(
    service, admin, inventory, tmp_path: Path
) -> None:
    initial = service.collector_settings(admin)["data"]
    assert initial == {
        "interval_seconds": 10,
        "stale_after_seconds": 40,
        "allowed_intervals": [5, 10, 30],
    }

    changed = service.update_collector_settings(
        admin,
        CollectorSettingsUpdate(interval_seconds=5),
        idempotency_key="collector-interval-5",
    )
    assert changed["settings"]["interval_seconds"] == 5
    assert changed["settings"]["stale_after_seconds"] == 35
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
    restarted = BrokerService(service.database, restarted_inventory)
    restarted.initialize()
    assert restarted.collector_interval_seconds() == 5
    assert restarted.collector.stale_after_seconds == 35


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
    assert service.collector.stale_after_seconds == 40
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
        assert response.json()["settings"]["stale_after_seconds"] == 35


def test_the_absence_window_is_its_own_clock(service, admin) -> None:  # noqa: ANN001
    """Telemetry freshness and process absence answer different questions.

    Telemetry freshness says whether a machine's readings are current. The
    absence window says how long the endpoint's own unbroken complete
    observations must leave a process out before it stops being a fact. The
    second is measured on ``absent_since``, which an outage clears, so it needs
    no ordering against the first, and it is unaffected by ``interval_seconds``
    changes since ``stale_after_seconds`` is now derived from the interval
    rather than settable independently.
    """

    from serverpilot.config import CollectorConfig, CollectorSettings

    default = CollectorConfig()
    assert default.process_absence_grace_seconds == 60

    widened = CollectorSettings.resolved(CollectorConfig(interval_seconds=10))
    assert widened.stale_after_seconds == 40
    assert widened.process_absence_grace_seconds == 60

    service.update_collector_settings(
        admin,
        CollectorSettingsUpdate(interval_seconds=30),
        idempotency_key="collector-interval-30-order",
    )
    assert service.collector.stale_after_seconds == 60
    assert service.collector.process_absence_grace_seconds == 60


