"""Debounce and rate limits, moved by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from serverpilot import alerts
from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig, ServerGroupConfig
from serverpilot.database import Database
from serverpilot.models import Alert
from serverpilot.schemas import RequestCreate
from serverpilot.service import ActorContext, BrokerService
from serverpilot.timeutil import utcnow
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


class _Clock:
    def __init__(self) -> None:
        self.now = utcnow() - timedelta(days=1)

    def watch(self, service, seconds: int, *endpoints: str) -> None:  # type: ignore[no-untyped-def]
        for _ in range(seconds // 10):
            self.now += timedelta(seconds=10)
            for endpoint in endpoints:
                service.ingest_observation(observation(endpoint, count=1, observed_at=self.now))


@pytest.fixture
def clock(monkeypatch) -> _Clock:  # type: ignore[no-untyped-def]
    fake = _Clock()
    monkeypatch.setattr("serverpilot.service.utcnow", lambda: fake.now)
    return fake


def test_an_outage_nobody_was_told_about_leaves_no_record(service, clock) -> None:  # type: ignore[no-untyped-def]
    """Waking the Mac must not fill the inbox with servers that were never down."""

    clock.watch(service, 20, "endpoint-a", "endpoint-b")
    clock.now += timedelta(hours=8)
    clock.watch(service, 60, "endpoint-a", "endpoint-b")

    assert not [row for row in service.list_reminders() if row["type"] == "server_offline"]


def test_a_server_going_up_and_down_is_not_a_banner_per_outage(service, admin, clock) -> None:  # type: ignore[no-untyped-def]
    clock.watch(service, 20, "endpoint-a", "endpoint-b")
    told = 0
    for _ in range(4):  # about 28 minutes
        clock.watch(service, 370, "endpoint-b")
        pending = [row for row in service.pending_notifications() if row["type"] == "server_offline"]
        told += len(pending)
        if pending:
            service.mark_reminders(admin, [row["id"] for row in pending], notified=True)
        clock.watch(service, 60, "endpoint-a", "endpoint-b")
    assert told == 1

    clock.watch(service, alerts.NOTIFY_REPEAT_SECONDS, "endpoint-a", "endpoint-b")
    clock.watch(service, 370, "endpoint-b")
    assert [row for row in service.pending_notifications() if row["type"] == "server_offline"]


@pytest.fixture
def grouped(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    inventory = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="A", weight=1)],
        server_groups=[ServerGroupConfig(id="group-a", display_name="G", workspace_path="/srv/x")],
        endpoints=[
            EndpointConfig(
                id=f"endpoint-{name}", host="127.0.0.1", port=2200 + index, ssh_user="gpu",
                labels=["t"], storage_group="s", project_ids=["project-a"], server_group_id="group-a",
            )
            for index, name in enumerate("ab")
        ],
    )
    root = Path(__file__).resolve().parents[1]
    service = BrokerService(Database(f"sqlite:///{tmp_path}/grouped.sqlite3", root), inventory)
    service.initialize()
    service.local_actor("admin")
    admin = ActorContext(id="admin", role="admin", project_ids=frozenset({"project-a"}))
    return service, admin


def _empty_then_free_one_card(service, admin, clock) -> None:  # type: ignore[no-untyped-def]
    both = ("endpoint-a", "endpoint-b")
    clock.watch(service, 20, *both)
    leases = [
        service.create_request(
            admin,
            RequestCreate.model_validate({
                "project_id": "project-a", "task_ref": f"task-{index}", "purpose": "p",
                "constraints": {"gpu_count": 1, "placement": "pack", "server_group_ids": ["group-a"]},
            }),
            idempotency_key=f"task-{index}",
        )["lease"]
        for index in range(2)
    ]
    clock.watch(service, 310, *both)  # past the settling after a start, and seen empty
    on_a = next(lease for lease in leases if any(gpu.startswith("endpoint-a") for gpu in lease["gpu_ids"]))
    service.release_lease(admin, on_a["id"], reason="workload_completed", idempotency_key="release")


def _capacity_state(service) -> str | None:  # type: ignore[no-untyped-def]
    return service._read(
        lambda session: session.scalar(
            select(Alert.state)
            .where(Alert.dedupe_key == "capacity:group:group-a")
            .order_by(Alert.first_seen_at.desc())
        )
    )


def test_an_offline_server_does_not_silence_its_group(grouped, clock) -> None:  # type: ignore[no-untyped-def]
    service, admin = grouped
    _empty_then_free_one_card(service, admin, clock)

    clock.watch(service, 480, "endpoint-a")

    assert _capacity_state(service) == "active"


def test_a_server_gone_quiet_for_a_moment_still_holds_the_answer(grouped, clock) -> None:  # type: ignore[no-untyped-def]
    """Its return must not read as cards appearing."""

    service, admin = grouped
    _empty_then_free_one_card(service, admin, clock)

    clock.watch(service, 200, "endpoint-a")

    assert _capacity_state(service) != "active"


def test_notices_rest_after_being_told() -> None:
    assert alerts.rested(None, at(0))
    assert not alerts.rested(at(0), at(alerts.NOTIFY_REPEAT_SECONDS - 1))
    assert alerts.rested(at(0), at(alerts.NOTIFY_REPEAT_SECONDS))
