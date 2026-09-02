"""One value answers "what do we know about this endpoint, and how old is it".

Four separate derivations used to answer it, and where they disagreed was
exactly where a healthy host was reported as failing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from serverpilot.freshness import freshness_of

NOW = datetime(2026, 9, 3, 12, 0, 0)
STALE_AFTER = 35


def _freshness(*, observed_ago: float | None, error_code: str | None):
    observed_at = None if observed_ago is None else NOW - timedelta(seconds=observed_ago)
    return freshness_of(
        observed_at=observed_at,
        attempted_at=NOW,
        error_code=error_code,
        error_detail=None if error_code is None else "detail",
        now=NOW,
        stale_after_seconds=STALE_AFTER,
    )


def test_a_host_never_observed_is_pending_not_failing() -> None:
    """Registering a host and reaching one are different events."""

    value = _freshness(observed_ago=None, error_code=None)
    assert not value.observed
    assert not value.fresh
    assert value.status == "PENDING"
    assert not value.unreachable


def test_a_host_never_observed_that_reported_an_error_is_failing() -> None:
    value = _freshness(observed_ago=None, error_code="auth_failed")
    assert value.status == "ERROR"
    assert value.unreachable


def test_one_failed_probe_does_not_unseat_a_recent_success() -> None:
    """This is the bug the whole change exists to prevent.

    A failed attempt describes the attempt, not the host. A probe that timed out
    two seconds after a good observation says nothing about whether the machine
    is there, and reporting it as a connection failure is what made a working
    server show as unreachable during any brief network stall.
    """

    value = _freshness(observed_ago=6, error_code="command_timeout")
    assert value.fresh
    assert value.status == "ONLINE"
    assert not value.unreachable


def test_a_host_becomes_unreachable_only_once_its_last_success_is_stale() -> None:
    assert not _freshness(observed_ago=STALE_AFTER, error_code="command_timeout").unreachable
    assert _freshness(observed_ago=STALE_AFTER + 1, error_code="command_timeout").unreachable


def test_silence_without_an_error_is_stale_not_error() -> None:
    """Nothing has failed; we simply have not heard anything recent."""

    value = _freshness(observed_ago=STALE_AFTER + 10, error_code=None)
    assert value.status == "STALE"
    assert not value.unreachable


def test_freshness_carries_the_age_so_no_client_subtracts_two_clocks() -> None:
    value = _freshness(observed_ago=12, error_code=None)
    assert value.age_seconds == 12
    assert value.stale_after_seconds == STALE_AFTER
