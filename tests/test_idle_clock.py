"""A card's idle clock counts what was seen, not how long the wall clock ran."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import Alert, Lease, LeaseResource
from serverpilot.timeutil import utcnow
from tests.helpers import observation
from tests.test_service import _make_persistent, request_data


class Clock:
    def __init__(self) -> None:
        self.now = utcnow() - timedelta(days=1)

    def tick(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> Clock:  # type: ignore[no-untyped-def]
    fake = Clock()
    monkeypatch.setattr("serverpilot.service.utcnow", lambda: fake.now)
    return fake


def _report(service, clock: Clock, endpoint_id: str = "endpoint-a") -> None:  # type: ignore[no-untyped-def]
    service.ingest_observation(observation(endpoint_id, count=1, observed_at=clock.now))


def _idle_claim(service, admin, clock: Clock) -> str:  # type: ignore[no-untyped-def]
    """A staged job: holds a card, runs nothing, and was watched for 12 minutes."""

    _report(service, clock)
    lease_id = service.create_request(admin, request_data("staged"), idempotency_key="staged")["lease"]["id"]
    _make_persistent(service, lease_id)
    for _ in range(72):
        clock.tick(10)
        _report(service, clock)
    return lease_id


def _lease_state(service, lease_id: str) -> str:  # type: ignore[no-untyped-def]
    return service._read(lambda session: session.get(Lease, lease_id).state)


def _observed_idle_seconds(service, clock: Clock) -> float | None:  # type: ignore[no-untyped-def]
    def read(session):  # type: ignore[no-untyped-def]
        anchor = service._idle_anchor(session.scalar(select(LeaseResource)), clock.now)
        return None if anchor is None else (clock.now - anchor).total_seconds()

    return service._read(read)


def _reminder_state(service, lease_id: str) -> str | None:  # type: ignore[no-untyped-def]
    return service._read(
        lambda session: session.scalar(
            select(Alert.state)
            .where(Alert.dedupe_key == f"idle:{lease_id}")
            .order_by(Alert.first_seen_at.desc())
        )
    )


@pytest.mark.parametrize("first_probe_fails", [False, True])
def test_a_sleeping_mac_does_not_count_as_idle_time(service, admin, clock, first_probe_fails) -> None:
    """Nothing runs while the lid is closed, not even a failure to record."""

    lease_id = _idle_claim(service, admin, clock)

    clock.tick(8 * 3600)
    if first_probe_fails:
        service.record_provider_failure("endpoint-a", "dead ssh master", "timeout")
        clock.tick(10)
    _report(service, clock)

    assert _lease_state(service, lease_id) == "HELD"
    assert _observed_idle_seconds(service, clock) < 60


def test_a_blip_pauses_the_clock_instead_of_starting_it_over(service, admin, clock) -> None:
    """A flaky link must not mean a forgotten claim is never returned."""

    _idle_claim(service, admin, clock)
    before = _observed_idle_seconds(service, clock)

    for _ in range(12):  # two minutes, with another server still reporting
        clock.tick(10)
        _report(service, clock, "endpoint-b")
    _report(service, clock)

    after = _observed_idle_seconds(service, clock)
    assert after >= before
    assert after <= before + service.collector.stale_after_seconds + 10


def test_an_outage_that_could_hide_a_job_starts_the_clock_over(service, admin, clock) -> None:
    lease_id = _idle_claim(service, admin, clock)

    for _ in range(36):  # six minutes, with another server still reporting
        clock.tick(10)
        _report(service, clock, "endpoint-b")

    assert _observed_idle_seconds(service, clock) is None
    assert _lease_state(service, lease_id) == "HELD"
    _report(service, clock)
    assert _observed_idle_seconds(service, clock) == 0


def test_a_reminder_stands_while_its_cards_cannot_be_seen(service, admin, clock) -> None:
    """Nothing being known is not a recovery."""

    lease_id = _idle_claim(service, admin, clock)
    assert _reminder_state(service, lease_id) == "active"

    for _ in range(36):
        clock.tick(10)
        _report(service, clock, "endpoint-b")
    assert _reminder_state(service, lease_id) == "active"

    # Back in view with a fresh clock, there is no longer a long idle to report.
    clock.tick(20)
    _report(service, clock)
    assert _reminder_state(service, lease_id) == "resolved"
