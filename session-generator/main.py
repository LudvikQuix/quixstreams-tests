"""Scripted event table for the live session-window rig.

EVENTS is emitted in list order and that order is the experiment's independent
variable: on the partition probe a single watermark governs every key, so a row
moved earlier is dropped as late and a row moved later closes another scenario's
session a step too soon. The proof of the order is section 7 of
dev-planning/session-windows-live/spec.md.

Every row carries its final `offset_ms`, band included, so the emitted Kafka
timestamp is `base_ms + offset_ms` with no arithmetic at run time.
"""

import logging
import os
import time

from quixstreams import Application
from quixstreams.sources import Source

logger = logging.getLogger(__name__)

# Must equal every probe's GAP_MS.
GAP_MS = 120000
# Phase `main` lives 20 minutes of event time above phase s7, which keeps its
# first event clear of the partition watermark left behind by the s7 closer.
MAIN_BAND_MS = 1200000

# (pos, phase, scenario, offset_ms, seq). A `seq` of -1 is a closer: an event
# 3 gaps past its scenario's last real event, far enough to open a session of its
# own (R1) and to close the real one (R3).
EVENTS: list[tuple[int, str, str, int, int]] = [
    (1, "s7a", "s7", 0, 0),
    (2, "s7a", "s7", 60000, 1),
    (3, "s7a", "s7", 120000, 2),
    (4, "s7a", "s7", 180000, 3),
    (5, "s7a", "s7", 240000, 4),
    # >>> the probes are stopped and restarted between pos 5 and pos 6 <<<
    (6, "s7b", "s7", 300000, 5),
    (7, "s7b", "s7", 360000, 6),
    (8, "s7b", "s7", 420000, 7),
    (9, "s7b", "s7", 480000, 8),
    (10, "s7b", "s7", 540000, 9),
    (11, "s7b", "s7", 900000, -1),
    (12, "main", "s1", MAIN_BAND_MS, 0),
    (13, "main", "s2", MAIN_BAND_MS, 0),
    (14, "main", "s3", MAIN_BAND_MS, 0),
    (15, "main", "s4", MAIN_BAND_MS, 0),
    (16, "main", "s5a", MAIN_BAND_MS, 0),
    (17, "main", "s5b", MAIN_BAND_MS, 0),
    (18, "main", "s1", MAIN_BAND_MS + 60000, 1),
    (19, "main", "s2", MAIN_BAND_MS + 60000, 1),
    (20, "main", "s3", MAIN_BAND_MS + 60000, 1),
    (21, "main", "s4", MAIN_BAND_MS + 60000, 1),
    (22, "main", "s5a", MAIN_BAND_MS + 60000, 1),
    (23, "main", "s5b", MAIN_BAND_MS + 60000, 1),
    (24, "main", "s1", MAIN_BAND_MS + 120000, 2),
    (25, "main", "s2", MAIN_BAND_MS + 120000, 2),
    (26, "main", "s5b", MAIN_BAND_MS + 120000, 2),
    # Out of order, in-gap join. Must follow pos 25, which lifts the partition
    # watermark to 1320000 and late_before to 1200000.
    (27, "main", "s2", MAIN_BAND_MS + 90000, 3),
    (28, "main", "s1", MAIN_BAND_MS + 180000, 3),
    (29, "main", "s5b", MAIN_BAND_MS + 180000, 3),
    (30, "main", "s3", MAIN_BAND_MS + 200000, 2),
    # Out of order, bridging merge, 20 s of margin: after pos 30, which opens the
    # session it bridges to, and before pos 32, which would make it late.
    (31, "main", "s3", MAIN_BAND_MS + 100000, 3),
    (32, "main", "s1", MAIN_BAND_MS + 240000, 4),
    (33, "main", "s5b", MAIN_BAND_MS + 240000, 4),
    (34, "main", "s1", MAIN_BAND_MS + 300000, 5),
    (35, "main", "s5b", MAIN_BAND_MS + 300000, 5),
    (36, "main", "s1", MAIN_BAND_MS + 360000, 6),
    (37, "main", "s5b", MAIN_BAND_MS + 360000, 6),
    (38, "main", "s1", MAIN_BAND_MS + 420000, 7),
    (39, "main", "s5b", MAIN_BAND_MS + 420000, 7),
    (40, "main", "s4", MAIN_BAND_MS + 60000 + 3 * GAP_MS, -1),
    (41, "main", "s1", MAIN_BAND_MS + 480000, 8),
    (42, "main", "s5b", MAIN_BAND_MS + 480000, 8),
    (43, "main", "s2", MAIN_BAND_MS + 120000 + 3 * GAP_MS, -1),
    (44, "main", "s1", MAIN_BAND_MS + 540000, 9),
    (45, "main", "s5b", MAIN_BAND_MS + 540000, 9),
    (46, "main", "s3", MAIN_BAND_MS + 200000 + 3 * GAP_MS, -1),
    (47, "main", "s1", MAIN_BAND_MS + 540000 + 3 * GAP_MS, -1),
    (48, "main", "s5b", MAIN_BAND_MS + 540000 + 3 * GAP_MS, -1),
    # Deliberately late, last of all: 750000 ms below late_before.
    (49, "main", "s4", MAIN_BAND_MS + 30000, 2),
]


def rows_for_phase(phase: str) -> list[tuple[int, str, str, int, int]]:
    """Return the rows of EVENTS belonging to `phase`, in emit order."""
    return [row for row in EVENTS if row[1] == phase]


class ScriptedSource(Source):
    """Produce one phase of EVENTS and return, so the Job exits 0."""

    def __init__(self, name: str, phase: str, base_ms: int, run_id: str) -> None:
        super().__init__(name=name)
        self._phase = phase
        self._base_ms = base_ms
        self._run_id = run_id

    def run(self) -> None:
        rows = rows_for_phase(self._phase)
        logger.info(
            "start phase=%s rows=%d base_ms=%d run_id=%s topic=%s",
            self._phase,
            len(rows),
            self._base_ms,
            self._run_id,
            self.producer_topic.name,
        )
        for pos, _phase, scenario, offset_ms, seq in rows:
            key = f"{self._run_id}-{scenario}"
            event_ms = self._base_ms + offset_ms
            payload = {
                "run_id": self._run_id,
                "scenario": scenario,
                "seq": seq,
                "offset_ms": offset_ms,
                "event_ms": event_ms,
                "base_ms": self._base_ms,
            }
            message = self.serialize(key=key, value=payload, timestamp_ms=event_ms)
            self.produce(
                key=message.key,
                value=message.value,
                headers=message.headers,
                timestamp=message.timestamp,
            )
            # One flush per row, ~49 round trips: a retried batch must never
            # reorder the stream.
            self.flush()
            logger.info(
                "pos=%d key=%s seq=%d offset_ms=%d event_ms=%d",
                pos,
                key,
                seq,
                offset_ms,
                event_ms,
            )
        logger.info(
            "done phase=%s rows=%d base_ms=%d topic=%s",
            self._phase,
            len(rows),
            self._base_ms,
            self.producer_topic.name,
        )


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    phase = os.environ["PHASE"]
    run_id = os.environ["RUN_ID"]
    base_ms_raw = os.environ["BASE_MS"].strip()
    if base_ms_raw:
        base_ms = int(base_ms_raw)
    else:
        base_ms = int(time.time() * 1000)
        logger.warning(
            "BASE_MS is blank, using now=%d - set BASE_MS to this value on the "
            "remaining phases, or seq 4 and seq 5 land more than a gap apart and "
            "the s7 session splits in two",
            base_ms,
        )

    app = Application()
    output_topic = app.topic(
        name=os.environ["output"],
        value_serializer="json",
        key_serializer="str",
    )
    app.add_source(
        ScriptedSource(
            name="session-generator",
            phase=phase,
            base_ms=base_ms,
            run_id=run_id,
        ),
        topic=output_topic,
    )
    app.run()


if __name__ == "__main__":
    main()
