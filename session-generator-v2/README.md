# Session Generator V2

A **Service** that produces one continuous multi-key run into `session-v2-in`, emits a
closer per active key, logs `RUN COMPLETE` and then idles. Phase 2 of the rig specified
in
[`../dev-planning/session-windows-live/spec-v2.md`](../dev-planning/session-windows-live/spec-v2.md);
phase 1's `session-generator/` is untouched and stays runnable.

## The schedule

Per key `i` the generator holds `next_event_ms`, `events_left_in_stream`, `streams_done`,
a monotonic `seq` and a private `random.Random(SEED + i)`. A min-heap of
`(next_event_ms, key_index)` is popped one event at a time, so the **scheduled** stream is
globally sorted by event time and ties break on ascending key index.

Inside a stream the next event is `EVENT_INTERVAL_MS` later. At the end of a stream the
key either retires (`STREAMS_PER_KEY`, or 1 for the last `IDLE_KEY_COUNT` keys) or waits
`GAP_FIXED_MS` / a draw from `[GAP_RANDOM_MIN_MS, GAP_RANDOM_MAX_MS]` and starts another.
A silence splits the key into two sessions exactly when it is `>= GAP_MS + 1`; every draw
is logged with its verdict and the run ends with `splits=` and `joins=`.

## Out-of-order and late injection

A perfectly ordered stream never makes an event late and never makes R1 merge, which
would leave both of those code paths untested at scale. So a share of the scheduled
events is **held back**: the event keeps its own timestamp and is produced later in the
stream, after events of other keys.

| | share | held back by | effect |
|---|---|---|---|
| out of order | `OUT_OF_ORDER_PCT` | `OUT_OF_ORDER_DELAY_MS` (`<= GAP_MS + GRACE_MS`) | arrives below the watermark but inside one gap, so it **joins** and can merge two sessions |
| late | `LATE_PCT` | `LATE_DELAY_MS` (`> GAP_MS + GRACE_MS`) | arrives below `watermark - gap - grace`, so it is **dropped** and fires `on_late` |

A held event is released immediately **before** the first scheduled event whose timestamp
reaches `event_ms + delay`. The watermark in force at that moment is therefore strictly
below `event_ms + delay`, which makes `OUT_OF_ORDER_DELAY_MS <= GAP_MS + GRACE_MS` an
admissibility guarantee rather than a hope. Anything still held when the schedule empties
is drained before the closers.

The selection rolls one `random.Random(SEED + 991)` draw per event, so the two classes are
mutually exclusive and turning injection off leaves every key's schedule byte-identical.
None of it needs an oracle change: the oracle reads what landed, in the order it landed.

## Why a Service that idles

`run()` must not return. A Service whose process exits is restarted by the platform and
replays the entire run, doubling every count. After the closers the source logs
`RUN COMPLETE` and sits in `while self.running: time.sleep(1)`; `Source.stop()` clears
`running` on SIGTERM and the Service shuts down cleanly. Stop it from the Portal once the
verdict has been read.

`instance_id` (a `uuid4` chosen once per process) is stamped on every payload, so a restart
that did happen is one `SELECT DISTINCT` away — the verdict Job fails the run on more than
one value.

## Variables

Described in `app.yaml`. `GAP_MS` and `GRACE_MS` must equal the three probes' and the
verdict Job's. `BASE_MS` stays blank on a Service.
