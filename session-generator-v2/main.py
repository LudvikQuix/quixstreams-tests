"""Continuous multi-key generator for phase 2 of the live session-window rig.

A min-heap of `(next_event_ms, key_index)` interleaves every key onto one globally
ascending event-time stream (spec-v2 section 5.2). On top of that schedule a share
of the events is held back and produced later than its own event time: an
out-of-order event lands within one inactivity gap of the watermark and must join a
stored session, a late event lands far below it and must be dropped. Both selections
come from `SEED`, so a run is reproducible.

A held-back event is released immediately **before** the first scheduled event whose
timestamp reaches `event_ms + delay`, so the watermark in force when it is produced
is strictly below `event_ms + delay`. That is what makes
`OUT_OF_ORDER_DELAY_MS <= GAP_MS + GRACE_MS` an admissibility guarantee.

Nothing here is load-bearing for the verdict: the oracle in `session-verdict` reads
what landed, in the order it landed. The parameters decide what the run covers.
"""

import heapq
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from importlib.metadata import version

from quixstreams import Application
from quixstreams.sources import Source

logger = logging.getLogger(__name__)

# Injection draws come from their own RNG, so turning injection on or off leaves
# every key's schedule byte-identical for the same SEED.
INJECTION_SEED_OFFSET = 991


@dataclass(frozen=True)
class Params:
    """Every generator parameter, resolved once in `main()`."""

    run_id: str
    key_count: int
    idle_key_count: int
    events_per_stream_min: int
    events_per_stream_max: int
    event_interval_ms: int
    gap_mode: str
    gap_fixed_ms: int
    gap_random_min_ms: int
    gap_random_max_ms: int
    streams_per_key: int
    run_forever: bool
    key_stagger_ms: int
    seed: int
    speed: str
    base_ms: int
    produce_rate_ms: int
    gap_ms: int
    grace_ms: int
    partition_count: int
    out_of_order_pct: int
    out_of_order_delay_ms: int
    late_pct: int
    late_delay_ms: int


@dataclass(frozen=True)
class Event:
    """One scheduled message. `seq` is -1 for a closer."""

    key_index: int
    seq: int
    event_ms: int
    stream_index: int
    gap_used_ms: int


def params_from_env() -> Params:
    """Read every parameter from the environment. Blank `BASE_MS` means "now"."""
    base_ms_raw = os.environ.get("BASE_MS", "").strip()
    return Params(
        run_id=os.environ["RUN_ID"],
        key_count=int(os.environ["KEY_COUNT"]),
        idle_key_count=int(os.environ["IDLE_KEY_COUNT"]),
        events_per_stream_min=int(os.environ["EVENTS_PER_STREAM_MIN"]),
        events_per_stream_max=int(os.environ["EVENTS_PER_STREAM_MAX"]),
        event_interval_ms=int(os.environ["EVENT_INTERVAL_MS"]),
        gap_mode=os.environ["GAP_MODE"],
        gap_fixed_ms=int(os.environ["GAP_FIXED_MS"]),
        gap_random_min_ms=int(os.environ["GAP_RANDOM_MIN_MS"]),
        gap_random_max_ms=int(os.environ["GAP_RANDOM_MAX_MS"]),
        streams_per_key=int(os.environ["STREAMS_PER_KEY"]),
        run_forever=os.environ["RUN_FOREVER"].strip().lower() == "true",
        key_stagger_ms=int(os.environ["KEY_STAGGER_MS"]),
        seed=int(os.environ["SEED"]),
        speed=os.environ["SPEED"],
        base_ms=int(base_ms_raw) if base_ms_raw else int(time.time() * 1000),
        produce_rate_ms=int(os.environ["PRODUCE_RATE_MS"]),
        gap_ms=int(os.environ["GAP_MS"]),
        grace_ms=int(os.environ["GRACE_MS"]),
        partition_count=int(os.environ["PARTITION_COUNT"]),
        out_of_order_pct=int(os.environ["OUT_OF_ORDER_PCT"]),
        out_of_order_delay_ms=int(os.environ["OUT_OF_ORDER_DELAY_MS"]),
        late_pct=int(os.environ["LATE_PCT"]),
        late_delay_ms=int(os.environ["LATE_DELAY_MS"]),
    )


class SessionGeneratorV2(Source):
    """Drive the min-heap schedule, inject hold-backs, close the run and idle."""

    def __init__(self, name: str, params: Params, instance_id: str) -> None:
        super().__init__(name=name)
        self._p = params
        self._instance_id = instance_id
        self._injection_rng = random.Random(params.seed + INJECTION_SEED_OFFSET)
        # (release_at, hold_order, kind, event); hold_order keeps the heap total.
        self._held: list[tuple[int, int, str, Event]] = []
        self._hold_order = 0
        self._splits = 0
        self._joins = 0
        self._held_out_of_order = 0
        self._held_late = 0
        self._produced = 0
        self._max_event_ms = 0

    def run(self) -> None:
        p = self._p
        rngs = [random.Random(p.seed + i) for i in range(p.key_count)]
        events_left = [
            rng.randint(p.events_per_stream_min, p.events_per_stream_max)
            for rng in rngs
        ]
        streams_done = [0] * p.key_count
        stream_index = [0] * p.key_count
        gap_used_ms = [0] * p.key_count
        seq = [0] * p.key_count

        heap = [(p.base_ms + i * p.key_stagger_ms, i) for i in range(p.key_count)]
        heapq.heapify(heap)
        logger.info(
            "RUN START instance_id=%s base_ms=%d keys=%d topic=%s",
            self._instance_id,
            p.base_ms,
            p.key_count,
            self.producer_topic.name,
        )

        while heap and self.running:
            event_ms, i = heapq.heappop(heap)
            self._wait_for(event_ms)
            self._release_due(event_ms)
            self._offer(
                Event(
                    key_index=i,
                    seq=seq[i],
                    event_ms=event_ms,
                    stream_index=stream_index[i],
                    gap_used_ms=gap_used_ms[i],
                )
            )
            seq[i] += 1
            events_left[i] -= 1
            if events_left[i] > 0:
                heapq.heappush(heap, (event_ms + p.event_interval_ms, i))
                continue

            streams_done[i] += 1
            limit = 1 if i >= p.key_count - p.idle_key_count else p.streams_per_key
            if not p.run_forever and streams_done[i] >= limit:
                continue
            gap = self._draw_gap(rngs[i], i)
            gap_used_ms[i] = gap
            stream_index[i] += 1
            events_left[i] = rngs[i].randint(
                p.events_per_stream_min, p.events_per_stream_max
            )
            heapq.heappush(heap, (event_ms + gap, i))

        self._release_all()
        t_close = self._close_run(stream_index)
        logger.info(
            "RUN COMPLETE instance_id=%s produced=%d max_event_ms=%d t_close=%d "
            "splits=%d joins=%d held_out_of_order=%d held_late=%d",
            self._instance_id,
            self._produced,
            self._max_event_ms,
            t_close,
            self._splits,
            self._joins,
            self._held_out_of_order,
            self._held_late,
        )
        # A Service whose process exits is restarted by the platform and would
        # replay the whole run into a second event-time band (spec-v2 risk R5).
        while self.running:
            time.sleep(1)

    def _close_run(self, stream_index: list[int]) -> int:
        """Emit one closer per non-idle key at `max_event + 3G + g`."""
        p = self._p
        if p.run_forever:
            return 0
        t_close = self._max_event_ms + 3 * p.gap_ms + p.grace_ms
        for i in range(p.key_count - p.idle_key_count):
            self._emit(
                Event(
                    key_index=i,
                    seq=-1,
                    event_ms=t_close,
                    stream_index=stream_index[i],
                    gap_used_ms=0,
                ),
                held="no",
            )
        return t_close

    def _draw_gap(self, rng: random.Random, key_index: int) -> int:
        """Draw the silence before a key's next stream; record whether it splits."""
        p = self._p
        if p.gap_mode == "fixed":
            gap = p.gap_fixed_ms
        else:
            gap = rng.randint(p.gap_random_min_ms, p.gap_random_max_ms)
        splits = gap >= p.gap_ms + 1
        if splits:
            self._splits += 1
        else:
            self._joins += 1
        logger.info("GAP key=%s gap_ms=%d splits=%s", self._key(key_index), gap, splits)
        return gap

    def _offer(self, event: Event) -> None:
        """Produce the event now, or hold it back for a later point in the stream."""
        p = self._p
        roll = self._injection_rng.random() * 100.0
        if roll < p.late_pct:
            kind, delay = "late", p.late_delay_ms
            self._held_late += 1
        elif roll < p.late_pct + p.out_of_order_pct:
            kind, delay = "ooo", p.out_of_order_delay_ms
            self._held_out_of_order += 1
        else:
            self._emit(event, held="no")
            return
        heapq.heappush(
            self._held, (event.event_ms + delay, self._hold_order, kind, event)
        )
        self._hold_order += 1
        logger.info(
            "HOLD kind=%s key=%s seq=%d event_ms=%d release_at=%d",
            kind,
            self._key(event.key_index),
            event.seq,
            event.event_ms,
            event.event_ms + delay,
        )

    def _release_due(self, event_ms: int) -> None:
        """Produce every held event whose release point the schedule has reached."""
        while self._held and self._held[0][0] <= event_ms:
            _, _, kind, event = heapq.heappop(self._held)
            self._emit(event, held=kind)

    def _release_all(self) -> None:
        """Drain the hold-back heap before the closer phase."""
        while self._held:
            _, _, kind, event = heapq.heappop(self._held)
            self._emit(event, held=kind)

    def _emit(self, event: Event, held: str) -> None:
        p = self._p
        key = self._key(event.key_index)
        payload = {
            "run_id": p.run_id,
            "instance_id": self._instance_id,
            "key_index": event.key_index,
            "stream_index": event.stream_index,
            "seq": event.seq,
            "event_ms": event.event_ms,
            "base_ms": p.base_ms,
            "gap_used_ms": event.gap_used_ms,
        }
        message = self.serialize(key=key, value=payload, timestamp_ms=event.event_ms)
        self.produce(
            key=message.key,
            value=message.value,
            headers=message.headers,
            timestamp=message.timestamp,
        )
        self.flush()
        self._produced += 1
        self._max_event_ms = max(self._max_event_ms, event.event_ms)
        logger.info(
            "key=%s seq=%d event_ms=%d stream_index=%d held=%s",
            key,
            event.seq,
            event.event_ms,
            event.stream_index,
            held,
        )

    def _key(self, key_index: int) -> str:
        return f"{self._p.run_id}-k{key_index:03d}"

    def _wait_for(self, event_ms: int) -> None:
        p = self._p
        if p.speed == "realtime":
            delay_ms = event_ms - int(time.time() * 1000)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)
        elif p.produce_rate_ms:
            time.sleep(p.produce_rate_ms / 1000)


def log_startup(
    p: Params, topic_name: str, num_partitions: int, instance_id: str
) -> None:
    """The startup block: resolved topic, every parameter, the derived counts."""
    active_keys = p.key_count - p.idle_key_count
    streams = active_keys * p.streams_per_key + p.idle_key_count
    logger.info("Starting Session Generator V2")
    logger.info("  quixstreams:            %s", version("quixstreams"))
    logger.info("  output topic:           %s", topic_name)
    logger.info("  topic num_partitions:   %d", num_partitions)
    logger.info("  PARTITION_COUNT:        %d", p.partition_count)
    logger.info("  base_ms:                %d", p.base_ms)
    logger.info("  instance_id:            %s", instance_id)
    logger.info("  RUN_ID:                 %s", p.run_id)
    logger.info("  KEY_COUNT:              %d", p.key_count)
    logger.info("  IDLE_KEY_COUNT:         %d", p.idle_key_count)
    logger.info("  EVENTS_PER_STREAM_MIN:  %d", p.events_per_stream_min)
    logger.info("  EVENTS_PER_STREAM_MAX:  %d", p.events_per_stream_max)
    logger.info("  EVENT_INTERVAL_MS:      %d", p.event_interval_ms)
    logger.info("  GAP_MODE:               %s", p.gap_mode)
    logger.info("  GAP_FIXED_MS:           %d", p.gap_fixed_ms)
    logger.info("  GAP_RANDOM_MIN_MS:      %d", p.gap_random_min_ms)
    logger.info("  GAP_RANDOM_MAX_MS:      %d", p.gap_random_max_ms)
    logger.info("  STREAMS_PER_KEY:        %d", p.streams_per_key)
    logger.info("  RUN_FOREVER:            %s", p.run_forever)
    logger.info("  KEY_STAGGER_MS:         %d", p.key_stagger_ms)
    logger.info("  SEED:                   %d", p.seed)
    logger.info("  SPEED:                  %s", p.speed)
    logger.info("  PRODUCE_RATE_MS:        %d", p.produce_rate_ms)
    logger.info("  GAP_MS:                 %d", p.gap_ms)
    logger.info("  GRACE_MS:               %d", p.grace_ms)
    logger.info("  OUT_OF_ORDER_PCT:       %d", p.out_of_order_pct)
    logger.info("  OUT_OF_ORDER_DELAY_MS:  %d", p.out_of_order_delay_ms)
    logger.info("  LATE_PCT:               %d", p.late_pct)
    logger.info("  LATE_DELAY_MS:          %d", p.late_delay_ms)
    logger.info("  active keys:            %d", active_keys)
    logger.info("  streams:                %d", streams)
    logger.info(
        "  events:                 %d..%d plus %d closers",
        streams * p.events_per_stream_min,
        streams * p.events_per_stream_max,
        active_keys,
    )


def main() -> None:
    loglevel = os.environ["LOGLEVEL"]
    logging.basicConfig(
        level=loglevel,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    params = params_from_env()
    instance_id = uuid.uuid4().hex

    app = Application(loglevel=loglevel)
    output_topic = app.topic(
        name=os.environ["output"],
        value_serializer="json",
        key_serializer="str",
    )
    # broker_config is populated by TopicManager.topic() (manager.py:179-181) in
    # this process, before the source subprocess is spawned.
    log_startup(
        params,
        output_topic.name,
        output_topic.broker_config.num_partitions,
        instance_id,
    )
    app.add_source(
        SessionGeneratorV2(
            name="session-generator-v2",
            params=params,
            instance_id=instance_id,
        ),
        topic=output_topic,
    )
    app.run()


if __name__ == "__main__":
    main()
