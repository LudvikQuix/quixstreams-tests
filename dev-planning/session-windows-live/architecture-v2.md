# Architecture notes — session-windows-live phase 2

Implementation notes for the rig specified in `spec-v2.md`. Same pin as phase 1:
`quixstreams @ git+https://github.com/quixio/quix-streams.git@74275f72ddd54ea6698c7acb8bff1e5663ec2c75`.
Phase 1 (`session-generator/`, `session-probe/`, `tools/`, the four phase-1 topics and
deployment blocks) is untouched; the three V2 probes are three more deployments of the
unmodified `session-probe`.

## Generator — the heap, and what is held back

`session-generator-v2/main.py` resolves every variable in `main()` into a frozen `Params`
and hands it to a `Source` subclass. Nothing is read at import time, so both new modules
import without an environment — which the `--selftest` entry point and the import smoke
check need.

`run()` holds a min-heap of `(next_event_ms, key_index)`. Each iteration pops the minimum,
sleeps (`PRODUCE_RATE_MS` in `fast`, until the wall clock in `realtime`), releases any
held-back events the schedule has reached, offers the popped event, then pushes the key
back: `+ EVENT_INTERVAL_MS` inside a stream, `+ GAP_FIXED_MS` or a draw from
`[GAP_RANDOM_MIN_MS, GAP_RANDOM_MAX_MS]` between streams, or not at all once
`streams_done` reaches its limit (`1` for the last `IDLE_KEY_COUNT` keys, else
`STREAMS_PER_KEY`). Each key has its own `random.Random(SEED + i)`, so its stream lengths
and gaps are independent of every other key's and of the injection draws.

When the heap empties the hold-back heap is drained, one closer per non-idle key is
produced at `max_event_ms + 3 * GAP_MS + GRACE_MS`, `RUN COMPLETE` is logged and the
source enters `while self.running: time.sleep(1)`. `run()` must not return: a Service whose
process exits is restarted by the platform and replays the whole run (risk R5). `uuid4` per
process is stamped on every payload as `instance_id`, so a restart that did happen is
visible in the verdict header.

### Out-of-order and late injection

One `random.Random(SEED + 991)` draw per scheduled event decides: `< LATE_PCT` → held by
`LATE_DELAY_MS`, `< LATE_PCT + OUT_OF_ORDER_PCT` → held by `OUT_OF_ORDER_DELAY_MS`,
otherwise produced immediately. A held event keeps its own event timestamp; it is pushed
onto a second heap keyed by `event_ms + delay` and produced **immediately before the first
scheduled event whose timestamp reaches that release point**.

That "before, not after" is the load-bearing detail. Every event produced so far is then
strictly below `event_ms + delay`, so the watermark `W` in force when the held event lands
satisfies `W < event_ms + delay`, hence
`late_before = W - G - g < event_ms + delay - G - g <= event_ms` whenever
`delay <= G + g`. Out-of-order admissibility is therefore a guarantee, not a hope — the
event arrives below the watermark, joins a stored session and can merge two of them.
Lateness is the same mechanism with `delay > G + g`; it is near-certain rather than
guaranteed, because a single schedule step larger than `LATE_DELAY_MS - G - g` (280 000 ms
at the defaults) would leave the watermark too low. That costs nothing: the oracle reads
what landed, so a hold-back that joins instead of dropping is predicted correctly and the
verdict is still exact — only the coverage claim weakens, and the `held_late` counter and
the probes' `on_late` lines say so.

Injection uses its own RNG so that turning it on or off leaves every key's schedule
byte-identical for the same `SEED`.

## Verdict Job and the oracle

`session-verdict/main.py` registers one no-op dataframe per input/output topic and calls
`app.run(timeout=IDLE_TIMEOUT_S, count=MAX_RECORDS, metadata=True)`. The records come back
flat, with `_key`, `_topic`, `_partition`, `_offset`, `_timestamp` merged alongside the
deserialized value (`runtracker.py:24-47`), so the oracle gets the input partition with no
`message_context()` call and no hand-rolled consumer. Records whose `run_id` differs are
counted in the per-topic read counts and dropped.

`expected_sessions(events, gap_ms, grace_ms, scope)` returns an `OracleResult` with
`finals` — a `Counter` over `(key, start, end, count, seqs)` — `updates`, an ordered list
of `(start, end, count, first_seq, last_seq)` per key, and `late_drops`. It is run twice:
`scope="key"` feeds the key probe and (through `updates`) the current probe,
`scope="partition"` feeds the partition probe.

It replays the records sorted by `(_partition, _offset)`. That single ordering is
sufficient for both strategies: in key mode the outcome depends only on the per-key order,
in partition mode only on the per-partition order, and both are subsequences of it. Per
record:

1. `W` = `max(ts, key_wm[p,k])`, and in partition scope also `max(part_wm[p], …)` folded
   back into `part_wm[p]`. `late_before = W - G - g`, `close_before = late_before - G`.
2. R2: `ts < late_before` → recorded as a late drop, no state change whatsoever.
3. R1: probe the two immediate neighbours in start order, match on
   `start - G <= ts < end + G`; two matches merge, one extends, none opens a new session.
4. `key_wm[p,k]` is raised, and the current-mode update for the session the event landed in
   is appended.
5. R3: close every session of the scope — `[k]`, or every key of partition `p` — whose
   `end <= close_before`.

Update-then-expire is the SDK's order and is the oracle's. `seqs` is the session's events
sorted by `(ts, arrival_index)`, which is what the store's `(timestamp_ms, counter)`
collection key produces; `first_seq` and `last_seq` are that list's ends, which is exactly
`Earliest`'s keep-incumbent-on-a-tie and `Latest`'s replace-on-a-tie.

**Why it is independent of `session.py`.** It is written from `spec-v2.md` section 3, and
in particular it does **not** model the two expiry cursors — `expire_by_key`'s per-key
cursor and `expire_by_partition`'s partition checkpoint. Those are optimisations that must
be invisible from the outside, so the oracle sweeps unconditionally and closes every due
session every time. A disagreement that traces back to a cursor is therefore a finding, by
design. The flip side is stated in the module docstring and the README: a bug in the oracle
is exactly as likely as a bug in the SDK until someone hand-traces the disagreeing key.

**The per-partition watermark.** The state store is per `(topic, partition)`, the
`WindowedPartitionTransaction` is per partition, and `advance_partition_timestamp` writes
under the empty prefix of that partition's store. So `part_wm` is a dict keyed by
`_partition`, never a single global number, and the R3 sweep scope in partition mode is
"every key of partition `p`". A globally-keyed watermark gets section 7.3 wrong: it would
close `r3-k003`'s session on P1 traffic that P1 never saw.

`python session-verdict/main.py --selftest` replays section 7.1's 27 records with the
section 7.3 partition map and asserts 6 key records, 7 partition records (the extra one
being the idle key's, closed by its partition-mate) and 24 ordered current updates. It
passes.

## Deviations from spec-v2

1. **Out-of-order and late injection exist.** Spec section 5.4 defers them to v2.1; the
   build brief moves them into v2 so the two defects PR 994 fixed are exercised at scale.
   Four new parameters, no oracle change — exactly as section 5.4 predicted.
2. **The section 7 deployment block ships with `OUT_OF_ORDER_PCT=0` and `LATE_PCT=0`.**
   The hand-derived 6 / 7 / 24 table assumes a perfectly ordered stream. Run 2 (section
   10.9, random gaps) turns injection on; the values are in the header of
   `quix-v2-blocks.yaml`.
3. **One verdict record per `(probe, key)`** plus the summary record of section 8.3,
   rather than section 8.3's one per probe. Per the build brief; the per-probe verdict is
   still derivable and the summary carries it.
4. **`--selftest` entry point** in `session-verdict/main.py`. Spec section 9.4 lists only
   `main.py`, but risk R2 makes the section 7 run "the oracle's own acceptance test", and
   that has to be runnable without a broker or an environment.
5. **Both apps call `logging.basicConfig`.** `configure_logging` only touches the
   `quixstreams` logger (`logging.py:106-129`), so a module logger's INFO lines would fall
   through to `logging.lastResort` at WARNING and never reach stdout.
6. **The startup block is logged in `main()`**, in the parent process, not inside
   `Source.run()`. `Topic.broker_config` is populated by `TopicManager.topic()`
   (`manager.py:179-181`) in that process, and section 5.9 wants `num_partitions` printed
   next to the declared `PARTITION_COUNT`.
7. **Expected event count is logged as a range.** Stream lengths are drawn lazily per
   stream, so only `streams * MIN .. streams * MAX plus N closers` is known at startup.
   With `EVENTS_PER_STREAM_MIN == MAX` it is exact.
8. **`BASE_MS` is read with `os.environ.get("BASE_MS", "")`.** Its documented default is
   blank, and an empty Quix variable is not guaranteed to reach the container.
9. **Instance count and partition shape are warnings, not failing checks.** The oracle
   reads what landed, so a restart's replay is simply more input and the verdict stays
   self-consistent; acceptance criteria 2 and 3 are about the run's evidence value, not
   its correctness. Both are printed in the header and carried in the summary record.
10. **Late drops are printed, not asserted to be zero** (section 8.3 point 5 predates
    injection).
11. **The verdict topic is `session-v2-verdict`** (section 9.1), not the brief's
    "session-verdict", which is the application name.
12. **`quix.yaml` is not edited.** The five deployment blocks and five topic blocks are in
    `quix-v2-blocks.yaml`, ready to paste, per the build brief.
