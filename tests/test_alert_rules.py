"""Debounce and rate limits, moved by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from serverpilot import alerts
from serverpilot.models import Alert
from tests.helpers import observation

T0 = datetime(2026, 10, 30, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_a_brief_outage_never_surfaces() -> None:
    first = alerts.step_present("pending", None, hold_seconds=300, now=at(0))
    assert first.state == "pending"
    held = alerts.step_present(first.state, first.pending_since, hold_seconds=300, now=at(120))
    assert held.state == "pending"
    assert alerts.step_absent(held.state, held.pending_since, clear_streak=0, clear_successes=2).state == "resolved"


def test_recovery_needs_two_successes_once_it_surfaced() -> None:
    active = alerts.step_present("pending", at(0), hold_seconds=300, now=at(300))
    assert active.state == "active"
    once = alerts.step_absent("active", at(0), clear_streak=0, clear_successes=2)
    assert once.state == "active"
    assert alerts.step_absent("active", at(0), clear_streak=once.clear_streak, clear_successes=2).state == "resolved"


def test_capacity_fires_on_the_edge_only_after_it_settles() -> None:
    def step(state, since, free, seconds, notified=None):  # type: ignore[no-untyped-def]
        return alerts.step_capacity(state, since, free_cards=free, last_notified_at=notified, now=at(seconds))

    assert step(None, None, 3, 0).state == "resolved"  # never seen empty: no edge
    armed = step(None, None, 0, 0)
    assert armed.state == alerts.ARMED
    pending = step(armed.state, None, 2, 10)
    assert pending.state == "pending"
    assert step(pending.state, pending.pending_since, 2, 100).state == "pending"
    assert step(pending.state, pending.pending_since, 2, 130).state == "active"
    # It emptied again before settling: back to waiting, nothing said.
    assert step(pending.state, pending.pending_since, 0, 60).state == alerts.ARMED


def test_capacity_rests_thirty_minutes_between_notices() -> None:
    step = alerts.step_capacity("pending", at(0), free_cards=1, last_notified_at=at(0), now=at(600))
    assert step.state == "pending"
    step = alerts.step_capacity("pending", at(0), free_cards=1, last_notified_at=at(0), now=at(1800))
    assert step.state == "active"


def test_groups_freed_together_arrive_as_one_notice() -> None:
    def item(name: str, seconds: int) -> dict:
        return {"id": name, "category": alerts.CAPACITY_AVAILABLE, "first_seen_at": at(seconds).isoformat(), "message": name}

    merged = alerts.coalesce([item("a", 0), item("b", 30), item("c", 200)])
    assert len(merged) == 2
    assert merged[0]["alert_ids"] == ["a", "b"]


def _rows(service):  # type: ignore[no-untyped-def]
    return service._read(lambda session: list(session.scalars(
        select(Alert).where(Alert.dedupe_key.is_not(None))
    ).all()))


def test_reminders_never_enter_the_blocking_alert_list(service) -> None:  # type: ignore[no-untyped-def]
    service.ingest_observation(observation("endpoint-a", count=2))
    assert all(row.active is False for row in _rows(service))
    assert not service._read(lambda session: list(session.scalars(
        select(Alert).where(Alert.active.is_(True))
    ).all()))
