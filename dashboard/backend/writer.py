"""Control write path: validate -> coalesce -> one keyed message on dashboard-in.

Only this module's thread ever calls produce(), which makes producer
thread-safety a non-question and gives the coalescer one obvious home.

Keying is not optional. Phase 1 makes partial update last-write-wins, and
last-write-wins needs a total order; unkeyed messages round-robin across
partitions, so a `parameters` write can overtake a `signals` write. Every
message goes out with PLANT_KEY.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from .lexicon import LexiconCache, LexiconSnapshot
from .settings import Settings

logger = logging.getLogger(__name__)

Envelope = dict[str, dict[str, Any]]


def coerce(descriptor: dict[str, Any], raw: Any) -> tuple[bool, Any]:
    """The Phase 1 coercion rules, re-run on the way out.

    bool is an int subclass in Python, so it is rejected explicitly before any
    numeric check - otherwise True would sail through and land as 1 in a float
    parameter. Strings are never coerced: the sim dropped string coercion
    deliberately, so sending "-8000" is a silent no-op on the plant.
    """
    datatype = descriptor["datatype"]
    if datatype == "bool":
        return isinstance(raw, bool), raw
    if isinstance(raw, bool):
        return False, None
    if datatype == "enum":
        for member in descriptor["enum"]:
            if raw == member["value"]:
                return True, member["value"]
        return False, None
    if not isinstance(raw, (int, float)):
        return False, None
    if datatype in ("int", "uint") and not isinstance(raw, int):
        return False, None
    value = float(raw) if datatype == "float" else int(raw)
    return descriptor["min"] <= value <= descriptor["max"], value


def validate(
    lexicon: LexiconSnapshot, signals: dict[str, Any], parameters: dict[str, Any]
) -> tuple[Envelope, list[str]]:
    """Re-run the client-side rules server-side. POST /api/control is a public
    route, so the browser is not the only possible caller.

    Out of range is blocked, never clamped: a clamped write looks accepted and
    leaves the control and the plant permanently disagreeing (D1).
    """
    accepted: Envelope = {"signals": {}, "parameters": {}}
    errors: list[str] = []

    for name, raw in signals.items():
        descriptor = lexicon.descriptor("signals", name, "input")
        if descriptor is None:
            errors.append(f"signals.{name}: not an input signal in this lexicon")
            continue
        ok, value = coerce(descriptor, raw)
        if not ok:
            errors.append(f"signals.{name}: {_reason(descriptor, raw)}")
            continue
        accepted["signals"][name] = value

    for name, raw in parameters.items():
        descriptor = lexicon.descriptor("parameters", name, None)
        if descriptor is None:
            errors.append(f"parameters.{name}: not a parameter in this lexicon")
            continue
        if descriptor["tunable"] is not True:
            # D1: a fixed parameter is never writable, at any entry point.
            errors.append(f"parameters.{name}: fixed at deploy time, not tunable")
            continue
        ok, value = coerce(descriptor, raw)
        if not ok:
            errors.append(f"parameters.{name}: {_reason(descriptor, raw)}")
            continue
        accepted["parameters"][name] = value

    return accepted, errors


def _reason(descriptor: dict[str, Any], raw: Any) -> str:
    if descriptor["datatype"] == "enum":
        members = [member["value"] for member in descriptor["enum"]]
        return f"{raw!r} is not one of {members}"
    if descriptor["datatype"] in ("uint", "int", "float") and isinstance(
        raw, (int, float)
    ):
        return (
            f"{raw!r} outside [{descriptor['min']}, {descriptor['max']}] "
            f"or not {descriptor['datatype']}"
        )
    return f"{raw!r} is not a valid {descriptor['datatype']}"


class ControlWriter:
    """Validated writes in, one coalesced envelope per WRITE_COALESCE_MS out."""

    def __init__(self, settings: Settings, lexicon: LexiconCache) -> None:
        self._settings = settings
        self._lexicon = lexicon
        self._queue: queue.Queue[Envelope] = queue.Queue()
        self.produced = 0

    def submit(self, signals: dict[str, Any], parameters: dict[str, Any]) -> list[str]:
        """Validate and enqueue. Returns the errors; on any error nothing is sent.

        Rejection is per request rather than per field: the dashboard is the
        sender, so an all-or-nothing boundary is the one place the user can still
        be told which field was refused. The plant stays field-granular.
        """
        accepted, errors = validate(self._lexicon.require(), signals, parameters)
        if errors:
            logger.warning("[WRITE] rejected: %s", "; ".join(errors))
            return errors
        if accepted["signals"] or accepted["parameters"]:
            self._queue.put(accepted)
        return []

    def run(self, producer: Any, topic: Any, stop: threading.Event) -> None:
        """Drain, merge last-write-wins per field, produce one keyed message."""
        period = self._settings.write_coalesce_ms / 1000.0
        while not stop.is_set():
            first = self._drain_blocking(period)
            if first is None:
                continue
            # Hold the window open once work exists, so a drag burst from one or
            # more tabs collapses into a single message instead of one each.
            stop.wait(period)
            merged = first
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                merged = _merge(merged, nxt)

            envelope = {k: v for k, v in merged.items() if v}
            message = topic.serialize(key=self._settings.plant_key, value=envelope)
            producer.produce(topic=topic.name, key=message.key, value=message.value)
            self.produced += 1
            logger.info("[WRITE] produced %s", envelope)

    def _drain_blocking(self, period: float) -> Envelope | None:
        try:
            return self._queue.get(timeout=period)
        except queue.Empty:
            return None


def _merge(base: Envelope, extra: Envelope) -> Envelope:
    """Preserves Phase 1 partial-update semantics exactly: an absent key means
    untouched, at both levels."""
    return {
        "signals": {**base["signals"], **extra["signals"]},
        "parameters": {**base["parameters"], **extra["parameters"]},
    }
