"""WebSocket hub: fan-out of telemetry, fan-in of control writes.

One socket per browser tab. The framing is transport-neutral on purpose - the
same message objects would ride an SSE stream plus POST /api/control if the
ingress ever refused an upgrade, so the fallback swaps this module and one
client module and touches nothing else.

Threading: `publish_*` is called from the QuixStreams consumer thread and only
appends to a list under a threading.Lock. Everything else runs inside the
uvicorn event loop. Nothing here blocks the consumer thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .lexicon import LexiconCache, LexiconError
from .settings import Settings
from .window import RollingWindow

logger = logging.getLogger(__name__)

# A plant that has published nothing for this long is reported stale. A stopped
# plant is a legitimate state, so this is a flag on /healthz and a badge in the
# UI - never a failed health check.
TELEMETRY_STALE_MS = 3000
PING_INTERVAL_S = 15.0
PONG_TIMEOUT_S = 30.0
STATUS_INTERVAL_S = 5.0

Sample = tuple[int, dict[str, float]]
PendingBatch = tuple[list[Sample], tuple[dict[str, Any], int] | None, int | None]


class Connection:
    """One tab. `out` is the bounded, droppable send queue."""

    def __init__(self, websocket: WebSocket, queue_max: int) -> None:
        self.ws = websocket
        self.out: deque[dict[str, Any]] = deque()
        self.wake = asyncio.Event()
        self.queue_max = queue_max
        self.signals: list[str] = []
        self.paused = False
        self.dropped = 0
        self.full_since: float | None = None
        self.last_pong = time.monotonic()
        self.open = True

    def enqueue(self, message: dict[str, Any], droppable: bool) -> None:
        """Bounded queue. Telemetry is droppable; control state never is.

        Losing an `applied` would leave a control permanently showing a value the
        plant does not hold. Losing a `frames` costs one flush period of chart.
        """
        if len(self.out) >= self.queue_max:
            if not self._discard_oldest_frames() and droppable:
                self.dropped += 1
                return
            if self.full_since is None:
                self.full_since = time.monotonic()
        else:
            self.full_since = None
        self.out.append(message)
        self.wake.set()

    def _discard_oldest_frames(self) -> bool:
        for index, item in enumerate(self.out):
            if item.get("t") == "frames":
                del self.out[index]
                self.dropped += 1
                return True
        return False

    def stalled_for(self) -> float:
        return 0.0 if self.full_since is None else time.monotonic() - self.full_since


class Hub:
    def __init__(
        self,
        settings: Settings,
        window: RollingWindow,
        lexicon: LexiconCache,
        submit_write: Callable[[dict[str, Any], dict[str, Any]], list[str]],
    ) -> None:
        self._settings = settings
        self._window = window
        self._lexicon = lexicon
        self._submit_write = submit_write
        self._connections: list[Connection] = []
        self._pending_lock = threading.Lock()
        self._pending: list[Sample] = []
        self._pending_applied: tuple[dict[str, Any], int] | None = None
        self._pending_lexicon: int | None = None
        self.total_dropped = 0

    # ---- producer side: called from other threads -------------------------

    def publish_sample(self, ts_ms: int, row: dict[str, float]) -> None:
        with self._pending_lock:
            self._pending.append((ts_ms, row))

    def publish_applied(self, applied: dict[str, Any], ts_ms: int) -> None:
        with self._pending_lock:
            self._pending_applied = (applied, ts_ms)

    def publish_lexicon(self, rev: int) -> None:
        with self._pending_lock:
            self._pending_lexicon = rev

    def _drain(self) -> PendingBatch:
        with self._pending_lock:
            samples, self._pending = self._pending, []
            applied, self._pending_applied = self._pending_applied, None
            rev, self._pending_lexicon = self._pending_lexicon, None
        return samples, applied, rev

    # ---- connection lifecycle --------------------------------------------

    async def serve(self, websocket: WebSocket) -> None:
        """Accept, pump, and clean up one connection."""
        await websocket.accept()
        conn = Connection(websocket, self._settings.ws_queue_max)
        self._connections.append(conn)
        sender = asyncio.create_task(self._sender(conn))
        try:
            while True:
                raw = await websocket.receive_text()
                await self._on_client_message(conn, raw)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            # Starlette raises this when the socket is already closed by the
            # stall watchdog below; it is the normal end of that path.
            pass
        finally:
            conn.open = False
            conn.wake.set()
            sender.cancel()
            # The send task may already have failed on a socket that went away;
            # awaiting it would re-raise that into the endpoint for no gain.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            if conn in self._connections:
                self._connections.remove(conn)
            self.total_dropped += conn.dropped

    async def _sender(self, conn: Connection) -> None:
        while conn.open:
            if not conn.out:
                conn.wake.clear()
                await conn.wake.wait()
                continue
            message = conn.out.popleft()
            await conn.ws.send_text(json.dumps(message))

    async def _on_client_message(self, conn: Connection, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            # Deliberate catch: a browser can send anything, and one malformed
            # frame must not take down a connection that is otherwise healthy.
            logger.warning("[WS] dropping unparseable frame")
            return
        if not isinstance(message, dict):
            return

        kind = message.get("t")
        if kind in ("hello", "sub"):
            conn.signals = _subscribed_names(message.get("signals"))
            conn.paused = False
            conn.enqueue(self._snapshot_message(conn), droppable=False)
        elif kind == "write":
            try:
                errors = self._submit_write(
                    _as_dict(message.get("signals")),
                    _as_dict(message.get("parameters")),
                )
            except LexiconError as exc:
                # Deliberate catch: without a lexicon there is nothing to
                # validate a write against, and an uncaught raise here would
                # kill an otherwise healthy socket instead of answering it.
                errors = [str(exc)]
            if errors:
                conn.enqueue(
                    {"t": "write_error", "seq": message.get("seq"), "errors": errors},
                    droppable=False,
                )
        elif kind == "pause":
            conn.paused = True
        elif kind == "resume":
            conn.paused = False
            conn.enqueue(self._snapshot_message(conn), droppable=False)
        elif kind == "pong":
            conn.last_pong = time.monotonic()

    def _snapshot_message(self, conn: Connection) -> dict[str, Any]:
        snapshot = self._window.snapshot(conn.signals, self._settings.history_seconds)
        applied, applied_ts = self._window.applied()
        lexicon = self._lexicon.snapshot()
        now_ms = int(time.time() * 1000)
        return {
            "t": "snapshot",
            "lexicon_rev": lexicon.rev if lexicon else 0,
            "ts": snapshot["ts"],
            "series": snapshot["series"],
            "applied": applied,
            "applied_age_ms": None if applied_ts is None else now_ms - applied_ts,
            "history_s": self._settings.history_seconds,
        }

    # ---- fan-out loops ----------------------------------------------------

    async def run(self) -> None:
        """Owns both periodic loops for the life of the process."""
        await asyncio.gather(self._flush_loop(), self._status_loop())

    async def _flush_loop(self) -> None:
        period = 1.0 / max(self._settings.ws_flush_hz, 0.1)
        while True:
            await asyncio.sleep(period)
            samples, applied, rev = self._drain()
            if applied is not None:
                payload = {"t": "applied", **applied[0], "ts": applied[1]}
                self._broadcast(payload, droppable=False)
            if rev is not None:
                self._broadcast({"t": "lexicon", "rev": rev}, droppable=False)
            if samples:
                self._broadcast_frames(samples)

    def _broadcast_frames(self, samples: list[Sample]) -> None:
        """One batch per flush tick, always an array - lowering WS_FLUSH_HZ under
        load therefore needs no client change.

        The projection is built once per distinct subscription set and shared by
        every connection that asked for that set; it is only ever read. The outer
        frame is per connection, because `dropped` is.
        """
        timestamps = [ts for ts, _ in samples]
        projections: dict[tuple[str, ...], dict[str, list[float | None]]] = {}
        for conn in self._connections:
            if conn.paused or not conn.signals:
                continue
            key = tuple(conn.signals)
            series = projections.get(key)
            if series is None:
                series = {
                    name: [row.get(name) for _, row in samples] for name in conn.signals
                }
                projections[key] = series
            conn.enqueue(
                {
                    "t": "frames",
                    "ts": timestamps,
                    "series": series,
                    "dropped": conn.dropped,
                },
                droppable=True,
            )

    def _broadcast(self, message: dict[str, Any], droppable: bool) -> None:
        for conn in self._connections:
            conn.enqueue(dict(message), droppable=droppable)

    async def _status_loop(self) -> None:
        last_ping = 0.0
        while True:
            await asyncio.sleep(STATUS_INTERVAL_S)
            now = time.monotonic()
            status = self.status()
            self._broadcast({"t": "status", **status}, droppable=False)
            if now - last_ping >= PING_INTERVAL_S:
                last_ping = now
                self._broadcast({"t": "ping"}, droppable=False)
            await self._reap(now)

    async def _reap(self, now: float) -> None:
        """Close connections that stalled or stopped answering pings.

        A reconnect costs one snapshot and is both cheaper and more correct than
        nursing a socket whose queue never drains.
        """
        for conn in list(self._connections):
            stalled = conn.stalled_for() > self._settings.ws_stall_timeout_s
            silent = now - conn.last_pong > PONG_TIMEOUT_S
            if not stalled and not silent:
                continue
            reason = "stalled queue" if stalled else "no pong"
            logger.warning("[WS] closing connection: %s", reason)
            conn.open = False
            conn.wake.set()
            with contextlib.suppress(Exception):
                await conn.ws.close(code=1013)

    def status(self) -> dict[str, Any]:
        last_ts = self._window.last_ts()
        age = None if last_ts is None else int(time.time() * 1000) - last_ts
        return {
            "telemetry_stale": age is None or age > TELEMETRY_STALE_MS,
            "last_sample_age_ms": age,
            "ws_clients": len(self._connections),
        }


def _subscribed_names(raw: Any) -> list[str]:
    """Only output signals live in the window; parameters arrive via `applied`."""
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and item.get("direction") == "output":
            if name not in names:
                names.append(name)
    return sorted(names)


def _as_dict(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}
