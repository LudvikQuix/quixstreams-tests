# Brief: PR1110 `LookupBuffer` — findings from a live deployment

## 1. Goal

Act on three findings from running quix-streams PR #1110 (`feat(lookup): buffer
late-config records instead of blocking the partition`) against a real Quix Cloud pipeline.
Finding 1 is a correctness gap and needs a fix; 2 and 3 need documentation or a knob.

## 2. Phase

**Phase 1: red-first tests, then fix.** Write a failing test per confirmed finding before
changing library code. A finding with no red test is plausible, not confirmed.

## 3. Context / inputs already in place

- **PR head SHA under test:** `6632b46cdb2d493e4facf6c00b78c608ae70af87`, branch
  `feature/sc-72821/qs-update-adding-grace-period-to-lookup`, open against `main`.
- **Test rig:** `C:\repos\quixstreams-tests` (repo `LudvikQuix/quixstreams-tests`, branch
  `dev`), deployed to Quix workspace `testrigorg-quixstreamstests-quixstr-df57e12b`.
  Spec `dev-planning/pr1110-grace-period/spec.md`, build notes
  `dev-planning/pr1110-grace-period/architecture.md`.
- **Shape:** data generator → `sensor-data` → two arms of the *same* `lookup-sink` image
  differing only in `BUFFER_ENABLED` → `enriched-sensor-data` → lakehouse table
  `pr1110_grace_v1`. Configuration is deliberately seeded *after* the data it applies to.
- **Library files that matter:** `quixstreams/dataframe/joins/lookups/buffer.py`,
  `buffer_operator.py`, `buffer_state.py`, `buffer_sweep.py`.

### What already works — do not "fix" these

| Behaviour | Evidence |
|---|---|
| Buffer constructs and runs against a real broker | `grace_ms=45000 on_timeout=emit max_per_key=10000 on_overflow=drop-newest store=lookup-buffer`, state store initialised |
| Recovers late-config records | run r2: identical 15129 rows per arm, buffered carried **1000 more** non-null thresholds (7579 vs 6579) |
| Deadline honoured under traffic | run r3: timeouts fired at **45002–45257 ms** against `grace_ms=45000` |
| Same record, both arms | `device-000/seq 0`: buffered `threshold=10.0` after 14495 ms; control `NULL` at 0 ms |
| Invents nothing when config never arrives | run r1 (no configs at all): both arms 0 thresholds, identical 3468 rows |
| `on_timeout="emit"` is lossless *when drained* | run r1 row counts identical across arms |
| Non-blocking | no partition stalls, no `max.poll.interval.ms` evictions observed |

---

## 4. Findings

### Finding 1 — CORRECTNESS: nothing drains the buffer except an incoming record

**Claim.** Every emission path — release, same-key timeout, and the partition sweep — lives
inside `BufferOperator.__call__`, which the framework invokes only when a message arrives on
that partition. There is no shutdown hook, no checkpoint hook and no timer: grepping the four
buffer files for `on_stop|on_close|close(|drain|shutdown|atexit|__del__` returns nothing, and
the only `flush()` is the state-index write inside the callback.

`grace_ms` is genuinely wall-clock —

```python
now_ms = int(time.time() * 1000)
cutoff = now_ms - self._grace_ms
```

— but those lines are *inside* `__call__`. So the semantics are **wall-clock deadline,
record-driven evaluation**: a record's grace expires on schedule in real time, but nobody
notices until the next record arrives to ask.

**Consequences, in increasing severity:**

1. Stopping the source strands whatever is still buffered.
2. A quiet or low-traffic partition holds expired records indefinitely — `on_timeout="emit"`
   silently does not fire, which reads to an operator as data loss.
3. **A finite stream always loses its tail.** Replay a topic to its end and the last buffered
   records never emit. This is the sharpest case, because the PR's own docstring justifies
   wall-clock over event-time precisely so that *"an event-time deadline would expire a
   backlog replay's records instantly"* — so replay is an intended use case.

**Observed:** run r3 ended with **448 records (~15% of that arm's output) still held** after
the generator stopped — buffered 2465 rows vs control 2913. They are durable in the
changelog-backed store, so not lost from the store, but they never emit until traffic resumes
on that partition.

**Be precise about what was stranded, because it bounds the severity.** A held record is by
definition one that is unresolved *at that moment*, so the stranded set splits in two:

- **Records whose config never arrives.** In r3 these were devices 005–009, and they are what
  the 448 actually were — the seed had already fired, so the configured devices' buffers had
  drained. These would have emitted with field defaults. The damage is therefore **missing
  rows, not missing enrichment**: with `buffer=None` they emit immediately with defaults; with
  the buffer they never emit at all.
- **Records whose config would have arrived later.** Not represented in r3's stranded set,
  but reachable whenever a stream ends before a pending config lands — the replay case in
  consequence 3 below. These lose real enrichment, not just a row.

So the r3 measurement demonstrates the weaker of the two; do not cite it as evidence of lost
enrichment. The row-count deficit is the reproducible symptom, and it is enough: a buffered
pipeline emits fewer rows than an unbuffered one given identical input, which is a difference
`buffer=None` is documented not to make.

**Red-first test to write before any fix.** Feed N records for one key with no matching
config; let `grace_ms` elapse in real wall-clock time; feed nothing further; assert the
records are emitted with their field defaults. RED on current code (nothing is emitted),
green after the fix. Add a second case asserting a *different* idle key on the same partition
is also settled.

**Suggested fix direction** (author's call): drain on checkpoint/commit, where a store
transaction is already open, plus a final drain on application stop. Do not add a background
thread — the PR's whole premise is that the run loop is single-threaded and must not block.

---

### Finding 2 — SCALING: timeout overshoot grows with key count

`BufferSweeper` settles at most `SWEEP_BUDGET = 4` keys and `SWEEP_EMIT_BUDGET = 256` records
per incoming record, so the lag between a deadline and its emission scales with the number of
buffered keys per partition.

| run | devices | `grace_ms` | observed dwell at timeout | overshoot |
|---|---|---|---|---|
| r3 | 10 | 45 000 | 45 002 – 45 257 ms | +2 – 257 ms |
| r1 | 100 | 300 000 | 330 187 – 503 881 ms | **+30 s – +204 s** |

Not a bug — the budget exists to bound per-callback latency — but `grace_ms` reads like a
deadline and behaves like a floor. Two asks: document that timeouts land *at or after*
`grace_ms`, never on it, with the dependence on key count and arrival rate; and consider
whether `SWEEP_BUDGET` should scale with the number of pending keys, or be exposed.

---

### Finding 3 — DOCS: the `valid_from` interaction is a footgun the buffer hides

`grace_ms` is wall-clock, but *which config version applies* is decided by
`Configuration.find_valid_version(timestamp)`, which matches `valid_from <= record
timestamp`. A config created after the data it describes therefore **never** applies to that
data, no matter how long the buffer holds it.

Without buffering this is visible immediately (records emit with defaults). **With buffering
it is invisible:** records buffer, wait the full grace, then time out and emit defaults —
identical in the output to "the config genuinely never arrived". A rig built this way runs
green and proves nothing.

Our seeder works around it by sending `metadata.valid_from` backdated 24 h and asserting the
readback (confirmed: the Quix DCM accepts and stores it). Ask: add a note to the
`LookupBuffer` docstring stating that buffering cannot rescue a record whose configuration is
valid only from *after* that record's timestamp, and pointing at `valid_from`.

---

## 5. Constraints & rules

- Red test first, per finding. Run it, see it RED, then fix.
- No background thread and no sleeping in the record path — that is the regression PR1110
  exists to prevent.
- Do not change the public constructor signature without calling it out; `buffer=None` must
  keep today's behaviour exactly.
- Preserve the documented invariants: within-key arrival order, `start=cutoff + 1` on the
  survivor read, changelog-backed store.

## 6. Outputs

- One failing test per confirmed finding, in the PR's own test layout.
- Fix for Finding 1.
- Docstring changes for Findings 2 and 3.

## 7. Sanity print

For Finding 1's test, print: records fed, wall-clock seconds waited, records emitted, and
records still in the store. The bug is `emitted == 0` with `in_store == N` after the grace
window.

## 8. Report back

1. Which findings reproduced as red tests, and which did not (with why).
2. The fix for Finding 1 and where the drain hooks in.
3. Any invariant the fix threatens — especially within-key ordering and the
   `get_interval()` expiry-floor caveat called out in `buffer_operator.py`'s module
   docstring.
4. Whether `SWEEP_BUDGET` should become a parameter.
