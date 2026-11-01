"""The overview draws recent utilization without one request per server."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from serverpilot import API_CAPABILITIES, retention
from serverpilot.models import GPUDevice, TelemetrySnapshot
from serverpilot.service import BrokerService
from serverpilot.timeutil import utcnow
from tests.helpers import observation


def _sample(gpu_id: str, at: datetime, utilization: int | None) -> TelemetrySnapshot:
    return TelemetrySnapshot(
        gpu_id=gpu_id,
        observed_at=at,
        collected_at=at,
        memory_used_mib=0,
        memory_free_mib=100,
        gpu_utilization_pct=utilization,
    )


def _seed(service: BrokerService, rows: dict[str, list[tuple[timedelta, int | None]]]) -> None:
    """Replace history with samples per endpoint, one card at a time."""

    now = utcnow()
    with service.database.session() as session:
        session.execute(delete(TelemetrySnapshot))
        for endpoint_id, samples in rows.items():
            cards = session.scalars(
                select(GPUDevice.id).where(GPUDevice.endpoint_id == endpoint_id).order_by(GPUDevice.id)
            ).all()
            for index, (age, utilization) in enumerate(samples):
                session.add(_sample(cards[index % len(cards)], now - age, utilization))
        session.commit()


def _values(series: dict) -> list[float]:
    return [point["gpu_utilization_pct"] for point in series["points"] if point["gpu_utilization_pct"] is not None]


def test_the_fleet_line_weighs_cards_not_servers(service, admin) -> None:
    """Two busy cards on one host outweigh one idle card on another."""

    service.ingest_observation(observation("endpoint-a", count=2))
    service.ingest_observation(observation("endpoint-b", count=1))
    age = timedelta(minutes=1)
    _seed(service, {"endpoint-a": [(age, 90), (age, 90)], "endpoint-b": [(age, 0)]})

    data = service.utilization_trends(admin)["data"]

    assert _values(data["fleet"]) == [60.0]
    assert _values(data["servers"]["endpoint-a"]) == [90.0]
    assert _values(data["servers"]["endpoint-b"]) == [0.0]


def test_a_gap_stays_a_gap(service, admin) -> None:
    """A collector outage is not idle cards: the line breaks rather than reads zero."""

    service.ingest_observation(observation("endpoint-a", count=1))
    _seed(service, {"endpoint-a": [(timedelta(minutes=1), 50), (timedelta(minutes=40), 70)]})

    points = service.utilization_trends(admin)["data"]["servers"]["endpoint-a"]["points"]

    assert None in [point["gpu_utilization_pct"] for point in points]
    assert _values({"points": points}) == [70.0, 50.0]


def test_each_line_stops_at_its_window(service, admin) -> None:
    """A sample too old for the server line still counts for the day-long fleet line."""

    service.ingest_observation(observation("endpoint-a", count=1))
    _seed(
        service,
        {
            "endpoint-a": [
                (timedelta(hours=3), 40),
                (timedelta(seconds=retention.TELEMETRY_SECONDS + 3600), 99),
            ]
        },
    )

    data = service.utilization_trends(admin)["data"]

    assert _values(data["fleet"]) == [40.0]
    assert "endpoint-a" not in data["servers"]
    assert data["fleet"]["window_seconds"] == retention.TELEMETRY_SECONDS


def test_a_card_with_no_reading_does_not_pull_the_average_down(service, admin) -> None:
    service.ingest_observation(observation("endpoint-a", count=2))
    age = timedelta(minutes=1)
    _seed(service, {"endpoint-a": [(age, 80), (age, None)]})

    data = service.utilization_trends(admin)["data"]

    assert _values(data["fleet"]) == [80.0]


def test_the_route_answers_and_is_declared_as_a_capability(build_app) -> None:
    with TestClient(build_app("trends")) as client:
        response = client.get("/api/v2/utilization-trends", headers={"X-ServerPilot-Actor": "test"})

    assert response.status_code == 200
    assert set(response.json()["data"]) == {"fleet", "servers"}
    assert "utilization_trends" in API_CAPABILITIES
