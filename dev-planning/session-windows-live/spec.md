# Session windows on a live Quix deployment

**Status:** Draft
**Project:** quixstreams-tests (`LudvikQuix/quixstreams-tests`, branch `dev`, context `testrig`)
**Created:** 2026-09-22
**Planned with:** Buddy
**Feature under test:** `sdf.session_window()` on quix-streams branch `fix/pr-994`, commit `74275f72`

---

## 1. Goal

Prove, on a real Quix Cloud deployment with a real RocksDB state volume and a real
mid-run restart, that `sdf.session_window()` behaves exactly as its docstring and
`docs/windowing.md` claim — by producing a scripted, arithmetically derived event table
into a one-partition topic and asserting the **exact** `(start, end, count, seqs)` of every
emitted session against a locally-run verdict table.

The rig is falsifiable in both directions: every expected record must appear **exactly
once**, and no record beyond the expected set may appear.

---

## 2. Contract under test

Quoted verbatim from `quixstreams/dataframe/windows/session.py` (the pinned commit):

> A session starts with the first event and extends every time another event arrives
> within `inactivity_gap_ms` of the session's boundaries. For a given message key the
> stored sessions are always **maximal, disjoint and non-adjacent** [...] An out-of-order
> event that falls within one gap of two open sessions **merges** them.
>
> Sessions are stored and emitted half-open, `[start, end)` [...] `end` is the timestamp of
> the last event plus one.
>
> An event is late only when `ts < watermark - gap - grace`, and a session closes once the
> watermark passes `last event + 2 * gap + grace`.

Restated as the three rules every expectation in §6 is derived from, with `G = gap`,
`g = grace`, `W = watermark`:

| # | Rule | Code |
|---|---|---|
| R1 | **Join.** An event joins a stored session `[start, end)` iff `start - G <= ts` **and** `end + G > ts`. Matching two stored sessions merges them into `[min(start, ts), max(end, ts+1))`. | `SessionWindow._matches`, `process_window` lines 115-174 |
| R2 | **Late.** An event is dropped iff `ts < W - G - g`. `W` is the max event timestamp seen **for the key** (`closing_strategy="key"`) or **for the partition** (`"partition"`). | `process_window` lines 88-111 |
| R3 | **Close.** A session closes iff `end <= W - 2G - g`. | `close_before = late_before - gap`, `expire_by_key` |

Two consequences used repeatedly below, both exact:

* `end` is `last_event + 1`, so a session whose last event is at `t` closes only once
  `W >= t + 1 + 2G + g` — **one millisecond later** than a naive `t + 2G + g`. Several
  steps of the emit-order proof in §7 hinge on exactly that 1 ms.
* `.current()` reports `(session_start, session_end)` of the session the event landed in.
  After a merge that is a **different, earlier `start`** than the update before it — the
  supersession the docs describe.

### 2.1 Two library facts that shape the design

1. **`.current()` rejects collectors.** `Window.current()` raises
   `InvalidOperation("BaseCollectors are not supported by 'current' windows")`
   (`windows/base.py:170`). The current-mode probe therefore **cannot** use
   `Collect("seq")`; it uses `Count()` + `Earliest("seq")` + `Latest("seq")` instead.
   This corrects the brief.
2. **`Collect` ordering is by event timestamp.** Values are stored with
   `id=timestamp_ms` and read back in id order, so `seqs` is in **event-time** order, not
   arrival order. That is what makes `[0, 1, 3, 2]` the expected value in S2 and S3 rather
   than `[0, 1, 2, 3]`.

---

## 3. Topology

```
                  Session Generator (Job, 3 runs: PHASE=s7a | s7b | main)
                                    │  Source subclass, scripted event table
                                    ▼
                          session-in   (1 partition)
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
   Session Probe Key        Session Probe Partition   Session Probe Current
   .final("key")            .final("partition")       .current("key")
   state 1 GiB              state 1 GiB               state 1 GiB
   Count + Collect          Count + Collect           Count + Earliest + Latest
              │                       │                       │
              ▼                       ▼                       ▼
     session-out-key        session-out-partition    session-out-current
              └───────────────────────┼───────────────────────┘
                                      ▼
                  tools/collect_results.py  (run locally by the operator)
                        JSONL per probe + verdict table + exit code
```

All three probes are **one application directory** (`session-probe/`) deployed three times;
they differ only by variables. That is what makes the key-vs-partition contrast in S5 a
paired comparison rather than two runs an operator has to trust are comparable.

### 3.1 Topics (all `partitions: 1`)

| name | partitions | retentionInMinutes | why 1 partition |
|---|---|---|---|
| `session-in` | 1 | 1440 | `closing_strategy="partition"` needs one watermark and one total order; the emit-order proof in §7 is only valid on a single partition. |
| `session-out-key` | 1 | 1440 | preserves per-key emission order for the `.current()` assertions |
| `session-out-partition` | 1 | 1440 | " |
| `session-out-current` | 1 | 1440 | " |

Use the **existing repo style** for the `topics:` block (`configuration: {partitions, retentionInMinutes}`).
Do not copy `replicationFactor`/`persisted` from the old scratch harness.

### 3.2 Deployments

Names must be ones that have **never existed in this workspace** (deleting a deployment
orphans its state volume; a name collision would re-attach a stale volume):

| deployment | application | type | resources (limits) | state | desiredStatus |
|---|---|---|---|---|---|
| `Session Probe Key` | `session-probe` | Service | cpu 200 / mem 500 | `{enabled: true, size: 1}` | Running |
| `Session Probe Partition` | `session-probe` | Service | cpu 200 / mem 500 | `{enabled: true, size: 1}` | Running |
| `Session Probe Current` | `session-probe` | Service | cpu 200 / mem 500 | `{enabled: true, size: 1}` | Running |
| `Session Generator` | `session-generator` | Job | cpu 200 / mem 300 | — | (Job) |

`resources:` uses the `limits:` nesting the repo's current `quix.yaml` already uses, **not**
the flat `cpu/memory/replicas` form in the scratch harness.

### 3.3 Variables

`session-probe/app.yaml` declares all of these; each `quix.yaml` deployment block repeats
every one with a concrete `value`. No hyphens in any variable name.

| name | inputType | Key probe | Partition probe | Current probe |
|---|---|---|---|---|
| `input` | InputTopic | `session-in` | `session-in` | `session-in` |
| `output` | OutputTopic | `session-out-key` | `session-out-partition` | `session-out-current` |
| `PROBE` | FreeText | `key` | `partition` | `current` |
| `EMIT_MODE` | FreeText | `final` | `final` | `current` |
| `CLOSING_STRATEGY` | FreeText | `key` | `partition` | `key` |
| `GAP_MS` | FreeText | `120000` | `120000` | `120000` |
| `GRACE_MS` | FreeText | `0` | `0` | `0` |
| `STORE_NAME` | FreeText | `sess` | `sess` | `sess` |
| `CONSUMER_GROUP` | FreeText | `sw_key_r1` | `sw_part_r1` | `sw_curr_r1` |
| `LOGLEVEL` | FreeText | `INFO` | `INFO` | `INFO` |

`session-generator/app.yaml`:

| name | inputType | value | notes |
|---|---|---|---|
| `output` | OutputTopic | `session-in` | |
| `PHASE` | FreeText | `s7a` | `s7a` \| `s7b` \| `main`; initial value must be `s7a` because a newly created Job runs once on creation |
| `BASE_MS` | FreeText | *(operator sets, epoch ms)* | **identical across all three runs**; blank ⇒ the app uses `now` and logs it at WARNING (only valid for a single-phase run) |
| `RUN_ID` | FreeText | `r1` | prefixes every message key; must not contain `-` |

---

## 4. Time model

Two constants, both hard-coded in `session-generator/main.py`:

```
GAP_MS       = 120000      # must equal every probe's GAP_MS
MAIN_BAND_MS = 1200000     # phase `main` lives 20 minutes of event time above phase s7
```

Every row of the event table carries an `offset_ms` **already including its band**, so the
emitted Kafka timestamp is simply `base_ms + offset_ms`. No arithmetic at run time, no way
for the bands to drift apart.

### 4.1 Why `BASE_MS` is an operator-set constant

S7 requires phases `s7a` and `s7b` — two separate Job runs, minutes apart in wall clock —
to share **one event-time origin**, or seq 4 and seq 5 land more than a gap apart and form
two sessions instead of one. A per-run `int(time.time()*1000)` cannot do that. So `BASE_MS`
is set once by the operator and repeated on all three runs.

### 4.2 Why phase `main` sits 20 minutes above phase `s7`

On the partition probe the watermark is shared by every key. Phase `s7b` ends with the s7
closer at `base + 900000`; after it, `W = base + 900000` and `late_before = base + 780000`.
Any `main` event below that would be silently dropped as late. `MAIN_BAND_MS = 1200000`
puts the first `main` event 420 s above the bound — clear by a wide margin, and the band
separation also closes the s7 closer's own session (§6, closer-only records).

### 4.3 Event timestamps are in the future

With `BASE_MS = now`, timestamps run from `now` to `now + 2100000` (35 min ahead). This is
deliberate: future timestamps can never be deleted early by time-based retention. It
assumes the topics use `CreateTime` (the Kafka and Quix default) — see §9.

---

## 5. Event table

`(pos, phase, key, offset_ms, seq)`. **Emit order is table order.** `key` is
`f"{RUN_ID}-{scenario}"`; the `run_id` prefix makes a rerun's keys disjoint from the
previous run's, so a fresh consumer group re-reading the retained input topic cannot
contaminate the result. Message value:
`{"run_id", "scenario", "seq", "offset_ms", "event_ms", "base_ms"}`.

### Phase `s7a` — 5 rows

| pos | key | offset_ms | seq |
|---|---|---|---|
| 1 | `r1-s7` | 0 | 0 |
| 2 | `r1-s7` | 60000 | 1 |
| 3 | `r1-s7` | 120000 | 2 |
| 4 | `r1-s7` | 180000 | 3 |
| 5 | `r1-s7` | 240000 | 4 |

### Phase `s7b` — 6 rows (produced **after** the probe restart)

| pos | key | offset_ms | seq | note |
|---|---|---|---|---|
| 6 | `r1-s7` | 300000 | 5 | |
| 7 | `r1-s7` | 360000 | 6 | |
| 8 | `r1-s7` | 420000 | 7 | |
| 9 | `r1-s7` | 480000 | 8 | |
| 10 | `r1-s7` | 540000 | 9 | |
| 11 | `r1-s7` | 900000 | -1 | closer = `540000 + 3*GAP` |

### Phase `main` — 38 rows (`M = 1200000`)

| pos | key | offset_ms | seq | note |
|---|---|---|---|---|
| 12 | `r1-s1` | 1200000 | 0 | |
| 13 | `r1-s2` | 1200000 | 0 | |
| 14 | `r1-s3` | 1200000 | 0 | |
| 15 | `r1-s4` | 1200000 | 0 | |
| 16 | `r1-s5a` | 1200000 | 0 | |
| 17 | `r1-s5b` | 1200000 | 0 | |
| 18 | `r1-s1` | 1260000 | 1 | |
| 19 | `r1-s2` | 1260000 | 1 | |
| 20 | `r1-s3` | 1260000 | 1 | |
| 21 | `r1-s4` | 1260000 | 1 | |
| 22 | `r1-s5a` | 1260000 | 1 | last s5a event — **no closer, ever** |
| 23 | `r1-s5b` | 1260000 | 1 | |
| 24 | `r1-s1` | 1320000 | 2 | |
| 25 | `r1-s2` | 1320000 | 2 | |
| 26 | `r1-s5b` | 1320000 | 2 | |
| 27 | `r1-s2` | **1290000** | 3 | **out of order** — in-gap join, arrives after pos 25 |
| 28 | `r1-s1` | 1380000 | 3 | |
| 29 | `r1-s5b` | 1380000 | 3 | |
| 30 | `r1-s3` | 1400000 | 2 | opens session B (140 s after seq 1) |
| 31 | `r1-s3` | **1300000** | 3 | **out of order** — bridging merge, arrives after pos 30 |
| 32 | `r1-s1` | 1440000 | 4 | |
| 33 | `r1-s5b` | 1440000 | 4 | |
| 34 | `r1-s1` | 1500000 | 5 | |
| 35 | `r1-s5b` | 1500000 | 5 | |
| 36 | `r1-s1` | 1560000 | 6 | |
| 37 | `r1-s5b` | 1560000 | 6 | |
| 38 | `r1-s1` | 1620000 | 7 | |
| 39 | `r1-s5b` | 1620000 | 7 | |
| 40 | `r1-s4` | 1620000 | -1 | closer = `M + 60000 + 3*GAP` |
| 41 | `r1-s1` | 1680000 | 8 | |
| 42 | `r1-s5b` | 1680000 | 8 | |
| 43 | `r1-s2` | 1680000 | -1 | closer = `M + 120000 + 3*GAP` |
| 44 | `r1-s1` | 1740000 | 9 | |
| 45 | `r1-s5b` | 1740000 | 9 | |
| 46 | `r1-s3` | 1760000 | -1 | closer = `M + 200000 + 3*GAP` |
| 47 | `r1-s1` | 2100000 | -1 | closer = `M + 540000 + 3*GAP` |
| 48 | `r1-s5b` | 2100000 | -1 | closer |
| 49 | `r1-s4` | **1230000** | 2 | **deliberately late** — emitted last of all |

49 rows total. The table is a literal list in `main.py`; `PHASE` selects the slice.

---

## 6. Scenarios and derived expectations

`G = 120000`, `g = 0`, so `late_before = W - 120000` and `close_before = W - 240000`.
All `start`/`end` below are **offsets from `base_ms`**.

### S1 — continuous activity does not fragment (`r1-s1`)

Ten events 60 s apart, i.e. every event is half a gap from the previous. By R1 each one
extends the same session (`end + G = prev + 1 + 120000 > prev + 60000`). The closer at
`M+900000` is `3G` past the last event, so by R1 it does **not** match
(`1740001 + 120000 = 1860001 <= 2100000`) and opens its own session; by R3 it closes the
real one (`1740001 <= 2100000 - 240000 = 1860000`).

* **key / partition:** one session `[M+0, M+540000+1) = [1200000, 1740001)`, count 10,
  seqs `[0,1,2,3,4,5,6,7,8,9]`.
* **current:** 10 updates, all `start = 1200000`, `end` walking
  `1200001 → 1260001 → … → 1740001`, count `1 → 10`.
* **Exercises:** session fragmentation at the `2G` boundary; the `end = last + 1` convention.

### S2 — an in-gap out-of-order event joins, it does not open a second session (`r1-s2`)

Events at `M+0, M+60000, M+120000`, then seq 3 at `M+90000` arriving **after** seq 2.
At seq 3: `W = M+120000` (key) so `late_before = M+0` and `M+90000` is not late (R2).
The stored session is `[M+0, M+120001)`; R1 gives `start - G = M-120000 <= M+90000` and
`end + G = M+240001 > M+90000` → match. The resize is a no-op
(`min(0,90000)=0`, `max(120001, 90001)=120001`) so the count rises to 4 with no new window.

* **key / partition:** one session `[1200000, 1320001)`, count 4, seqs `[0, 1, 3, 2]`
  (event-time order: 0, 60000, **90000**, 120000).
* **current:** `(1200000,1200001,1) (1200000,1260001,2) (1200000,1320001,3) (1200000,1320001,4)`
  — note the 4th update repeats the boundary and only the count moves.
* **Exercises:** the `timestamp_ms >= window_start` guard that used to open a second session
  for an event landing inside an existing one.

### S3 — a bridging event merges two sessions (`r1-s3`)

Events at `M+0`, `M+60000` (session A `[M+0, M+60001)`), then `M+200000`. A does not match:
`end + G = M+180001 <= M+200000` → session B `[M+200000, M+200001)`. Then seq 3 at
`M+100000`:

* not late: `W = M+200000`, `late_before = M+80000`, and `100000 >= 80000` — **margin 20 s**;
* matches A: `M+60001 + 120000 = M+180001 > M+100000`;
* matches B: `M+200000 - 120000 = M+80000 <= M+100000`;
* two matches ⇒ merge to `[min(0,100000), max(200001,100001)) = [M+0, M+200001)`, count
  `2 (A) + 1 (new) + 1 (B) = 4`.

* **key / partition:** one session `[1200000, 1400001)`, count 4, seqs `[0, 1, 3, 2]`.
* **current — this is the supersession evidence:**
  `(1200000,1200001,1) (1200000,1260001,2) (1400000,1400001,1) (1200000,1400001,4)`.
  The third update announces a session starting at `1400000`; the fourth **supersedes** it
  with one starting at `1200000`. Diagnostics: the merged record must carry
  `first_seq = 0`, `last_seq = 2`.
* **Exercises:** `_matches` on both neighbours, `delete_window` of both keys, aggregator
  `merge()`, and the documented `.current()` supersession. Absorbs the brief's S6.

### S4 — a genuinely late event is dropped and never resurrects a closed session (`r1-s4`)

Events at `M+0`, `M+60000`; closer at `M+420000`; then seq 2 at `M+30000` **last of all**.
At the closer: `W = M+420000`, `close_before = M+180000 >= M+60001` → the session closes.
At seq 2: `late_before = M+300000` and `M+30000 < M+300000` → late by 270000 ms → `on_late`
fires with `start = M+30000, end = M+30001`, and `process_window` returns `[], []`.

* **key / partition:** exactly one session `[1200000, 1260001)`, count 2, seqs `[0, 1]`.
* **current:** `(1200000,1200001,1) (1200000,1260001,2)` and nothing for seq 2.
* **On every probe:** exactly one `on_late` log line for `r1-s4`, and **no record anywhere**
  with `start = 1230000`.
* **Note:** on the partition probe the session closes earlier than the closer — at pos 36,
  `W = M+360000`, `close_before = M+120000 >= M+60001` (see §7). Same record, earlier moment.
* **Exercises:** closed sessions being re-created by a late event; `on_late` plumbing.

### S5 — `closing_strategy` decides whether an idle key ever emits (`r1-s5a`, `r1-s5b`)

`s5a`: two events, **no closer**, no further traffic on that key.
`s5b`: identical to S1, with a closer.

* **key probe:** `s5b` emits `[1200000, 1740001)` count 10; `s5a` **never emits** — its own
  watermark stops at `M+60000`, so `close_before = M-180000` and nothing is ever due.
* **partition probe:** `s5a` **does** emit `[1200000, 1260001)` count 2, seqs `[0, 1]`,
  closed at pos 36 by `s1`/`s5b` traffic (`W = M+360000`, `close_before = M+120000 >= 60001`).
* **current probe:** `s5a` produces its two updates like any other key (`.current()` does
  not wait for closing).
* **This contrast — one record present on one topic and absent on the other, from the same
  input — is the whole of S5.**

### S6 — folded into S3

### S7 — a session survives a process restart (`r1-s7`)

Phase `s7a` sends seq 0-4 (`0 … 240000`). The operator then **stops and restarts the
probes** — state volume open + changelog recovery. Phase `s7b` sends seq 5-9
(`300000 … 540000`) and the closer at `900000`.

Seq 5 is 60 s after seq 4, i.e. inside one gap of the recovered session `[0, 240001)`, so
by R1 it extends it — **if and only if** the session is still in RocksDB after the restart.

* **key / partition:** one session `[0, 540001)`, count 10, seqs `[0..9]`, **emitted exactly
  once**. A fragmented result (e.g. `[0,240001)` count 5 plus `[300000,540001)` count 5) is
  the failure signature.
* **current:** 5 updates before the restart (`(0,1,1) … (0,240001,5)`) and 5 after
  (`(0,300001,6) … (0,540001,10)`), with `start = 0` throughout. A post-restart update with
  `start = 300000` is the failure signature.
* On the **partition** probe the s7 closer's own session `[900000, 900001)` is later closed
  by phase `main`'s first event (`W = 1200000`, `close_before = 960000 >= 900001`) and
  appears as a closer-only record; the key probe never emits it.
* **Caveat to record in the README:** S7 proves *state survives a restart*, not *state lived
  on the volume* — an ephemeral store rebuilt from the changelog would also pass. The
  volume is verified separately, by the startup log (§8.1).

### Grace — deliberately out of scope

`grace_ms` is a window-definition parameter, not a per-key one, so testing it needs a fourth
probe with `GRACE_MS > 0`. It only shifts both `late_before` and `close_before` by the same
constant and never changes which session an event joins, so a live rig adds nothing the unit
tests do not already cover. **Left to the unit tests.**

### 6.1 Complete expected record sets

**`session-out-key`** — exactly 6 records, no closer-only records (every key's last message
*is* its closer, so no key's watermark ever passes its closer session's `end + 2G`):

| key | start | end | count | seqs |
|---|---|---|---|---|
| `r1-s1` | 1200000 | 1740001 | 10 | `[0,1,2,3,4,5,6,7,8,9]` |
| `r1-s2` | 1200000 | 1320001 | 4 | `[0,1,3,2]` |
| `r1-s3` | 1200000 | 1400001 | 4 | `[0,1,3,2]` |
| `r1-s4` | 1200000 | 1260001 | 2 | `[0,1]` |
| `r1-s5b` | 1200000 | 1740001 | 10 | `[0,1,2,3,4,5,6,7,8,9]` |
| `r1-s7` | 0 | 540001 | 10 | `[0,1,2,3,4,5,6,7,8,9]` |

and **no `r1-s5a` record**.

**`session-out-partition`** — the same 6, **plus** `r1-s5a` `[1200000, 1260001)` count 2
seqs `[0,1]`, plus exactly 4 closer-only records (count 1, seqs `[-1]`), which the collector
drops: `r1-s7 [900000,900001)`, `r1-s4 [1620000,1620001)`, `r1-s2 [1680000,1680001)`,
`r1-s3 [1760000,1760001)`. The `r1-s1` and `r1-s5b` closer sessions stay open — nothing
follows them.

**`session-out-current`** — 42 real updates (one per non-late event) plus 6 closer-only
updates, in this per-key order:

| key | ordered `(start, end, count)` |
|---|---|
| `r1-s7` | `(0,1,1) (0,60001,2) (0,120001,3) (0,180001,4) (0,240001,5)` ‖ restart ‖ `(0,300001,6) (0,360001,7) (0,420001,8) (0,480001,9) (0,540001,10)` |
| `r1-s1` | `(1200000,1200001,1) (…,1260001,2) (…,1320001,3) (…,1380001,4) (…,1440001,5) (…,1500001,6) (…,1560001,7) (…,1620001,8) (…,1680001,9) (…,1740001,10)` |
| `r1-s5b` | identical to `r1-s1` |
| `r1-s2` | `(1200000,1200001,1) (1200000,1260001,2) (1200000,1320001,3) (1200000,1320001,4)` |
| `r1-s3` | `(1200000,1200001,1) (1200000,1260001,2) (1400000,1400001,1) (1200000,1400001,4)` |
| `r1-s4` | `(1200000,1200001,1) (1200000,1260001,2)` |
| `r1-s5a` | `(1200000,1200001,1) (1200000,1260001,2)` |

Closer-only updates (dropped by the collector): `r1-s7 (900000,900001,1)`,
`r1-s4 (1620000,1620001,1)`, `r1-s2 (1680000,1680001,1)`, `r1-s3 (1760000,1760001,1)`,
`r1-s1 (2100000,2100001,1)`, `r1-s5b (2100000,2100001,1)`.

---

## 7. Emit-order proof

The order in §5 is not cosmetic. On the partition probe **one** watermark governs every key,
so a row emitted too early is dropped as late and a row emitted too late closes another
scenario's session prematurely. The rule the table encodes: **ascending `offset_ms`, with
each deliberate out-of-order row placed immediately after the row that raises the watermark
above it.** Proof, checking R2 for every row that is not trivially at the current maximum,
and R3 at every watermark step (phase `main`; `W` and `close_before` are offsets from base):

**Lateness (R2) — the only rows not at the running maximum:**

| pos | key | ts | `W` when emitted | `late_before = W - 120000` | late? |
|---|---|---|---|---|---|
| 27 | `r1-s2` | 1290000 | 1320000 | 1200000 | no — margin 90000 |
| 31 | `r1-s3` | 1300000 | 1400000 | 1280000 | no — **margin 20000** |
| 40 | `r1-s4` | 1620000 | 1620000 | 1500000 | no |
| 43 | `r1-s2` | 1680000 | 1680000 | 1560000 | no |
| 46 | `r1-s3` | 1760000 | 1760000 | 1640000 | no |
| 49 | `r1-s4` | 1230000 | 2100000 | 1980000 | **YES — by design, late by 750000 ms** |

Position 31 is the tightest row in the rig: it must be emitted after pos 30 (which creates
session B) and before pos 32 (which would raise `late_before` to 1320000 and drop it).

**Closing (R3) at each watermark step of phase `main`:**

| after pos | `W` | `close_before` | sessions with `end <= close_before` | emitted |
|---|---|---|---|---|
| 17 | 1200000 | 960000 | `r1-s7` closer `[900000,900001)` | closer-only |
| 23 | 1260000 | 1020000 | — | |
| 26 | 1320000 | 1080000 | — | |
| 29 | 1380000 | 1140000 | — | |
| 30 | 1400000 | 1160000 | — | |
| 33 | 1440000 | 1200000 | none — every `end` is `>= 1200001` (**1 ms margin**) | |
| 35 | 1500000 | 1260000 | none — `s4`/`s5a` end at 1260001 (**1 ms margin**) | |
| 37 | 1560000 | 1320000 | `r1-s4 [.,1260001)`, `r1-s5a [.,1260001)` | **s4, s5a** |
| 39 | 1620000 | 1380000 | `r1-s2 [.,1320001)` | **s2** |
| 42 | 1680000 | 1440000 | `r1-s3 [.,1400001)` | **s3** |
| 45 | 1740000 | 1500000 | — | |
| 46 | 1760000 | 1520000 | — | |
| 47 | 2100000 | 1860000 | `r1-s1 [.,1740001)`, `r1-s5b [.,1740001)`, and the `s4`/`s2`/`s3` closer sessions | **s1, s5b + 3 closer-only** |

The two 1 ms margins (rows `after pos 33` and `after pos 35`) are the half-open-interval
semantics under test: a session whose last event is at `t` must **not** close at
`W = t + 2G`, only at `W = t + 2G + 1`. If the implementation ever used `end < close_before`
or `last_event + 2G`, the s4/s5a records would appear one watermark step early and the rig
would catch it.

The partition sweep gate (`expire_by_partition`: skip while `watermark < checkpoint`) does
not suppress any of the above — the checkpoint is recomputed after each sweep as
`min(first_open.end) + 2G + g` over all prefixes, which is 1 ms below the next due
watermark by construction:
after pos 33 it is `1260001 + 240000 = 1500001` (so pos 35 at `W = 1500000` correctly does
not sweep, and nothing was due); after pos 37 it is `1320001 + 240000 = 1560001`; after
pos 39, `1400001 + 240000 = 1640001`; after pos 42, `1740001 + 240000 = 1980001`.

**Key probe:** per-key order in the table is exactly the per-scenario order analysed in §6,
and each key's watermark is independent, so interleaving changes nothing there.

---

## 8. Applications

Both apps: `dockerfile` first line `FROM python:3.13-slim-trixie`, `apt-get install -y
--no-install-recommends git` retained (the requirement is a git URL), requirements copied
before the source so the pip layer survives a code edit, `ENTRYPOINT ["python3", "main.py"]`.
One `main.py` per app, no helper modules.

`requirements.txt` for both:

```
quixstreams @ git+https://github.com/quixio/quix-streams.git@<40-char SHA>
```

ArchDev resolves the SHA with `git -C C:\repos\quix-streams-wt-fix994 rev-parse 74275f72`
and **must confirm it is reachable from the public remote** before committing
(`git ls-remote https://github.com/quixio/quix-streams.git refs/heads/fix/pr-994` — if the
returned head differs, check the SHA is an ancestor of it; pip cannot fetch an unpushed
commit and the build fails minutes later with an opaque error).

### 8.1 `session-probe/main.py`

* `Application(consumer_group=os.environ["CONSUMER_GROUP"], auto_offset_reset="earliest")`.
  **Do not pass `state_dir`.** `Application.__init__` (app.py:282-286) only consults
  `Quix__Deployment__State__Path` when `state_dir is None`; passing anything — including the
  old harness's `os.environ.get("Quix__State__Dir", "state")` — overrides the platform
  volume and silently lands the store on ephemeral container disk.
* Startup log, in this order (this block **is** the volume evidence):
  `quixstreams version` (`importlib.metadata.version("quixstreams")`), `app.config.state_dir`,
  `os.environ.get("Quix__Deployment__State__Path")`,
  `os.environ.get("Quix__Deployment__State__Enabled")`, then
  `PROBE / EMIT_MODE / CLOSING_STRATEGY / GAP_MS / GRACE_MS / STORE_NAME / CONSUMER_GROUP`,
  and the resolved `input_topic.name` and `output_topic.name` (they must carry the workspace
  prefix).
* Topics: `app.topic(os.environ["input"], value_deserializer="json", key_deserializer="str")`
  and `app.topic(os.environ["output"], value_serializer="json", key_serializer="str")`.
* Window: `sdf.session_window(inactivity_gap_ms=GAP_MS, grace_ms=GRACE_MS, name=STORE_NAME,
  on_late=on_late)`.
* Aggregations — **conditional on `EMIT_MODE`, because `.current()` raises on collectors**:
  * `final` → `.agg(count=Count(), seqs=Collect("seq"))`
  * `current` → `.agg(count=Count(), first_seq=Earliest("seq"), last_seq=Latest("seq"))`
* Emission: `.final(closing_strategy=CLOSING_STRATEGY)` or
  `.current(closing_strategy=CLOSING_STRATEGY)`.
* `on_late(value, key, timestamp_ms, late_by_ms, start, end, store_name, topic, partition,
  offset)` — the 10-positional-argument `WindowOnLateCallback` protocol. Logs
  `LATE key=… ts=… late_by=… would_be=[start,end)` and returns `True` so the library's own
  warning is emitted too.
* `sdf.apply(describe, metadata=True)` — `describe(value, key, timestamp, headers)` adds:
  `key` (the full message key), `run_id` and `scenario` (`key.partition("-")`), `probe`,
  `emit_mode`, `closing_strategy`, `span_seconds = round((end - start) / 1000, 1)`.
* `sdf.print(metadata=True)` then `sdf.to_topic(output_topic)`.

### 8.2 `session-generator/main.py`

* A `quixstreams.sources.Source` subclass registered with
  `app.add_source(source, topic=app.topic(os.environ["output"], value_serializer="json",
  key_serializer="str"))`, then `app.run()` — **not** a bare `app.get_producer()` loop.
  Two reasons: (a) the Quix workspace prefix is applied to a topic name during
  `app.run()`'s topic setup, so a producer-only script would produce to an unprefixed,
  wrong topic; (b) `Application._run_sources` loops
  `while run_tracker.running and source_manager.is_alive()`, so when the source's `run()`
  returns, `app.run()` returns and the Job exits 0 — exactly the Job semantics needed.
  This is the pattern already proven in this repo by `data-generator/main.py`.
* The event table is a module-level list of tuples in table order. `PHASE` selects the slice.
* `base_ms = int(BASE_MS)` if `BASE_MS` is non-blank, else `int(time.time() * 1000)` logged
  at WARNING with "set BASE_MS to this value on the remaining phases".
* Per row: `message = self.serialize(key=key, value=payload, timestamp_ms=base_ms + offset_ms)`
  then `self.produce(key=…, value=…, headers=…, timestamp=message.timestamp)`, then
  **`self.flush()`**. Flushing every message costs ~49 round trips and removes any chance
  that a librdkafka retry reorders the stream — emit order is this rig's independent
  variable, so it is not left to default producer settings.
* Log one line per row (`pos, phase, key, seq, offset_ms, event_ms`) and a final
  `phase=<PHASE> rows=<n> base_ms=<n> topic=<resolved name>` summary.

---

## 9. Collector — `tools/collect_results.py`

Run locally by the operator; the only place any assertion lives.

**Connection.** `load_dotenv(<repo root>/.env)` then

```python
app = Application(
    quix_sdk_token=os.environ["Quix__Pat__Token"],
    quix_portal_api=os.environ["Quix__Portal__Api"],
    consumer_group=f"session-collect-{RUN_ID}-{int(time.time())}",
    auto_offset_reset="earliest",
)
```

A PAT is explicitly accepted where an SDK token is expected (`platforms/quix/api.py:78`:
*"A Quix Cloud auth token (SDK or PAT) is required"*), and the workspace comes from
`Quix__Workspace__Id`. This route makes the app a **Quix app**, so topic names and the
consumer group are workspace-prefixed automatically and the broker credentials (including
the SASL password, which is **blank** in the checked-out `.env`) are fetched from the Portal.
Do not hand-build a `ConnectionConfig` from `Quix__Broker__*` unless the PAT route fails —
if it does, note that `SecurityMode=SaslSsl` must be mapped to `security_protocol="sasl_ssl"`
and `SaslMechanism=ScramSha512` to `sasl_mechanism="SCRAM-SHA-512"` (the pydantic validators
only change case, they do not split the CamelCase), the password must be filled in, **and
every topic name must be prefixed with `Quix__Workspace__Id` by hand**.

**Read.** One `app.dataframe(app.topic(name, value_deserializer="json",
key_deserializer="str"))` per output topic (no operations), then

```python
records = app.run(timeout=30, metadata=True)
```

`timeout` is an **idle** timeout, and `RunTracker.set_as_running` gives it a 60 s head start
(`runtracker.py:166`), so expect the collector to take ~90 s and print nothing until it
returns.

**Process.**

1. Group by `value["probe"]` (`key` / `partition` / `current`).
2. Write raw JSONL, one file per probe, to
   `dev-planning/session-windows-live/results/<run-id>/<probe>.jsonl` (gitignored).
3. Drop **other-run** records: `value["run_id"] != RUN_ID` — count and report them as
   "ignored (previous run)".
4. Drop **closer-only** records: final probes `value["seqs"] == [-1]`; current probe
   `value["count"] == 1 and value["last_seq"] == -1`. Count and report them.
5. Derive `base_ms`: the `start` of the `s7` record on the `key` probe. Fallback: the `s1`
   record's `start - 1200000`. Neither present ⇒ hard FAIL with that message.
6. Normalise every record to `(scenario, start - base_ms, end - base_ms, count, seqs)`.
7. Compare against one `EXPECTED` dict in the same file, holding §6.1 verbatim:
   `{"key": {"s1": [(1200000, 1740001, 10, [0,...,9])], ...}, "partition": {...},
   "current": {"s1": [(1200000, 1200001, 1), ...], ...}}`
   — final probes compare `(start, end, count, seqs)` as a multiset per scenario; the current
   probe compares the **ordered list** of `(start, end, count)` per scenario.

**Output.** A verdict table `scenario × probe → expected / observed / PASS|FAIL`, then three
lists: MISSING, UNEXPECTED, DUPLICATE. Then the counts of ignored closer-only and other-run
records. Exit code 1 if any line is FAIL or any UNEXPECTED/DUPLICATE record remains.

---

## 10. Runbook

Prerequisites: `quix use testrig`; the three probes and the generator exist only after the
first sync; nothing below reads a container log except steps 5 and 9.

1. **Pick `BASE_MS`.** `python -c "import time; print(int(time.time()*1000))"`. Put it in the
   `Session Generator` block in `quix.yaml`. Keep it identical for all three phases.
   On a **rerun**, take a fresh value — it must be at least `2100000` ms (35 min) above the
   previous run's, which a natural rerun cadence satisfies; together with the `RUN_ID` key
   prefix this is what makes a re-read of the retained input topic harmless.
2. **Commit and push `dev`.** Quix builds from the pushed branch; `quix.yaml` must be in the
   same commit as the app directories, or the sync is rejected with "Reference should have
   affected the workspace descriptor".
3. **Sync.** `POST /workspaces/{ws}/pull`, then `quix cloud environments sync <ws>` (or
   Portal → Pipeline → Sync). Without the pull the sync compares the old commit to itself.
4. **Wait for three probes Running and the `Session Generator` Job Completed.** The Job
   auto-runs on creation with `PHASE=s7a`. The probes do **not** need to be up first:
   `auto_offset_reset="earliest"` with a fresh consumer group reads the topic from the
   beginning regardless.
5. **Verify the state volume** in each probe's log (`quix cloud deployments logs <guid>`):
   `state_dir` must equal `Quix__Deployment__State__Path` and
   `Quix__Deployment__State__Enabled` must be `true`. If `state_dir` is `state` or
   `/app/state` while the platform path is something else, **stop here** — the rig would
   still pass S7 via changelog recovery and prove nothing about the volume.
6. **Restart.** Wait ~20 s after the Job completes so the probes' checkpoints commit
   (default `commit_interval` 5 s). Stop all three probes; **poll until each reports
   Stopped** (SIGTERM shutdown can take a minute or more); start all three; poll until
   Running and each log shows its state partitions opened / recovered.
7. **Phase `s7b`.** The generator is Completed, so a PATCH is accepted (a PATCH against a
   Running deployment is silently ignored). PATCH `PHASE=s7b`, start it, wait for Completed.
8. **Phase `main`.** PATCH `PHASE=main`, start it, wait for Completed.
9. **Collect.** Wait ~30 s, then `python tools/collect_results.py --run-id r1`. Read the
   verdict table; `echo $LASTEXITCODE` is the machine-readable answer. For the S4 evidence
   also grep each probe's log for `LATE key=r1-s4`.

**Reset for a rerun:** bump `RUN_ID`, all three `CONSUMER_GROUP`s and `BASE_MS` together,
then repeat from step 2. A new consumer group gives each probe a fresh state directory
(`StateStoreManager` roots the store at `state_dir / consumer_group`) and a fresh changelog;
the new `RUN_ID` keeps the replayed old-run keys disjoint; the new `BASE_MS` keeps the
partition watermark monotonic across the replay. A hard reset (delete and recreate the four
topics) is only needed if a phase was accidentally run twice within one `RUN_ID`.

---

## 11. Repo change list

**Delete** (PR #1110 rig, finished; its history stays in `dev-planning/pr1110-grace-period/`):

```
config-seeder/      data-generator/    lake-sink/    lookup-sink/    mongodb/
tools/negative-tests/
```

**Modify:**

* `quix.yaml` — remove all seven PR #1110 deployment blocks (`MongoDB`,
  `Dynamic Configuration Manager`, `Data Generator`, `Config Seeder`,
  `Lookup Sink - Buffered`, `Lookup Sink - Control`, `Lake Sink`) and all three topic blocks
  (`sensor-data`, `config-updates`, `enriched-sensor-data`); add the four deployments of
  §3.2 and the four topics of §3.1.
* `README.md` — rewrite: what the rig proves, the pinned SHA, the scenario table from §12,
  the pipeline diagram from §3, the runbook from §10, how to read the verdict, and the S7
  caveat from §6.
* `.gitignore` — add `dev-planning/session-windows-live/results/`.

**Add:**

```
session-generator/{main.py, app.yaml, dockerfile, requirements.txt}
session-probe/{main.py, app.yaml, dockerfile, requirements.txt}
tools/collect_results.py
```

**Keep untouched:** `dev-planning/pr1110-grace-period/`, the root `.env` (untracked),
`.env.example`.

---

## 12. Acceptance criteria

"The branch works on a real deployment" means all seven of these:

1. `session-out-key` contains **exactly** the 6 records of §6.1, each **exactly once**, with
   exact `start`, `end`, `count` and `seqs` — and **no** `r1-s5a` record and no other record.
2. `session-out-partition` contains exactly those 6 plus `r1-s5a [1200000, 1260001)` count 2,
   each exactly once, plus exactly the 4 listed closer-only records and nothing else.
3. `session-out-current` contains exactly the 42 real updates of §6.1 **in the per-key order
   given** (in particular `r1-s3`'s `start=1400000` update immediately followed by its
   `start=1200000, end=1400001` supersession), plus the 6 closer-only updates.
4. The `r1-s7` record on the key probe is `[0, 540001)` count 10 seqs `[0..9]`, emitted
   **exactly once**, after the probe was stopped and restarted between seq 4 and seq 5.
5. `on_late` fires **exactly once per probe**, for `r1-s4` at `ts = base + 1230000`, and no
   record on any topic has `start = base + 1230000`.
6. Every probe logs a `state_dir` equal to its `Quix__Deployment__State__Path`, with
   `Quix__Deployment__State__Enabled=true`.
7. `tools/collect_results.py` exits 0.

Any FAIL is a finding against `fix/pr-994`, to be reproduced as a red unit test before any
code changes.

---

## 13. Risks and open questions

| # | Risk | Mitigation |
|---|---|---|
| R1 | **`state_dir` trap.** Passing `state_dir` at all defeats the platform volume. | §8.1: never pass it; assert the resolved path in the startup log (runbook step 5). |
| R2 | **Producer-only script produces to an unprefixed topic.** Workspace prefixing happens during `app.run()` topic setup, not at `app.topic()`. | §8.2: use a `Source` + `app.run()`. The generator logs the resolved topic name; it must contain the workspace id. |
| R3 | **`.current()` + `Collect` raises at build time.** | §8.1: mode-conditional aggregations. If ArchDev copies the scratch harness verbatim, the Current probe crashes on startup — that is the first thing to check if it will not start. |
| R4 | **`BASE_MS` differing between phases** silently splits the S7 session in two. | Operator-set constant; every probe record's `start` is absolute, so a mismatch shows up as an unexpected `r1-s7` pair rather than a silent pass. |
| R5 | **Future timestamps rejected or overwritten.** If the topic is `LogAppendTime`, or the broker enforces `message.timestamp.after.max.ms`, the whole event-time script is destroyed. | Both default to CreateTime / unbounded. Detection: the collector sees `start` values clustered at wall-clock now with tiny spans. Fallback: set `BASE_MS` to `now - 2100000`, which moves the whole band into the past (retention then applies — raise `retentionInMinutes`). |
| R6 | **Pinned SHA not on the public remote** ⇒ every image build fails at `pip install`. | §8: verify with `git ls-remote` before committing. |
| R7 | **A phase run twice** duplicates its rows and corrupts every count on that key. | Bump `RUN_ID` + consumer groups + `BASE_MS` and rerun; the runbook says so. |
| R8 | **Deployment quota counts Stopped deployments**; **deleting a deployment orphans its volume**. | The wipe in §11 frees the quota; §3.2 uses four names that never existed in this workspace. |
| R9 | **No REST endpoint for runtime logs.** | All evidence except `on_late` and the state-dir line is on output topics and read by the collector. `quix cloud deployments logs <guid>` is the secondary path for those two. |
| R10 | **20 s margin on pos 31** (§7) is the only expectation that could flip from an emit-order slip. | The generator flushes after every message and logs each row's position, so the produced order is auditable after the fact. |

**Open questions for the implementer:**

* Does the `testrig` workspace's default topic configuration use `CreateTime`? (R5. Confirm
  in the Portal before the first run; it costs 30 seconds and invalidates everything if wrong.)
* Does `quix cloud deployments logs <guid>` work on this cluster? If not, criteria 5 and 6
  need the Portal log viewer instead, and the runbook should say so.
* Should a fourth probe with `GRACE_MS=300000` be added later? Only if a grace-related defect
  is suspected; §6 argues it adds nothing today.

---

## 14. References

* `C:\repos\quix-streams-wt-fix994\quixstreams\dataframe\windows\session.py` — the contract.
* `C:\repos\quix-streams-wt-fix994\docs\windowing.md` §"Session Windows" — the user-facing claims.
* `C:\repos\quix-streams-wt-fix994\quixstreams\app.py:282-286` — `state_dir` resolution.
* `C:\repos\quix-streams-wt-fix994\quixstreams\dataframe\windows\base.py:170` — `.current()` rejects collectors.
* `C:\repos\quix-streams-wt-fix994\quixstreams\platforms\quix\api.py:78` — PAT accepted as an SDK token.
* `C:\repos\quix-streams_review\dev-planning\session-window-live-test\` — the scratch harness this supersedes (wrong pin, wrong base image, `state_dir` trap, `Collect` in current mode).
* `C:\repos\quixstreams-tests\dev-planning\pr1110-grace-period\` — the previous rig in this repo; house style for `quix.yaml`, `app.yaml` and dockerfiles.

---

## 15. Sanity print

**Expected sessions** — all times are offsets from `base_ms`; `M = 1200000`.

| scenario | key | probe | expected `(start, end, count)` |
|---|---|---|---|
| S1 | `r1-s1` | key / partition | `(1200000, 1740001, 10)` |
| | | current | 10 updates, `start=1200000`, `end` 1200001→1740001, count 1→10 |
| S2 | `r1-s2` | key / partition | `(1200000, 1320001, 4)` seqs `[0,1,3,2]` |
| | | current | `(1200000,1200001,1) (…,1260001,2) (…,1320001,3) (…,1320001,4)` |
| S3 | `r1-s3` | key / partition | `(1200000, 1400001, 4)` seqs `[0,1,3,2]` |
| | | current | `(1200000,1200001,1) (…,1260001,2) (1400000,1400001,1) (1200000,1400001,4)` |
| S4 | `r1-s4` | key / partition | `(1200000, 1260001, 2)` + one `on_late`, no record at 1230000 |
| | | current | `(1200000,1200001,1) (1200000,1260001,2)` |
| S5 | `r1-s5a` | key | **no record** |
| | `r1-s5a` | partition | `(1200000, 1260001, 2)` |
| | `r1-s5a` | current | `(1200000,1200001,1) (1200000,1260001,2)` |
| | `r1-s5b` | key / partition | `(1200000, 1740001, 10)` |
| S7 | `r1-s7` | key / partition | `(0, 540001, 10)` — once, across a restart |
| | | current | 10 updates, `start=0`, 5 before and 5 after the restart |

**Emit order** — `(pos, key, seq, offset_ms)`, table order = emit order:

```
s7a: (1,s7,0,0) (2,s7,1,60000) (3,s7,2,120000) (4,s7,3,180000) (5,s7,4,240000)
        >>> STOP AND RESTART ALL THREE PROBES HERE <<<
s7b: (6,s7,5,300000) (7,s7,6,360000) (8,s7,7,420000) (9,s7,8,480000)
     (10,s7,9,540000) (11,s7,-1,900000)
main:(12,s1,0,1200000) (13,s2,0,1200000) (14,s3,0,1200000) (15,s4,0,1200000)
     (16,s5a,0,1200000) (17,s5b,0,1200000)
     (18,s1,1,1260000) (19,s2,1,1260000) (20,s3,1,1260000) (21,s4,1,1260000)
     (22,s5a,1,1260000) (23,s5b,1,1260000)
     (24,s1,2,1320000) (25,s2,2,1320000) (26,s5b,2,1320000)
     (27,s2,3,1290000)      <- out of order, in-gap join
     (28,s1,3,1380000) (29,s5b,3,1380000)
     (30,s3,2,1400000)
     (31,s3,3,1300000)      <- out of order, bridging merge  [20 s margin]
     (32,s1,4,1440000) (33,s5b,4,1440000)
     (34,s1,5,1500000) (35,s5b,5,1500000)
     (36,s1,6,1560000) (37,s5b,6,1560000)
     (38,s1,7,1620000) (39,s5b,7,1620000) (40,s4,-1,1620000)
     (41,s1,8,1680000) (42,s5b,8,1680000) (43,s2,-1,1680000)
     (44,s1,9,1740000) (45,s5b,9,1740000)
     (46,s3,-1,1760000)
     (47,s1,-1,2100000) (48,s5b,-1,2100000)
     (49,s4,2,1230000)      <- deliberately late, last of all
```
