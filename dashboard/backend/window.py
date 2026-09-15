"""The D6 rolling window: the last HISTORY_SECONDS of telemetry, in memory.

Time-bounded rather than count-bounded. Deriving a sample count from whatever
the plant calls its sample period would hardcode one model's parameter name into
the backend; bounding by time makes a 100 Hz plant work with no code change and
keeps the window honest when the plant stalls.

Written by the QuixStreams consumer thread, read by the uvicorn loop, so every
public method takes the lock. The lock is held only for deque work - the
columnar projection of a snapshot is built inside it too, because it walks the
same deque.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

Row = dict[str, float]


class RollingWindow:
    def __init__(self, history_seconds: float, max_samples: int) -> None:
        self._history_ms = history_seconds * 1000.0
        self._max_samples = max_samples
        self._lock = threading.Lock()
        self._samples: deque[tuple[int, Row]] = deque()
        self._applied: dict[str, Any] | None = None
        self._applied_ts: int | None = None
        self._last_ts: int | None = None

    def append(self, ts_ms: int, row: Row) -> None:
        with self._lock:
            self._samples.append((ts_ms, row))
            self._last_ts = ts_ms
            while self._samples and ts_ms - self._samples[0][0] > self._history_ms:
                self._samples.popleft()
            while len(self._samples) > self._max_samples:
                self._samples.popleft()

    def set_applied(self, applied: dict[str, Any], ts_ms: int) -> None:
        with self._lock:
            self._applied = applied
            self._applied_ts = ts_ms

    def applied(self) -> tuple[dict[str, Any] | None, int | None]:
        with self._lock:
            return self._applied, self._applied_ts

    def last_ts(self) -> int | None:
        with self._lock:
            return self._last_ts

    def rows(self) -> int:
        with self._lock:
            return len(self._samples)

    def snapshot(
        self, names: list[str], seconds: float | None = None
    ) -> dict[str, Any]:
        """Columnar projection: uPlot consumes [xs, ys1, ys2, ...] directly, so the
        wire format is chosen to feed the chart library without a transform.

        A name missing from a row is emitted as null, which uPlot renders as a
        break rather than a straight line through the gap.
        """
        with self._lock:
            if not self._samples:
                return {"ts": [], "series": {name: [] for name in names}, "gaps": []}
            cutoff = -1.0
            if seconds is not None:
                cutoff = self._samples[-1][0] - seconds * 1000.0
            selected = [item for item in self._samples if item[0] >= cutoff]

        timestamps = [ts for ts, _ in selected]
        series: dict[str, list[float | None]] = {
            name: [row.get(name) for _, row in selected] for name in names
        }
        return {"ts": timestamps, "series": series, "gaps": []}
