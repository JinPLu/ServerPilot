"""Decide when a condition becomes something worth telling a person.

Pure rules, no database and no clock of their own, so debouncing and rate
limits can be tested by moving time by hand. ``service.py`` gathers the facts
and owns the rows. These alerts live beside the older ``active`` ones, which
admission and repair still read: a row here has a ``dedupe_key`` and is never
``active``, so nothing that blocks a claim can start depending on a reminder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

TASK_IMPACTED = "task_impacted"
SERVER_ABNORMAL = "server_abnormal"
RESOURCE_IDLE = "resource_idle"
CAPACITY_AVAILABLE = "capacity_available"

OPEN_STATES = ("armed", "pending", "active", "acked")
#: Waiting for a group to have cards. Invisible: it is the memory that the
#: group was empty, which is what makes the next free card an edge.
ARMED = "armed"

OFFLINE_HOLD_SECONDS = 300
KEEPALIVE_FAULT_HOLD_SECONDS = 300
UNCONFIRMED_HOLD_SECONDS = 60
RECOVERY_SUCCESSES = 2
AT_RISK_LEAD_SECONDS = 600
EXPIRY_LEAD_SECONDS = 1800
OVERRUN_FACTOR = 2

CAPACITY_STABLE_SECONDS = 120
CAPACITY_REPEAT_SECONDS = 1800
COALESCE_SECONDS = 60
#: Older than this and a missed alert is history, not news.
NOTIFY_WITHIN_SECONDS = 86_400
EVALUATE_EVERY_SECONDS = 15


@dataclass(frozen=True)
class Signal:
    """A condition that is true right now."""

    key: str
    category: str
    alert_type: str
    severity: str
    resource_type: str
    resource_id: str
    message: str
    hold_seconds: int = 0
    clear_successes: int = 1
    #: Recorded for the inbox, never pushed.
    silent: bool = False
    lease_id: str | None = None
    project_id: str | None = None
    task_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    state: str
    pending_since: datetime | None
    clear_streak: int = 0


def step_present(state: str, pending_since: datetime | None, *, hold_seconds: int, now: datetime) -> Step:
    """Advance a row whose condition holds."""

    if state in {"active", "acked"}:
        return Step(state, pending_since)
    since = pending_since or now
    if now - since >= timedelta(seconds=hold_seconds):
        return Step("active", since)
    return Step("pending", since)


def step_absent(state: str, pending_since: datetime | None, *, clear_streak: int, clear_successes: int) -> Step:
    """Advance a row whose condition no longer holds."""

    if state == "pending":
        return Step("resolved", None)
    streak = clear_streak + 1
    if streak >= clear_successes:
        return Step("resolved", pending_since, streak)
    return Step(state, pending_since, streak)


def step_capacity(
    state: str | None,
    pending_since: datetime | None,
    *,
    free_cards: int,
    last_notified_at: datetime | None,
    now: datetime,
) -> Step:
    """Fire once when a group goes from no cards to some and stays that way."""

    if free_cards <= 0:
        if state in {"active", "acked"}:
            return Step("resolved", None)
        return Step(ARMED, None)
    if state is None or state in {"active", "acked"}:
        # Never seen empty, or already told: not an edge.
        return Step(state or "resolved", pending_since)
    since = pending_since or now
    settled = now - since >= timedelta(seconds=CAPACITY_STABLE_SECONDS)
    rested = last_notified_at is None or now - last_notified_at >= timedelta(
        seconds=CAPACITY_REPEAT_SECONDS
    )
    return Step("active" if settled and rested else "pending", since)


def coalesce(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold capacity alerts raised within a minute of each other into one."""

    others = [item for item in alerts if item["category"] != CAPACITY_AVAILABLE]
    capacity = sorted(
        (item for item in alerts if item["category"] == CAPACITY_AVAILABLE),
        key=lambda item: item["first_seen_at"],
    )
    batches: list[list[dict[str, Any]]] = []
    for item in capacity:
        if batches and (
            datetime.fromisoformat(item["first_seen_at"])
            - datetime.fromisoformat(batches[-1][0]["first_seen_at"])
        ) <= timedelta(seconds=COALESCE_SECONDS):
            batches[-1].append(item)
        else:
            batches.append([item])
    for batch in batches:
        head = dict(batch[0])
        if len(batch) > 1:
            head["message"] = f"{len(batch)} 组服务器有空闲 GPU"
            head["alert_ids"] = [item["id"] for item in batch]
        others.append(head)
    return others
