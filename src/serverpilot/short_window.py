"""The last few readings of each card and host, held in memory for a short average.

The server table shows load as an average of the last half minute, because a
single collection can catch a job mid-step. Persisted history keeps one point a
minute, too coarse for a window that short, so the readings each collection
brings are kept here instead. Nothing decides from them: they are for a person
to read, and after a restart the first averages simply hold fewer readings.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Hashable
from datetime import datetime, timedelta
from typing import Any


class ShortWindow:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        # Collections write on the domain thread; snapshots read from the API's
        # worker threads.
        self._lock = threading.Lock()
        self._readings: dict[Hashable, deque[tuple[datetime, dict[str, float | None]]]] = {}

    def add(self, key: Hashable, observed_at: datetime, values: dict[str, float | None]) -> None:
        with self._lock:
            readings = self._readings.setdefault(key, deque())
            # A retried transaction or a stale observation brings nothing new.
            if readings and observed_at <= readings[-1][0]:
                return
            readings.append((observed_at, values))
            horizon = observed_at - timedelta(seconds=self.seconds)
            while readings and readings[0][0] <= horizon:
                readings.popleft()

    def average(
        self, key: Hashable, observed_at: datetime, current: dict[str, float | None]
    ) -> dict[str, Any]:
        """Average the readings in the window that ends at ``observed_at``.

        ``current`` is the reading the snapshot shows as now, so the average
        always includes it, even before this process has seen a collection.
        """

        horizon = observed_at - timedelta(seconds=self.seconds)
        with self._lock:
            window = [
                values for at, values in self._readings.get(key, ()) if horizon < at < observed_at
            ]
        window.append(current)

        def mean(name: str) -> float | None:
            present = [float(values[name]) for values in window if values.get(name) is not None]
            return round(sum(present) / len(present), 2) if present else None

        return {
            "window_seconds": self.seconds,
            "sample_count": len(window),
            **{name: mean(name) for name in current},
        }
