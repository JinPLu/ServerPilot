"""How old an observation is, and what that means for one endpoint.

One owner for a question that used to have four answers. `endpoint_reachability`
asked "was it ever observed", `_endpoint_is_unreachable` asked "is there an error
and is the last success stale", the snapshot's monitor ladder asked a third
version inline, and the collector's back-off predicate asked a fourth. They
agreed most of the time, which is worse than disagreeing loudly: the places they
differed were exactly the places a healthy host was reported as failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Freshness:
    """What the control plane currently knows about one endpoint."""

    observed_at: datetime | None
    attempted_at: datetime | None
    age_seconds: float | None
    stale_after_seconds: int
    error_code: str | None
    error_detail: str | None

    @property
    def observed(self) -> bool:
        """Whether this endpoint has ever answered."""

        return self.observed_at is not None

    @property
    def fresh(self) -> bool:
        """Whether what we know is current enough to act on."""

        return self.age_seconds is not None and self.age_seconds <= self.stale_after_seconds

    @property
    def unreachable(self) -> bool:
        """Whether ServerPilot can no longer learn anything about this host.

        A failed attempt on its own does not qualify. `last_error` describes one
        attempt, not the host; a probe that timed out two seconds after a good
        observation says nothing about whether the machine is there.
        """

        return self.error_code is not None and not self.fresh

    @property
    def status(self) -> str:
        """The public monitor value for the connectivity axis.

        Whether a human disabled or drained the endpoint is a different axis and
        is read from the endpoint's own columns, never inferred from here.
        """

        if not self.observed:
            return "PENDING" if self.error_code is None else "ERROR"
        if self.fresh:
            return "ONLINE"
        return "ERROR" if self.error_code is not None else "STALE"


def freshness_of(
    *,
    observed_at: datetime | None,
    attempted_at: datetime | None,
    error_code: str | None,
    error_detail: str | None,
    now: datetime,
    stale_after_seconds: int,
) -> Freshness:
    """Build the one Freshness value for an endpoint's provider state."""

    age = None if observed_at is None else (now - observed_at).total_seconds()
    return Freshness(
        observed_at=observed_at,
        attempted_at=attempted_at,
        age_seconds=age,
        stale_after_seconds=stale_after_seconds,
        error_code=error_code,
        error_detail=error_detail,
    )
