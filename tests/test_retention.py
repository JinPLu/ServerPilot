"""Every table that accumulates has a window, and a live lease survives all of them."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, text

from serverpilot import retention
from serverpilot.models import Alert, AuditEvent, IdempotencyRecord, TelemetrySnapshot
from serverpilot.service import BrokerService
from serverpilot.timeutil import utcnow
from tests.helpers import observation


def _count(service: BrokerService, model) -> int:
    with service.database.session() as session:
        return len(session.scalars(select(model)).all())


def test_every_window_is_a_positive_number_of_seconds() -> None:
    """A window of zero or None would silently mean "delete everything"."""

    windows = {
        name: value
        for name, value in vars(retention).items()
        if name.endswith("_SECONDS") and not name.startswith("_")
    }
    assert windows
    for name, value in windows.items():
        assert isinstance(value, int) and value > 0, name


def test_telemetry_history_is_kept_for_its_window_and_no_longer(
    service: BrokerService, admin
) -> None:
    service.ingest_observation(observation(count=1))
    assert _count(service, TelemetrySnapshot) >= 1

    with service.database.session() as session:
        for snapshot in session.scalars(select(TelemetrySnapshot)).all():
            snapshot.observed_at = utcnow() - timedelta(
                seconds=retention.TELEMETRY_SECONDS + 60
            )
        session.commit()

    deleted = service.prune_expired()
    assert deleted["telemetry_snapshots"] >= 1
    assert _count(service, TelemetrySnapshot) == 0


def test_a_replay_key_older_than_its_window_is_dropped(
    service: BrokerService, admin
) -> None:
    """The client that could still send this key gave up on the call long ago."""

    with service.database.session() as session:
        session.add(
            IdempotencyRecord(
                actor_id=admin.id,
                action="test.action",
                key="stale-key",
                response_json="{}",
                created_at=utcnow() - timedelta(seconds=retention.IDEMPOTENCY_SECONDS + 60),
            )
        )
        session.add(
            IdempotencyRecord(
                actor_id=admin.id,
                action="test.action",
                key="fresh-key",
                response_json="{}",
                created_at=utcnow(),
            )
        )
        session.commit()

    service.prune_expired()
    with service.database.session() as session:
        remaining = {item.key for item in session.scalars(select(IdempotencyRecord)).all()}
    assert remaining == {"fresh-key"}


def test_audit_history_older_than_its_window_is_dropped(
    service: BrokerService, admin
) -> None:
    service.ingest_observation(observation(count=1))
    with service.database.session() as session:
        events = session.scalars(select(AuditEvent)).all()
        for event in events[:1]:
            event.created_at = utcnow() - timedelta(seconds=retention.AUDIT_SECONDS + 60)
        expected_removed = 1 if events else 0
        session.commit()

    deleted = service.prune_expired()
    assert deleted["audit_events"] == expected_removed


def test_an_active_alert_is_never_pruned_however_old_it_is(
    service: BrokerService, admin
) -> None:
    """Age does not end a condition. Only resolution does."""

    old = utcnow() - timedelta(seconds=retention.RESOLVED_ALERT_SECONDS * 10)
    with service.database.session() as session:
        session.add(
            Alert(
                id="still-happening",
                alert_type="collector_unreachable",
                severity="warning",
                resource_type="endpoint",
                resource_id="endpoint-a",
                message="still happening",
                active=True,
                first_seen_at=old,
                last_seen_at=old,
            )
        )
        session.add(
            Alert(
                id="over-and-done",
                alert_type="collector_unreachable",
                severity="warning",
                resource_type="endpoint",
                resource_id="endpoint-b",
                message="over and done",
                active=False,
                first_seen_at=old,
                last_seen_at=old,
            )
        )
        session.commit()

    service.prune_expired()
    with service.database.session() as session:
        remaining = {item.id for item in session.scalars(select(Alert)).all()}
    assert "still-happening" in remaining
    assert "over-and-done" not in remaining


def test_pruning_leaves_current_telemetry_and_live_leases_alone(
    service: BrokerService, admin
) -> None:
    """A retention pass must never be able to take a card away from its holder."""

    service.ingest_observation(observation(count=2))
    before = service.snapshot(admin)["data"]
    service.prune_expired()
    after = service.snapshot(admin)["data"]

    assert [gpu["id"] for gpu in after["gpus"]] == [gpu["id"] for gpu in before["gpus"]]
    assert len(after["leases"]) == len(before["leases"])
    assert [endpoint["id"] for endpoint in after["endpoints"]] == [
        endpoint["id"] for endpoint in before["endpoints"]
    ]


def test_space_is_reclaimed_only_when_enough_of_the_file_is_free(
    service: BrokerService, admin
) -> None:
    """Rewriting the whole file to recover a little of it is not worth doing.

    Both directions matter. A predicate that never fires makes the rewrite dead
    code; one that always fires rewrites the database every hour for nothing.
    """

    from serverpilot import retention as policy

    service.ingest_observation(observation(count=4))
    def free_fraction() -> float:
        with service.database.session() as session:
            free = session.execute(text("PRAGMA freelist_count")).scalar() or 0
            total = session.execute(text("PRAGMA page_count")).scalar() or 1
        return free / total

    # Nothing has been deleted, so there is nothing worth reclaiming.
    service.database.reclaim_space()
    assert free_fraction() < policy.VACUUM_FREE_FRACTION

    with service.database.session() as session:
        session.execute(text("CREATE TABLE bulk (id INTEGER PRIMARY KEY, blob TEXT)"))
        session.execute(
            text("INSERT INTO bulk (blob) SELECT hex(randomblob(2000)) FROM pragma_page_count()")
        )
        for _ in range(11):
            session.execute(text("INSERT INTO bulk (blob) SELECT blob FROM bulk"))
        session.commit()
    with service.database.session() as session:
        session.execute(text("DELETE FROM bulk"))
        session.commit()
    assert free_fraction() >= policy.VACUUM_FREE_FRACTION

    # Now it is worth rewriting, and the freed pages go back to the filesystem.
    service.database.reclaim_space()
    assert free_fraction() < policy.VACUUM_FREE_FRACTION
