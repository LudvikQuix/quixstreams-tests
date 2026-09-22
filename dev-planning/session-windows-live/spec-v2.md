# Session windows on a live Quix deployment — phase 2: continuous generator + oracle

**Status:** Draft
**Project:** quixstreams-tests (`LudvikQuix/quixstreams-tests`, branch `dev`, context `testrig`)
**Created:** 2026-09-22
**Planned with:** Buddy
**Feature under test:** `sdf.session_window()` on quix-streams branch `fix/pr-994`, commit `74275f72`
**Supersedes nothing.** Phase 1 (`spec.md`) stays in the repo and stays runnable.

---

## 1. Goal

Replace the scripted single-key Job with a **continuous generator Service** that produces
many keys across many partitions, with parameterised fixed and random silence gaps, and
replace the hard-coded expectation table with a **self-checking oracle** — an independent
reimplementation of the three session-window rules that reads the input topic and derives
the exact set of sessions each probe must emit.

Phase 1 proves the adversarial cases (out-of-order joins, bridging merges, deliberate
lateness, restart survival) on six hand-derived keys. Phase 2 proves the same three rules
hold at scale: dozens of keys, several partitions, hundreds of sessions, gaps drawn from a
seeded RNG — cases no human will ever write a table for.

---

## 2. What phase 1 proved, and what v2 adds

| | phase 1 (`spec.md`) | phase 2 (this spec) |
|---|---|---|
| generator | Job, 49-row literal table, 3 manual phases | Service, parameterised, one run |
| keys | 6, hand-named scenarios | `KEY_COUNT`, default 8, generic `k000…` |
| partitions | 1 | `PARTITION_COUNT`, default 2, keys > partitions |
| gaps | fixed by the table | fixed (`GAP_FIXED_MS`) or seeded-random |
| expectations | 49-row `EXPECTED` dict in the collector | computed by an oracle from the input topic |
| verdict runs | on the operator's laptop | **in-cluster Job**, laptop path kept as secondary |
| covers R1 join/extend | yes | yes, at scale |
| covers R1 **merge** | yes (S3) | **no** — v2 emits in ascending event-time order |
| covers R2 **lateness** | yes (S4) | **no** — same reason (see §5.4) |
| covers R3 close | yes | yes, per key and per partition |
| covers key-vs-partition contrast | yes (S5, one partition) | yes, **per partition** (§5.5) |
| covers restart survival | yes (S7) | no — phase 1 keeps that |

**The division of labour is deliberate.** Phase 1 is the deterministic regression case for
the adversarial orderings; v2 is the scale and randomness case. Both must pass. Neither is
a replacement for the other, and §11 keeps both deployable at once.

---

## 3. Contract under test

Unchanged from phase 1 §2, restated because every line of the oracle derives from it.
`G = inactivity_gap_ms`, `g = grace_ms`, `W = watermark`, source
`quixstreams/dataframe/windows/session.py` at the pinned commit.

| # | Rule | Code |
|---|---|---|
| R1 | **Join.** An event joins a stored session `[start, end)` iff `start - G <= ts` **and** `end + G > ts`. Matching two stored sessions merges them into `[min(start, ts), max(end, ts+1))`. | `_matches` (l. 205-212), `process_window` (l. 115-174) |
| R2 | **Late.** An event is dropped iff `ts < W - G - g`. `W` is the max event timestamp for **the key** (`closing_strategy="key"`) or **the partition** (`"partition"`). | `process_window` (l. 88-111) |
| R3 | **Close.** A session closes iff `end <= W - 2G - g`, where `end` is the last event plus one. | `close_before = late_before - gap`, `expire_by_key` (l. 214-259) |

Five implementation facts the oracle must also model, each verified in the source:

1. **`end = last_event + 1`.** A session whose last event is at `t` closes only once
   `W >= t + 1 + 2G + g` — one millisecond later than a naive `t + 2G + g`.
2. **`Collect` ordering.** `add_to_collection` stores under the composite key
   `(timestamp_ms, monotonic counter)` (`windowed/transaction.py:359-372`), so collected
   values come back in **event-time order, ties broken by arrival order**. Duplicate
   timestamps do **not** overwrite each other.
3. **`Earliest` / `Latest` are by event timestamp, not arrival.** `Earliest` keeps the
   incumbent on a tie (`timestamp < old_timestamp`); `Latest` replaces on a tie
   (`timestamp >= old_timestamp`) — `aggregations.py:294-360`.
4. **The partition watermark folds in the key's own watermark**:
   `advance_partition_timestamp(max(ts, state.get_latest_timestamp() or 0))`. On a fresh
   store the partition watermark already dominates every key's, so this reduces to "the max
   accepted timestamp seen in this partition". It matters only when a store written under
   `closing_strategy="key"` is later read under `"partition"`, which v2 never does.
5. **`.current()` rejects collectors** (`windows/base.py:170`). The current probe uses
   `Count() + Earliest("seq") + Latest("seq")`. Unchanged from phase 1.

The two expiry cursors (`expire_by_key`'s per-key cursor, `expire_by_partition`'s
partition checkpoint) are **optimisations that must be invisible**. The oracle does not model
them: it closes every session whose `end <= close_before`, every time. A disagreement that
traces to a cursor is therefore a finding, by design — see §8.3.

---

## 4. Topology

```
        Session Generator V2   (Service, Source subclass, min-heap scheduler)
                    │
                    ▼
        session-v2-in   (PARTITION_COUNT partitions, murmur2 keying)
        ┌───────────┬───────────┬───────────────────────────────┐
        ▼           ▼           ▼                               │
  Probe Key V2  Probe Part V2  Probe Curr V2                    │
  .final("key") .final("partition") .current("key")             │
        │           │           │                               │
        ▼           ▼           ▼                               │
  session-v2-   session-v2-   session-v2-                       │
   out-key      out-partition  out-current                      │
        └───────────┴───────────┴───────────────────────────────┘
                    │  all four topics
                    ▼
        Session Verdict V2  (Job)
          app.run(timeout=IDLE_TIMEOUT_S, metadata=True)
          → oracle over the input topic
          → compare per probe
          → verdict to the log AND to session-v2-verdict
```

**The three probes reuse `session-probe/` unchanged.** They are three more deployments of
the phase-1 application with v2 topic names and fresh consumer groups. `describe()` already
splits the message key on its first hyphen into `run_id` / `scenario`, and v2 keys are
`r3-k007`, so the existing code stamps `run_id="r3"`, `scenario="k007"` with no edit.
**ArchDev must not fork or modify `session-probe/`.**

---

## 5. The generator

### 5.1 Parameters

All in `session-generator-v2/app.yaml` **and** repeated with a concrete value in the
`quix.yaml` deployment block. No hyphens in variable names.

| name | type | default | what breaks if it is wrong |
|---|---|---|---|
| `output` | OutputTopic | `session-v2-in` | — |
| `RUN_ID` | FreeText | `r3` | Prefixes every key. **Must contain no hyphen** — the probe splits the key on the first one. A stale value lets a re-read of the retained topic contaminate the verdict. |
| `KEY_COUNT` | int | `8` | Must exceed `PARTITION_COUNT`, or no partition carries several keys and requirement 3 is untested. See §5.5 for the recommended ratio. |
| `IDLE_KEY_COUNT` | int | `1` | The last N keys emit one stream and get **no closer**. 0 removes the key-vs-partition contrast entirely. |
| `EVENTS_PER_STREAM_MIN` | int | `3` | `1` makes every stream a single-event session; fine, but `count` stops being evidence. |
| `EVENTS_PER_STREAM_MAX` | int | `3` | `MIN == MAX` gives fixed-length streams. `MAX < MIN` is an operator error the startup log shows. |
| `EVENT_INTERVAL_MS` | int | `60000` | **Must be `>= 1` and `<= GAP_MS`.** At `EVENT_INTERVAL_MS == GAP_MS` events still join by exactly 1 ms (`end + G = prev + 1 + G > prev + G`); at `GAP_MS + 1` every event becomes its own session and the stream concept collapses. `0` produces duplicate timestamps within a key, which is legal but makes `Earliest`/`Latest` tie-dependent. |
| `GAP_MODE` | FreeText | `fixed` | `fixed` \| `random`. |
| `GAP_FIXED_MS` | int | `150000` | The silence between two streams of one key. **Splits the key into two sessions iff `GAP_FIXED_MS >= GAP_MS + 1`.** 150000 vs 120001 → margin 29999 ms. See §5.6. |
| `GAP_RANDOM_MIN_MS` | int | `60000` | |
| `GAP_RANDOM_MAX_MS` | int | `300000` | The range **must straddle `GAP_MS`** — that is the point of random mode. 60000-300000 around `G=120000` splits roughly 60 % of gaps. A range entirely above `GAP_MS` degenerates to `fixed`; entirely below, every key produces exactly one long session. |
| `STREAMS_PER_KEY` | int | `2` | How many streams a non-idle key emits before retiring. Bounded, so the run terminates and can be judged. |
| `RUN_FOREVER` | FreeText | `false` | `true` ignores `STREAMS_PER_KEY`, emits no closers, never retires a key. Soak mode; see §5.7. |
| `KEY_STAGGER_MS` | int | `0` | Key `i`'s first event is at `BASE_MS + i * KEY_STAGGER_MS`. `0` synchronises every key onto the same ticks (maximum determinism, used by the hand-derived run of §7). Non-zero de-synchronises them, which makes partition sweeps fire at many distinct watermarks. |
| `SEED` | int | `20260922` | RNG seed. Same seed + same parameters + `SPEED=fast` ⇒ the same event stream, so a random run is reproducible and can be re-derived offline. |
| `SPEED` | FreeText | `fast` | `fast` \| `realtime`. See §5.3. |
| `BASE_MS` | FreeText | `""` (blank) | Event-time origin, epoch ms. **Blank means "now at process start"**, which is the v2 default — unlike phase 1, v2 is one run, so there is nothing to keep aligned across runs. A pinned value plus a Service restart replays the whole run into the same band and doubles every count; blank makes a restart land in a disjoint band instead. See risk R5. |
| `PRODUCE_RATE_MS` | int | `50` | Sleep between produces in `fast` mode only. `0` saturates the broker from a Service. |
| `GAP_MS` | int | `120000` | **Must equal every probe's `GAP_MS` and the verdict Job's.** The generator uses it only for the closer timestamp and for the split/no-split log line; the oracle uses it for everything. |
| `GRACE_MS` | int | `0` | Same: must match the probes and the verdict Job. |
| `PARTITION_COUNT` | int | `2` | **Log line only.** The `topics:` block in `quix.yaml` is authoritative; the generator logs `output_topic.broker_config.num_partitions` next to this value and a mismatch is visible at startup. |
| `LOGLEVEL` | FreeText | `INFO` | |

### 5.2 Per-key state machine and the interleaving rule

The generator is a `quixstreams.sources.Source` subclass registered with
`app.add_source(source, topic=app.topic(os.environ["output"], value_serializer="json",
key_serializer="str"))` and started with `app.run()` — the phase-1 pattern, for the same
reasons (`architecture.md` §"Generator").

Per key `i` it holds three numbers in memory: `next_event_ms[i]`,
`events_left_in_stream[i]`, `streams_done[i]`, plus `seq[i]` (monotonic across the whole run,
starting at 0) and a private `random.Random(SEED + i)`.

**The interleaving rule, stated exactly:**

> The generator holds a min-heap of `(next_event_ms, key_index)`. Each iteration pops the
> minimum, produces that one event, then pushes the key back with its new `next_event_ms`
> (or drops it if the key has retired). Ties are broken by ascending `key_index`.

Initialisation: `next_event_ms[i] = base_ms + i * KEY_STAGGER_MS`,
`events_left_in_stream[i] = rng_i.randint(EVENTS_PER_STREAM_MIN, EVENTS_PER_STREAM_MAX)`,
`streams_done[i] = 0`.

After producing an event for key `i` at time `t`:

```
events_left_in_stream[i] -= 1
if events_left_in_stream[i] > 0:
    next_event_ms[i] = t + EVENT_INTERVAL_MS              # inside a stream
else:
    streams_done[i] += 1
    limit = 1 if i is an idle key else STREAMS_PER_KEY
    if not RUN_FOREVER and streams_done[i] >= limit:
        retire key i                                       # not pushed back
    else:
        gap = GAP_FIXED_MS                                 # GAP_MODE=fixed
              or rng_i.randint(GAP_RANDOM_MIN_MS, GAP_RANDOM_MAX_MS)
        next_event_ms[i] = t + gap                         # gap measured from the LAST event
        events_left_in_stream[i] = rng_i.randint(EVENTS_PER_STREAM_MIN, EVENTS_PER_STREAM_MAX)
```

Idle keys are the last `IDLE_KEY_COUNT` indices: they emit exactly one stream and receive
no closer.

Three consequences of the heap, all load-bearing:

* **The produced stream is globally sorted by event time.** Within any partition, the
  watermark is always the timestamp of the most recent message, which is what makes the
  hand-derivation in §7 tractable and the oracle's R3 sweeps easy to follow.
* **No event is ever late and no R1 merge ever happens.** Lateness and merging are phase
  1's job (§2). This is not a limitation of the oracle — see §5.4.
* **With `KEY_STAGGER_MS = 0` the order within one tick is key-index order** — a strict
  round robin, and every key shares the same timestamps.

### 5.3 Time model — `fast` and `realtime`

| | `SPEED=fast` | `SPEED=realtime` |
|---|---|---|
| `base_ms` | `BASE_MS` or `now` | `now` |
| event timestamp | `next_event_ms` from the schedule, produced as fast as `PRODUCE_RATE_MS` allows | same number, and the generator sleeps until wall clock reaches it |
| a 2.5-minute gap costs | ~0 s | 150 s |
| use for | every iteration, the acceptance run | one confirmation run that the rig behaves the same when event time equals wall clock |

`realtime` sleeps `max(0, next_event_ms - now_ms)` before each produce; `PRODUCE_RATE_MS`
is ignored. Everything else is identical, including the closer phase (which in `realtime`
means waiting `3 * GAP_MS` of real time — 6 minutes at `G = 120000`).

Timestamps are in the future in `fast` mode, as in phase 1 §4.3. That is deliberate:
future timestamps cannot be deleted early by time-based retention. Same `CreateTime`
assumption, same fallback (risk R6).

### 5.4 Ordering is deliberately in-order — and why the oracle makes that cheap to change

The oracle reads the **input topic in `(partition, offset)` order**, so whatever order
actually landed is the oracle's input. Out-of-order or late injection would therefore need
**zero oracle changes** — one extra generator parameter (e.g. `OUT_OF_ORDER_PCT`, which
would re-order the heap pop by holding an event back N pops) and the verdict still works.
That is v2.1, not v2: phase 1 already covers those code paths deterministically, and adding
a second random axis before the first one is proven makes a disagreement harder to triage.
Recorded here so nobody reinvents the oracle when it is added.

### 5.5 Multi-key and multi-partition

* **The generator never sets an explicit partition.** `Source.produce()` is called without
  `partition=`, so the mapping is the real one a user gets: librdkafka's `murmur2` — quix
  streams pins `"partitioner": "murmur2"` in `kafka/producer.py:127`, which is the Java
  default, **not** librdkafka's own `consistent_random` default. The generator does not
  reimplement it and does not try to predict the mapping.
* **`KEY_COUNT` must exceed `PARTITION_COUNT`** so at least one partition carries several
  keys. Because murmur2 over a handful of keys is lumpy, use `KEY_COUNT >= 4 *
  PARTITION_COUNT` for any run whose verdict is meant to mean something (defaults: 8 keys,
  2 partitions). The **verdict Job reports the observed `key → partition` map** from the
  input records' `_partition` metadata — that is authoritative and needs no hashing.
* **With `PARTITION_COUNT > 1` the `closing_strategy="partition"` watermark is per
  partition.** The state store is per `(topic, partition)`, the
  `WindowedPartitionTransaction` is per partition, and `advance_partition_timestamp` writes
  under the empty prefix of that partition's store. So the oracle must group by partition,
  never globally.
* **Cross-partition interleaving is irrelevant to the result.** In key mode the outcome
  depends only on the per-key order; in partition mode only on the per-partition order.
  Both are exactly the `(partition, offset)` order the oracle replays. This is what makes
  the oracle deterministic without knowing how the probe's consumer interleaved its
  partitions — state it in the code as the one-line justification for sorting by
  `(_partition, _offset)`.
* **For the probes**, one consumer instance holds all partitions (`replicas: 1`); each
  partition gets its own RocksDB store under the same volume. No probe code changes.

### 5.6 "At least 2 windows with 2.5 min gap"

With `GAP_MS = 120000`, a 150 000 ms silence splits a key into two sessions:
`end + G = last + 1 + 120000 <= last + 150000`, so R1 does not match and a new session
opens. The split condition is exactly `GAP_FIXED_MS >= GAP_MS + 1`.

**The 2.5 minutes is the silence between two streams, not the inactivity gap.** If anyone
sets `GAP_MS = 150000`, a 150 s silence stops splitting (`end + G = last + 150001 >
last + 150000` → it joins) and the whole scenario becomes one session. `GAP_MS` must stay
below `GAP_FIXED_MS`. The generator logs, per gap drawn, whether it splits, and a final
`splits=<n> joins=<n>` line.

### 5.7 End of run — the closer phase

A `final` probe emits nothing for a session that never closes, so a bounded run must push
the watermark past every open session. When the heap empties (all keys retired):

```
T_close = max_event_ms_emitted + 3 * GAP_MS + GRACE_MS
for each NON-IDLE key, in key-index order:
    produce(key, ts=T_close, seq=-1)
```

`3G` reproduces phase 1's convention and clears both bounds with margin: it does not join
the last session (`end + G = last + 1 + G <= last + 3G` for `G >= 1`) and it closes it
(`end = last + 1 <= T_close - 2G - g = max_event + G - g`).

Each closer opens a one-event session `[T_close, T_close+1)` that **never closes**, because
nothing follows it. So unlike phase 1, **v2 expects zero closer-only records on every output
topic** — the oracle predicts the closer sessions as still-open and the verdict compares the
full multiset including them. Any record with `seqs == [-1]` is a finding (§10 criterion 7).

Then the Source logs `RUN COMPLETE` with the manifest summary and **idles**:
`while self.running: time.sleep(1)`. `Source.stop()` sets `running` to `False` on SIGTERM,
`run()` returns, `app.run()` returns, the Service shuts down cleanly. `run()` must **not**
return on its own: a Service whose process exits is restarted by the platform and would
replay the entire run.

In `RUN_FOREVER=true` mode there is no closer phase and no idle: each key's previous
sessions still close as that key keeps producing, so the `final` probes emit continuously.
The last session of every key stays open — which the oracle predicts correctly, so no
special handling is needed. The operator stops the generator before running the verdict.

### 5.8 Message payload

`key = f"{RUN_ID}-k{i:03d}"`. Value:

```json
{
  "run_id": "r3",
  "instance_id": "8f3c…",     // uuid4 chosen once at process start
  "key_index": 7,
  "stream_index": 1,          // 0-based, the generator's notion of a burst
  "seq": 5,                   // monotonic per key across the whole run; -1 for a closer
  "event_ms": 1790086836421,
  "base_ms": 1790086446421,
  "gap_used_ms": 150000       // the silence that preceded this stream; 0 for stream 0
}
```

`instance_id` is the restart detector: the verdict counts distinct values and fails the run
if there is more than one (§10 criterion 2).

`self.flush()` after every produce, as in phase 1. At `PRODUCE_RATE_MS = 50` that is free,
and it keeps the produced order equal to the scheduled order so the hand-derivation of §7
holds literally. Note that ordering is **not** load-bearing for correctness in v2 — the
oracle judges whatever order landed — so a soak run may raise `PRODUCE_RATE_MS` or batch
without invalidating the verdict.

### 5.9 Startup log

One block, mirroring `session-probe`'s: `quixstreams` version, resolved output topic name
(must carry the workspace prefix), `output_topic.broker_config.num_partitions` **next to**
the declared `PARTITION_COUNT`, `base_ms`, `instance_id`, and every parameter from §5.1.
Then the derived numbers: total keys, idle keys, expected event count
(`sum over keys of events` + closers), and `T_close` once it is known.

---

## 6. The oracle

### 6.1 Data source: the input topic, read start to end

**Chosen: the input topic.** Not the generator's manifest.

* It verifies what actually *landed*, including any reordering, duplication or loss between
  the generator and the probes — the manifest would assert the generator's intent and
  silently forgive a broker-side surprise.
* It carries the **real partition** per record, so nothing has to reimplement murmur2.
* It makes the oracle **independent of every generator parameter**: it needs `GAP_MS` and
  `GRACE_MS` and nothing else. `SEED`, `GAP_MODE`, `STREAMS_PER_KEY` are invisible to it.
  A generator bug therefore cannot hide itself in the expectation.
* It is exactly the byte stream the probes consumed, so a disagreement cannot be blamed on
  "the verdict read something else".

The generator's manifest is still logged (§5.9) for human diagnosis, but no assertion
depends on it.

### 6.2 Algorithm

Input: the list of input-topic records, sorted by `(_partition, _offset)`. Run it **once per
probe configuration** — `(strategy="key")` for the key probe and the current probe,
`(strategy="partition")` for the partition probe.

```
per (partition, key):  sessions  = sorted list of Session(start, end, count, events)
                       key_wm    = 0            # max accepted ts for this key
per partition:         part_wm   = 0            # max accepted ts for this partition

for record in records sorted by (_partition, _offset):
    p, k, ts, seq = record._partition, record._key, record._timestamp, record.seq

    if strategy == "partition":
        part_wm[p] = W = max(part_wm[p], ts, key_wm[p,k])
    else:
        W = max(ts, key_wm[p,k])

    late_before  = W - G - g
    close_before = late_before - G

    # R2
    if ts < late_before:
        late_drops.append((k, ts)); continue        # no state change whatsoever

    # R1 — probe the two immediate neighbours in start order
    prev = last session of (p,k) with start <= ts
    foll = first session of (p,k) with start >= ts + 1
    matched = [s for s in (prev, foll) if s.start - G <= ts and s.end + G > ts]

    if len(matched) == 2:
        new = Session(min(prev.start, ts), max(foll.end, ts + 1),
                      prev.count + 1 + foll.count,
                      prev.events + [(ts, arrival_index, seq)] + foll.events)
        remove prev, foll; insert new
    elif len(matched) == 1:
        m = matched[0]
        m.start = min(m.start, ts); m.end = max(m.end, ts + 1)
        m.count += 1; m.events.append((ts, arrival_index, seq))
    else:
        insert Session(ts, ts + 1, 1, [(ts, arrival_index, seq)])

    key_wm[p,k] = max(key_wm[p,k], ts)

    # the current probe's update, emitted for the session the event landed in
    current_updates[k].append((new_or_matched.start, new_or_matched.end,
                               new_or_matched.count,
                               earliest_seq(new_or_matched), latest_seq(new_or_matched)))

    # R3 — the sweep scope is the whole point of the two strategies
    scope = all keys of partition p   if strategy == "partition"   else [k]
    for key2 in scope:
        for s in sessions[p, key2] with s.end <= close_before, in start order:
            final_expected[strategy].add((key2, s.start, s.end, s.count, seqs_of(s)))
            remove s
```

with

* `seqs_of(s)` = the `seq` values of `s.events` sorted by `(ts, arrival_index)` — §3 fact 2;
* `earliest_seq(s)` = the `seq` of the event with the smallest `ts`, **first-seen wins on a
  tie**; `latest_seq(s)` = largest `ts`, **last-seen wins on a tie** — §3 fact 3;
* `arrival_index` = a global counter over the sorted record list, so ties are resolved the
  way the store's collection counter resolves them.

Update-then-expire is the SDK's order and must be the oracle's. The just-written session can
never itself be due (`end >= ts + 1` and `close_before <= ts - 2G - g`), but a session of
another key in the same partition frequently is.

### 6.3 Output schema

| probe | shape |
|---|---|
| `key` | `Counter[(key, start, end, count, tuple(seqs))]` — run with `strategy="key"` |
| `partition` | `Counter[(key, start, end, count, tuple(seqs))]` — run with `strategy="partition"` |
| `current` | `dict[key, list[(start, end, count, first_seq, last_seq)]]`, ordered — from the `strategy="key"` run |

All timestamps **absolute**. v2 needs no `base_ms` normalisation: the oracle reads the real
timestamps off the input topic, so the phase-1 `derive_base_ms` step and its failure mode
disappear.

Observed side, from the three output topics: the same shapes, built from the probe records'
`key`, `start`, `end`, `count`, `seqs` / `first_seq`, `last_seq` fields, filtered to
`run_id == RUN_ID`. Order for the `current` probe comes from `(_partition, _offset)` on the
output topic, which is a total order because the v2 output topics have one partition (§9).

### 6.4 The oracle is a reimplementation, and that cuts both ways

State this plainly in the module docstring and in the README:

> The oracle implements the same three rules `session.py` implements. A disagreement is a
> finding **in one of them**, and a bug in the oracle is exactly as likely as a bug in the
> SDK until someone traces the disagreement by hand.

Two rules follow, both binding on ArchDev:

1. **Write the oracle from §3, not from `session.py`.** A line-by-line transliteration
   reproduces the SDK's bugs and the rig passes while the feature is broken. In particular
   do **not** port the expiry cursors (§3, last paragraph).
2. **Triage protocol for any FAIL** (§10): reduce to the single disagreeing key, hand-trace
   its `<= 10` events against R1/R2/R3, only then say which side is wrong, and reproduce it
   as a red unit test in quix-streams before any code change.

---

## 7. The 2.5-minute fixed scenario, hand-derived

Run it first. Parameters:

```
KEY_COUNT=4  PARTITION_COUNT=2  IDLE_KEY_COUNT=1  KEY_STAGGER_MS=0
GAP_MODE=fixed  GAP_FIXED_MS=150000
EVENT_INTERVAL_MS=60000  EVENTS_PER_STREAM_MIN=EVENTS_PER_STREAM_MAX=3
STREAMS_PER_KEY=2  SPEED=fast  GAP_MS=120000  GRACE_MS=0  RUN_ID=r3
```

Keys `r3-k000 … r3-k003`; `r3-k003` is the idle key. All offsets below are from `base_ms`.

### 7.1 The event list

24 events. `KEY_STAGGER_MS = 0`, so all keys share the same ticks and the order within a
tick is key-index order.

| tick (offset) | keys emitting, in order | seq |
|---|---|---|
| 0 | k000, k001, k002, k003 | 0 |
| 60000 | k000, k001, k002, k003 | 1 |
| 120000 | k000, k001, k002, k003 | 2 — stream 0 ends; **k003 retires** |
| 270000 | k000, k001, k002 | 3 — 150 000 ms after the last event |
| 330000 | k000, k001, k002 | 4 |
| 390000 | k000, k001, k002 | 5 — stream 1 ends, all three retire |
| 750000 | k000, k001, k002 | −1 — closers, `390000 + 3 * 120000` |

`T_close = 390000 + 360000 = 750000`. k003 gets no closer.

**Partitions.** murmur2 decides; nobody can predict it on paper. The derivation below
assumes `k000 → P0, k001 → P1, k002 → P0, k003 → P1` — it is written out so the *shape* of
the answer is checkable; the oracle recomputes it from the map the verdict Job reports.
§7.5 says what changes if the map differs.

### 7.2 Key probe (`closing_strategy="key"`) — 6 records

Per active key, in its own event order (each key's watermark is its own):

| its event | `W` | `close_before` | effect |
|---|---|---|---|
| 0 | 0 | −240000 | new session `[0, 1)` |
| 60000 | 60000 | −180000 | `end + G = 120001 > 60000` → extend to `[0, 60001)` |
| 120000 | 120000 | −120000 | extend to `[0, 120001)`, count 3 = **session A** |
| 270000 | 270000 | 30000 | A: `end + G = 240001 <= 270000` → no match → new `[270000, 270001)`; A `end 120001 > 30000`, stays open |
| 330000 | 330000 | 90000 | extend to `[270000, 330001)`; A still open (margin 30000) |
| 390000 | 390000 | **150000** | extend to `[270000, 390001)` = **session B**; A `end 120001 <= 150000` → **A closes and is emitted** |
| 750000 | 750000 | 510000 | closer: `B.end + G = 510001 <= 750000` → no match → new `[750000, 750001)`; B `end 390001 <= 510000` → **B closes and is emitted** |

| key | records |
|---|---|
| `r3-k000` | `[0, 120001)` count 3 seqs `[0,1,2]`  ·  `[270000, 390001)` count 3 seqs `[3,4,5]` |
| `r3-k001` | identical |
| `r3-k002` | identical |
| `r3-k003` | **none** — its own watermark stops at 120000, so `close_before = -120000` and nothing is ever due |

**Total: 6 records. No closer-only record** — each closer's own session `[750000, 750001)`
never closes.

### 7.3 Partition probe (`closing_strategy="partition"`) — 7 records

P0 = {k000, k002}, both active. P0's message order is k000, k002 alternating at each tick.

| P0 message | `W` | `close_before` | sweep? | emitted |
|---|---|---|---|---|
| k000@0 | 0 | −240000 | yes (checkpoint unset) | — |
| k002@0 … k002@120000 | ≤120000 | ≤ −120000 | gated / nothing due | — |
| k000@270000 | 270000 | 30000 | yes | — (both A's end 120001 > 30000) |
| k002@270000 … k002@330000 | ≤330000 | ≤90000 | gated / nothing due | — |
| **k000@390000** | 390000 | **150000** | yes | **k000 A and k002 A** — k002's A closes on k000's message |
| k002@390000 | 390000 | 150000 | gated | — |
| **k000@750000** | 750000 | **510000** | yes | **k000 B and k002 B** |
| k002@750000 | 750000 | 510000 | gated | — |

P1 = {k001 active, k003 idle}. P1's order is k001, k003 alternating for the first three
ticks, then k001 alone.

| P1 message | `W` | `close_before` | emitted |
|---|---|---|---|
| k001@0 … k003@120000 | ≤120000 | ≤ −120000 | — |
| k001@270000 | 270000 | 30000 | — |
| k001@330000 | 330000 | 90000 | — |
| **k001@390000** | 390000 | **150000** | **k001 A and k003 A** — the idle key emits, closed by its partition-mate's traffic |
| **k001@750000** | 750000 | 510000 | **k001 B** |

**Total: 7 records** — the 6 of the key probe **plus** `r3-k003 [0, 120001)` count 3 seqs
`[0,1,2]`. That one extra record is the whole key-vs-partition contrast, now proven inside a
multi-partition topic. Still no closer-only records.

Every "gated" row above was checked against `expire_by_partition`'s checkpoint arithmetic
(`min over prefixes of first_open.end + 2G + g`): the checkpoint lands 1 ms above the next
watermark that could close anything, so the gate never suppresses a due session. The
checkpoint values along P0 are 240001 → 360001 → 570001 → 990001.

### 7.4 Current probe (`.current(closing_strategy="key")`) — 24 updates

One update per accepted event. No event is late, so **the current probe's record count
equals the input event count** — a cheap global invariant (§10 criterion 4).

| key | ordered `(start, end, count, first_seq, last_seq)` |
|---|---|
| `r3-k000`, `r3-k001`, `r3-k002` | `(0,1,1,0,0)` `(0,60001,2,0,1)` `(0,120001,3,0,2)` `(270000,270001,1,3,3)` `(270000,330001,2,3,4)` `(270000,390001,3,3,5)` `(750000,750001,1,-1,-1)` |
| `r3-k003` | `(0,1,1,0,0)` `(0,60001,2,0,1)` `(0,120001,3,0,2)` |

`3 × 7 + 3 = 24`. ✓

### 7.5 If the partition map differs

The oracle is unaffected — it groups by the observed `_partition`. What changes is the
**evidence**:

* If `k003` lands alone on a partition, its session never closes on either probe, the
  partition probe emits 6 records and the key-vs-partition contrast is **vacuous**. The
  verdict Job flags this (§10 criterion 3) and the run must be repeated with a different
  `KEY_COUNT`.
* If all four keys land on one partition, the multi-partition claim is untested. Same flag,
  same remedy.

---

## 8. The verdict Job

### 8.1 Where the oracle runs, and why

**In-cluster, as a Quix Job (`session-verdict/`).** `tools/collect_results.py` currently
cannot reach the broker from the laptop: the PAT-authenticated `Application` builds, the
portal returns the librdkafka config, and `list_topics` raises
`KafkaError{code=_TRANSPORT,val=-195,"Failed to get metadata: Local: Broker transport
failure"}` — with raw TCP to both `broker-bootstrap-test…:30095` and `kafka-0-test…:30095`
open from that machine. Broker access from inside the workspace is known-good (three probes
have been consuming and producing for two runs). Putting the oracle where the broker works
takes the unresolved transport issue off the critical path entirely.

`tools/collect_results.py` stays exactly as it is, for phase 1, and the transport failure
stays open as risk R1 with its two hypotheses.

The verdict app runs **unchanged** on the laptop the day the transport is fixed: `Application()`
with no arguments picks up `Quix__Sdk__Token` + `Quix__Portal__Api` from the environment
(`app.py:288-326`), and `platforms/quix/api.py:78` accepts a PAT where an SDK token is
expected. So exporting the three `.env` values and running `python session-verdict/main.py`
is the secondary path, with no second copy of the oracle.

### 8.2 Completion rule

`app.run(timeout=IDLE_TIMEOUT_S, metadata=True, count=MAX_RECORDS)` over **all four topics
at once** — the input topic and the three output topics, one no-op
`app.dataframe(app.topic(...))` each. `timeout` is an **idle** timeout, so the call returns
once nothing has arrived on any of the four for `IDLE_TIMEOUT_S`.

That is a correct quiescence detector, and the reason is worth a comment: the probes emit
only in response to input, so "input quiet **and** all three outputs quiet" means either the
probes are caught up or a probe is stuck — and a stuck probe shows up as MISSING records
with its lag visible in the per-topic counts the verdict prints. `RunTracker` gives the
timeout a 60 s head start (`runtracker.py:166`), so the Job takes roughly
`IDLE_TIMEOUT_S + 60` s and prints nothing until it returns.

Default `IDLE_TIMEOUT_S = 60`, `MAX_RECORDS = 200000`.

The records come back **flat**, with the metadata prefixed: `_key`, `_topic`, `_partition`,
`_offset`, `_timestamp` merged alongside the deserialized value
(`runtracker.py:24-47`, confirmed in phase 1's `architecture.md`). So the oracle gets the
input partition with no `message_context()` call and no hand-rolled consumer.

No control topic, no end-of-run marker, no timer thread. If a run needs judging while the
generator is still producing (`RUN_FOREVER`), stop the generator first.

### 8.3 What it emits

To **stdout** (the primary evidence path, read with `quix cloud deployments logs <guid>`):

1. The run header: `run_id`, `GAP_MS`, `GRACE_MS`, records read per topic, distinct
   `instance_id` values, `base_ms` values seen.
2. The observed **key → partition map**, and a `PARTITION SHAPE` line naming any partition
   holding fewer than 2 keys and whether an idle key is among them.
3. The verdict table: one row per `(probe, key)` with expected / observed counts and
   `PASS | FAIL`.
4. Every `MISSING`, `UNEXPECTED`, `DUPLICATE` and `ORDER` finding, in that order, one per
   line, with the full `(key, start, end, count, seqs)` tuple.
5. Aggregates: total sessions expected and observed per probe, sessions per key
   (min/median/max), keys with 1 session vs `>= 2` (did the split actually happen), late
   drops predicted by the oracle (must be 0 in v2).
6. `PASS` / `FAIL` and the failing-check count. Exit code 1 on any failure.

To **`session-v2-verdict`**, via `app.get_producer()` after `app.run()` returns, one record
per probe plus one summary record, keyed by `f"{RUN_ID}-{probe}"`:

```json
{ "run_id": "r3", "probe": "key", "verdict": "PASS",
  "expected": 6, "observed": 6,
  "missing": [], "unexpected": [], "duplicate": [], "order": [] }
```

```json
{ "run_id": "r3", "probe": "summary", "verdict": "PASS",
  "input_events": 24, "keys": 4, "partitions": 2,
  "key_partition_map": {"r3-k000": 0, "r3-k001": 1, "r3-k002": 0, "r3-k003": 1},
  "instances": 1, "base_ms": [1790086446421],
  "gap_ms": 120000, "grace_ms": 0,
  "sessions_per_probe": {"key": 6, "partition": 7, "current": 24} }
```

### 8.4 Parameters

| name | type | default | note |
|---|---|---|---|
| `input` | InputTopic | `session-v2-in` | the oracle's data source |
| `key_input` | InputTopic | `session-v2-out-key` | |
| `partition_input` | InputTopic | `session-v2-out-partition` | |
| `current_input` | InputTopic | `session-v2-out-current` | |
| `output` | OutputTopic | `session-v2-verdict` | |
| `RUN_ID` | FreeText | `r3` | records of any other run are counted and ignored |
| `GAP_MS` | int | `120000` | **must equal the probes'**; the oracle's only physical constant |
| `GRACE_MS` | int | `0` | same |
| `IDLE_TIMEOUT_S` | int | `60` | |
| `MAX_RECORDS` | int | `200000` | memory ceiling for the collect-mode run |
| `CONSUMER_GROUP` | FreeText | `sv2_r3` | fresh per run, or the Job resumes and reads nothing |
| `LOGLEVEL` | FreeText | `INFO` | |

It does **not** take `SEED`, `GAP_MODE`, `KEY_COUNT`, `STREAMS_PER_KEY` or `PARTITION_COUNT`.
Everything else is derived from what it reads (§6.1).

---

## 9. Topology and `quix.yaml` changes

### 9.1 New topics

| name | partitions | retentionInMinutes | why |
|---|---|---|---|
| `session-v2-in` | **`PARTITION_COUNT`** (default 2) | 1440 | the multi-partition subject; authoritative source of the partition count |
| `session-v2-out-key` | 1 | 1440 | one partition gives a total order, so the `current` probe's per-key ordering assertion is exact and free |
| `session-v2-out-partition` | 1 | 1440 | " |
| `session-v2-out-current` | 1 | 1440 | " |
| `session-v2-verdict` | 1 | 1440 | one record per probe plus a summary |

Existing `session-in`, `session-out-*` are untouched — phase 1 keeps working.

### 9.2 New deployments

Names must never have existed in this workspace (deleting a deployment orphans its state
volume; a collision re-attaches a stale one).

| deployment | application | type | resources (limits) | state | desiredStatus |
|---|---|---|---|---|---|
| `Session Generator V2` | `session-generator-v2` | Service | cpu 200 / mem 300 | — | **Stopped** |
| `Session Probe Key V2` | `session-probe` | Service | cpu 200 / mem 500 | `{enabled: true, size: 1}` | Running |
| `Session Probe Partition V2` | `session-probe` | Service | cpu 200 / mem 500 | `{enabled: true, size: 1}` | Running |
| `Session Probe Current V2` | `session-probe` | Service | cpu 200 / mem 500 | `{enabled: true, size: 1}` | Running |
| `Session Verdict V2` | `session-verdict` | Job | cpu 200 / mem 500 | — | (Job) |

`desiredStatus: Stopped` on the generator is deliberate: the operator starts it only after
the three probes are Running and their state-dir lines are verified, and a Stopped
deployment is the one a `PATCH /deployments/{id}` with `{"variables": {...}}` is accepted
on. The verdict Job auto-runs once on creation and will FAIL against an empty topic set —
harmless, same wrinkle phase 1 had with `PHASE=s7a`.

### 9.3 Probe variables for the three V2 deployments

Identical to phase 1 except:

| name | Key V2 | Partition V2 | Current V2 |
|---|---|---|---|
| `input` | `session-v2-in` | `session-v2-in` | `session-v2-in` |
| `output` | `session-v2-out-key` | `session-v2-out-partition` | `session-v2-out-current` |
| `CONSUMER_GROUP` | `sw2_key_r3` | `sw2_part_r3` | `sw2_curr_r3` |

`PROBE`, `EMIT_MODE`, `CLOSING_STRATEGY`, `GAP_MS`, `GRACE_MS`, `STORE_NAME`, `LOGLEVEL`
carry the phase-1 values. Every variable is repeated explicitly in both `app.yaml` and each
`quix.yaml` block — syncing rewrites `quix.yaml` lossily from the Portal side.

### 9.4 Repo change list

**Add:**

```
session-generator-v2/{main.py, app.yaml, dockerfile, requirements.txt}
session-verdict/{main.py, app.yaml, dockerfile, requirements.txt}
```

Both dockerfiles: `FROM python:3.13-slim-trixie`, the `apt-get install -y
--no-install-recommends git` layer, requirements copied before the source,
`ENTRYPOINT ["python3", "main.py"]`. Both `requirements.txt`:
`quixstreams @ git+https://github.com/quixio/quix-streams.git@74275f72ddd54ea6698c7acb8bff1e5663ec2c75`
— the same pin as the three existing apps.

**Modify:**

* `quix.yaml` — add the five deployments of §9.2 and the five topics of §9.1. Change
  nothing in the four existing blocks.
* `README.md` — add a "Phase 2" section: what v2 adds over phase 1 (§2's table), the
  parameter table (§5.1), the oracle's contract and the "a bug in the oracle is as likely"
  warning (§6.4), and the v2 runbook (§11).

**Do not touch:** `session-generator/`, `session-probe/`, `tools/collect_results.py`, the
four phase-1 topics, the four phase-1 deployment blocks, `dev-planning/session-windows-live/
spec.md` and `architecture.md`.

### 9.5 Work breakdown

| # | sub-feature | depends on | owner |
|---|---|---|---|
| 1 | `session-generator-v2/main.py` — heap scheduler, §5.2/§5.3/§5.7/§5.8/§5.9 | — | ArchDev |
| 2 | `session-verdict/main.py` — collect-mode read, oracle §6.2, comparison, §8.3 output | — | ArchDev |
| 3 | `quix.yaml` topics + deployments (§9.1-9.3), both `app.yaml`s, both dockerfiles | 1, 2 | ArchDev |
| 4 | `README.md` phase-2 section | 1-3 | DocuGuy |
| 5 | Lint + import smoke (`python -c "import main"` per app dir) | 1-3 | Tester |
| 6 | Deploy, run §11, read the verdict | 5 | main thread (operator) |

Sub-features 1 and 2 are independent and can be built in parallel; only `GAP_MS`, `GRACE_MS`
and the payload shape of §5.8 couple them, and the oracle depends on none of the payload
except `seq` and `run_id`.

---

## 10. Acceptance criteria

1. **`Session Verdict V2` exits 0** and prints `verdict=PASS` for all three probes.
2. **Exactly one distinct `instance_id`** among the input records — the generator Service did
   not restart mid-run. More than one invalidates the idle-key contrast (a second band's
   traffic closes the idle key's session on the key probe too) and the run must be repeated.
3. **Partition shape is non-vacuous**: every partition that carries an idle key also carries
   at least one active key, and at least two partitions are non-empty. Otherwise the
   key-vs-partition contrast and the multi-partition claim prove nothing; rerun with a
   different `KEY_COUNT` (§7.5).
4. **`current` probe record count == accepted input event count.** In v2 no event is late,
   so every input event produces exactly one update. A shortfall localises the failure to a
   specific key and event before any session arithmetic is needed.
5. **At least half the non-idle keys show `>= 2` sessions** — the gap actually split
   something. In `GAP_MODE=fixed` with `GAP_FIXED_MS >= GAP_MS + 1` it must be *all* of them.
6. **The fixed 2.5-minute run of §7 matches its hand-derived table**: 6 records on
   `session-v2-out-key`, 7 on `session-v2-out-partition` (the extra one being the idle key's
   first session), 24 updates on `session-v2-out-current` in the per-key order of §7.4 —
   modulo the observed partition map (§7.5).
7. **No record on any output topic has `seqs == [-1]`** (final probes) or
   `count == 1 and last_seq == -1` (current probe) other than the per-key closer updates
   §7.4 lists. Closer *sessions* must stay open; a closer-only session record means a
   watermark advanced further than the rules allow.
8. **Every V2 probe logs `state_dir == Quix__Deployment__State__Path`** with
   `Quix__Deployment__State__Enabled=true`.
9. **A random-mode run** (`GAP_MODE=random`, `GAP_RANDOM_MIN_MS=60000`,
   `GAP_RANDOM_MAX_MS=300000`, `KEY_COUNT=8`, `STREAMS_PER_KEY=5`, `KEY_STAGGER_MS=7000`,
   fresh `SEED`) also passes 1-5, 7, 8, and its log shows both `splits` and `joins` non-zero.

Any FAIL is a finding against `fix/pr-994` — after the §6.4 triage, and reproduced as a red
unit test before any code changes.

---

## 11. Runbook

Prerequisites: `quix use testrig`; phase-1 deployments may stay as they are (see risk R4 on
quota).

1. **Choose the parameters.** For the first run use §7's fixed 2.5-minute set. Put them in
   the `Session Generator V2` block in `quix.yaml`; leave `BASE_MS` blank.
2. **Commit and push `dev`.** `quix.yaml` must be in the same commit as the two new app
   directories or the sync is rejected with "Reference should have affected the workspace
   descriptor".
3. **Sync.** `POST /workspaces/{ws}/pull`, then `quix cloud environments sync <ws>`. Without
   the pull the sync compares the old commit to itself.
4. **Wait** for the three V2 probes to reach Running. `Session Generator V2` is created
   Stopped; `Session Verdict V2` auto-runs once and FAILs against empty topics — ignore it.
5. **Verify the state volume** in each V2 probe's log (`quix cloud deployments logs <guid>`):
   `state_dir` must equal `Quix__Deployment__State__Path` and
   `Quix__Deployment__State__Enabled` must be `true`. If not, stop — the rig would still
   pass via changelog recovery and prove nothing about the volume.
6. **Start the generator.** `PUT /deployments/{id}/start`. Watch its log for the startup
   block, then the per-event lines, then `splits=… joins=…`, then `RUN COMPLETE` with
   `T_close` and the event count. In `SPEED=fast` with the §7 parameters this takes about
   30 s (24 events at `PRODUCE_RATE_MS=50` plus startup).
7. **Wait ~60 s** for the probes to drain and their checkpoints to commit (default
   `commit_interval` 5 s).
8. **Run the verdict.** The Job is Completed, so a `PATCH /deployments/{id}` with
   `{"variables": {"RUN_ID": "r3", "CONSUMER_GROUP": "sv2_r3", ...}}` — a **dict keyed by
   name**, not a list — is accepted; it is silently ignored on a Running deployment. Then
   `PUT /deployments/{id}/start` and wait for Completed. Read the verdict with
   `quix cloud deployments logs <guid>`; it prints nothing for ~90 s
   (`IDLE_TIMEOUT_S` + `RunTracker`'s 60 s head start).
9. **Stop the generator** (`PUT /deployments/{id}/stop`) so it does not idle against quota.
10. **Second run — random mode.** Bump `RUN_ID`, all three probe `CONSUMER_GROUP`s, the
    verdict `CONSUMER_GROUP` and `SEED` together, set `GAP_MODE=random` with the §10.9
    parameters, and repeat from step 2.

**Reset for a rerun:** bump `RUN_ID`, the three probe `CONSUMER_GROUP`s, the verdict
`CONSUMER_GROUP`, and `SEED` **together**. A new consumer group gives each probe a fresh
state directory (`StateStoreManager` roots the store at `state_dir / consumer_group`) and a
fresh changelog; the new `RUN_ID` keeps the replayed old-run keys disjoint; `BASE_MS` stays
blank so the new run's band is naturally above the old one. A hard reset (delete and
recreate the five v2 topics) is only needed if the generator was accidentally started twice
within one `RUN_ID` — and criterion 2 detects exactly that.

**`realtime` confirmation run (optional, ~15 min):** the §7 parameters with
`SPEED=realtime`. Expect the identical verdict; the only difference is that the generator
takes 2.5 real minutes over the gap and 6 real minutes before the closers.

---

## 12. Risks and open questions

| # | risk | mitigation |
|---|---|---|
| R1 | **The laptop cannot reach the broker.** `list_topics` → `_TRANSPORT / -195` despite open raw TCP to both bootstrap and broker hostnames. | The oracle runs in-cluster (§8.1), so this is off the critical path. Two hypotheses to test first, in this order: **(a) TLS/SNI or CA path** — the portal config points at a downloaded `certificate.pem`; on Windows the path or the download may be silently wrong, or the certificate's SAN may not cover `broker-bootstrap-test…`. Probe it natively with `consumer_extra_config={"debug": "security,broker,protocol"}` and read librdkafka's handshake trace. **(b) Advertised listeners** — the bootstrap connects but the broker advertises addresses that only resolve inside the cluster; open TCP to `kafka-0-test…:30095` weakens this but does not kill it. Compare the portal's `bootstrap.servers` against what resolves. Cheap third check: confirm the fetched config actually contains a non-empty `sasl.password` (the checked-out `.env` has it blank; the PAT route is supposed to fill it). |
| R2 | **Oracle-vs-SDK ambiguity.** A FAIL says the two disagree, not which is wrong — and the oracle is new, untested code. | §6.4: write it from §3 and not from `session.py`; do not port the expiry cursors; triage by hand-tracing the single disagreeing key before touching either side. The §7 hand-derived run is the oracle's own acceptance test: run it first, and if the oracle disagrees with §7, the oracle is wrong. |
| R3 | **Multi-partition watermark.** Grouping the oracle globally instead of per partition produces a plausible-looking but wrong expectation for the partition probe — and it would *also* be wrong in a way that looks like an SDK bug. | §5.5 and §6.2: `part_wm` is keyed by partition and the R3 sweep scope is "all keys of partition p". The §7.3 derivation, which splits P0 from P1, is the test: a globally-keyed oracle gets k003's record wrong. |
| R4 | **Deployment quota counts Stopped deployments.** Four phase-1 deployments plus five new ones. | Check the quota before syncing. If it is tight, stop (do not delete) the phase-1 probes — deleting orphans their state volumes and phase 1 would need new names to run again. |
| R5 | **The generator Service restarts mid-run** and replays the whole event set. | `BASE_MS` blank ⇒ a fresh band per process, so a replay does not collide with the first pass; `instance_id` in every payload makes it visible; criterion 2 fails the run. Do **not** pin `BASE_MS` on a Service. |
| R6 | **Future timestamps rejected or overwritten** (topic on `LogAppendTime`, or `message.timestamp.after.max.ms` enforced) — `SPEED=fast` puts events up to ~12 min ahead. | Both default to CreateTime / unbounded, and phase 1's two runs already produced 35 min ahead successfully. Detection: the verdict sees `start` values clustered at wall-clock now with ~0 spans. Fallback: `SPEED=realtime`. |
| R7 | **`KEY_COUNT` too close to `PARTITION_COUNT`** leaves a partition with one key or none, and the run proves less than it looks like it proves. | `KEY_COUNT >= 4 * PARTITION_COUNT`; criterion 3; the verdict prints the observed map and the `PARTITION SHAPE` warning. |
| R8 | **A Service that never stops.** `run()` must idle, not return, or the platform restarts it — see R5. But an idling generator also consumes quota and CPU indefinitely. | §5.7's idle loop plus runbook step 9 (stop it after `RUN COMPLETE`). `RUN_FOREVER=false` is the default. |
| R9 | **Random seed reproducibility.** `SEED` reproduces the *schedule*, not the run: `BASE_MS=now` shifts every absolute timestamp, and `SPEED=realtime` adds jitter. | The oracle needs neither — it reads absolute timestamps off the topic (§6.1). `SEED` is for reproducing *which* gaps were drawn when re-deriving a disagreement offline; pin `BASE_MS` **and** use `SPEED=fast` for that single purpose, accepting R5 for that one run. |
| R10 | **`app.run(timeout=…)` holds every record in memory.** A soak run could be large. | `count=MAX_RECORDS` caps it; the header prints records-read per topic so a truncated read is obvious rather than silent. |
| R11 | **The verdict Job auto-runs on creation** against empty topics and reports FAIL. | Expected; runbook step 4 says to ignore it. Same wrinkle phase 1 had with `PHASE=s7a`. |

**Open questions for the implementer:**

* Does `session-probe`'s `describe()` survive keys of the form `r3-k007` unchanged? It
  splits on the first hyphen, so `run_id="r3"`, `scenario="k007"` — this spec assumes yes
  and forbids editing the probe. Confirm by reading it before writing anything else.
* Should `GAP_MS` differ between the three V2 probes to test that the rules hold at another
  gap? Not in this spec — one gap keeps the paired comparison honest. Worth a fourth probe
  later if a gap-dependent defect is suspected.
* `grace_ms` remains out of scope, for the phase-1 reason: it shifts `late_before` and
  `close_before` by the same constant and never changes which session an event joins. The
  oracle takes `g` as a parameter, so a grace run costs only a fourth probe when it is
  wanted.

---

## 13. References

* `dev-planning/session-windows-live/spec.md` — phase 1: the three rules (§2), the scripted
  table (§5), the derived expectations (§6), the emit-order proof (§7), the collector (§9).
* `dev-planning/session-windows-live/architecture.md` — phase-1 implementation notes; the
  flat record shape from `RunCollector.add_value_and_metadata` (`runtracker.py:24-47`) that
  §8.2 depends on.
* `C:\repos\quix-streams-wt-fix994\quixstreams\dataframe\windows\session.py` — the contract.
* `…\quixstreams\state\rocksdb\windowed\transaction.py:120-155` — `get_latest_timestamp`,
  `advance_partition_timestamp`; `:359-378` — the collection's `(id, counter)` key ordering.
* `…\quixstreams\dataframe\windows\aggregations.py:283-360` — `Earliest` / `Latest` tie rules.
* `…\quixstreams\dataframe\windows\base.py:170` — `.current()` rejects collectors.
* `…\quixstreams\kafka\producer.py:127` — `"partitioner": "murmur2"`.
* `…\quixstreams\app.py:282-326` — `state_dir` resolution and the env-based Quix detection
  that lets `session-verdict/main.py` run from a laptop unchanged.
* `…\quixstreams\platforms\quix\api.py:78` — a PAT is accepted where an SDK token is expected.

---

## 14. Sanity print

`KEY_COUNT=4, PARTITION_COUNT=2, GAP_MODE=fixed, GAP_FIXED_MS=150000,
EVENT_INTERVAL_MS=60000, EVENTS_PER_STREAM=3, STREAMS_PER_KEY=2, GAP_MS=120000,
IDLE_KEY_COUNT=1, KEY_STAGGER_MS=0, GRACE_MS=0, RUN_ID=r3`.
Offsets from `base_ms`. Assumed map `k000→P0, k001→P1, k002→P0, k003→P1`.

**The 24 events, in emit order:**

```
offset 0       P0 r3-k000 seq0   P1 r3-k001 seq0   P0 r3-k002 seq0   P1 r3-k003 seq0
offset 60000   P0 r3-k000 seq1   P1 r3-k001 seq1   P0 r3-k002 seq1   P1 r3-k003 seq1
offset 120000  P0 r3-k000 seq2   P1 r3-k001 seq2   P0 r3-k002 seq2   P1 r3-k003 seq2
                                                              >>> r3-k003 retires (idle key) <<<
                                                              >>> 150 000 ms silence <<<
offset 270000  P0 r3-k000 seq3   P1 r3-k001 seq3   P0 r3-k002 seq3
offset 330000  P0 r3-k000 seq4   P1 r3-k001 seq4   P0 r3-k002 seq4
offset 390000  P0 r3-k000 seq5   P1 r3-k001 seq5   P0 r3-k002 seq5
                                                              >>> all three retire <<<
offset 750000  P0 r3-k000 seq-1  P1 r3-k001 seq-1  P0 r3-k002 seq-1   (closers, 390000 + 3G)
```

**`session-v2-out-key` — exactly 6 records:**

| key | start | end | count | seqs |
|---|---|---|---|---|
| `r3-k000` | 0 | 120001 | 3 | `[0,1,2]` |
| `r3-k000` | 270000 | 390001 | 3 | `[3,4,5]` |
| `r3-k001` | 0 | 120001 | 3 | `[0,1,2]` |
| `r3-k001` | 270000 | 390001 | 3 | `[3,4,5]` |
| `r3-k002` | 0 | 120001 | 3 | `[0,1,2]` |
| `r3-k002` | 270000 | 390001 | 3 | `[3,4,5]` |

and **no `r3-k003` record**, and no record with `seqs == [-1]`.

**`session-v2-out-partition` — exactly 7 records:** the six above, **plus**

| key | start | end | count | seqs |
|---|---|---|---|---|
| `r3-k003` | 0 | 120001 | 3 | `[0,1,2]` |

closed on P1 by `r3-k001`'s event at offset 390000. Emission moments: k000 A and k002 A both
on `k000@390000`; k000 B and k002 B both on `k000@750000`; k001 A and k003 A on
`k001@390000`; k001 B on `k001@750000`.

**`session-v2-out-current` — exactly 24 updates**, `(start, end, count, first_seq, last_seq)`
in this per-key order:

```
r3-k000  (0,1,1,0,0) (0,60001,2,0,1) (0,120001,3,0,2)
         (270000,270001,1,3,3) (270000,330001,2,3,4) (270000,390001,3,3,5)
         (750000,750001,1,-1,-1)
r3-k001  identical
r3-k002  identical
r3-k003  (0,1,1,0,0) (0,60001,2,0,1) (0,120001,3,0,2)
```

**`session-v2-verdict` — 4 records:** `key` PASS 6/6, `partition` PASS 7/7, `current` PASS
24/24, and the summary of §8.3.
