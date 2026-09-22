# Tester verification — session-windows-live phase 2, round 1

**Spec:** `dev-planning/session-windows-live/spec-v2.md`
**Architecture notes:** `dev-planning/session-windows-live/architecture-v2.md`
**Scope:** `session-generator-v2/`, `session-verdict/`, `dev-planning/session-windows-live/quix-v2-blocks.yaml`.
Phase-1 files untouched and not re-tested.
**Python used:** `C:\repos\quix-streams-wt-pr994\.venv\Scripts\python`,
`PYTHONPATH=C:\repos\quix-streams-wt-fix994`.

All 11 checks: **PASS**. No bugs filed this round.

---

## Check 1 — Lint

**Command:**
```
python -m ruff check session-generator-v2/main.py session-verdict/main.py
python -m ruff format --check session-generator-v2/main.py session-verdict/main.py
```

**Result:** PASS
```
All checks passed!
2 files already formatted
```

## Check 2 — Compile

**Command:** `python -m py_compile session-generator-v2/main.py session-verdict/main.py`

**Result:** PASS (exit 0, no output).

## Check 3 — Import smoke, no env set

**Command:** `env -i PATH=$PATH PYTHONPATH=$PYTHONPATH python -c "import main"`, run from inside
each app directory with the environment cleared.

**Result:** PASS for both. Import succeeded with no exception, no environment access, and no
`Application` construction (both modules read the environment only inside `main()`, per
architecture-v2.md's "Generator" section). Only output was an unrelated `AuthlibDeprecationWarning`
from a third-party dependency (`authlib`), not from either module.

## Check 4 — Oracle self-test, and independent spec comparison

**Command:** `cd session-verdict && python main.py --selftest`

**Result:** PASS. Exit 0.
```
SELFTEST PASS: 6 key, 7 partition, 24 current - spec-v2 7.2/7.3/7.4
```

**Independent field-by-field comparison against spec-v2 §7.2/7.3/7.4** (not accepting the
script's own "PASS"):

- **Key probe (6 records):** printed records are exactly `r3-k000`, `r3-k001`, `r3-k002` each
  with `[0,120001) count 3 seqs (0,1,2)` and `[270000,390001) count 3 seqs (3,4,5)`, and no
  `r3-k003` record. Matches §7.2's table exactly, field by field.
- **Partition probe (7 records):** the six above plus `r3-k003 [0,120001) count 3 seqs (0,1,2)`
  — the idle key closed by its partition-mate's traffic. Matches §7.3 exactly, including the
  "extra one" framing from §10 criterion 6.
- **Current probe (24 updates):** `r3-k000`/`k001`/`k002` each print the 7-tuple sequence
  `(0,1,1,0,0) (0,60001,2,0,1) (0,120001,3,0,2) (270000,270001,1,3,3) (270000,330001,2,3,4)
  (270000,390001,3,3,5) (750000,750001,1,-1,-1)`; `r3-k003` prints the first three tuples only.
  Matches §7.4 exactly, in the same per-key order, `3×7+3=24`.

**Verdict:** the self-test's claimed numbers are independently confirmed correct against the
spec, not merely self-consistent.

## Check 5 — Per-partition watermark (most important check)

**Static reading of `expected_sessions` in `session-verdict/main.py`:**
- `key_watermark` is a `dict[(partition, key), int]` — always scoped by `(partition, key)`.
- `partition_watermark` is a `dict[partition, int]` — keyed by `_partition` only, never a
  single global number.
- `scope="key"`: `watermark = max(timestamp, key_watermark.get(scoped, 0))` — never touches
  `partition_watermark`.
- `scope="partition"`: `watermark = max(partition_watermark.get(partition,0), timestamp,
  key_watermark.get(scoped,0))`, written back only to `partition_watermark[partition]`.
- R3 sweep in partition scope: `[scoped_key for scoped_key in sessions if scoped_key[0] ==
  partition]` — sweeps only keys of that partition, never all keys globally.

Confirms by reading: the watermark is correctly per-partition, never global.

**Counter-example constructed and run:**
- Key `A` on partition 0: three events at `t=0,60000,120000` forming session
  `[0,120001) count 3`, then A goes permanently silent (no closer).
- Key `B` on partition 1: ten events stepping `500000` ms apart (`t=0,500000,...,4500000`),
  which drives partition 1's watermark far past any close threshold.
- Ran `expected_sessions(events, gap_ms=120000, grace_ms=0, scope="partition")`.

**Result:** PASS.
```
A's closed sessions (partition 0): []
B's closed sessions (partition 1): [nine of B's own single-event sessions...]
```
A's session was **not** closed by B's traffic on the other partition — a global watermark
would have closed it (B's watermark reaches 4,500,000, far past `A.end + 2G + g = 240001`).
This is the exact scenario risk R3 and §7.3 warn about (`r3-k003` closed by its
partition-mate, never by a different partition), and the oracle gets it right.

## Check 6 — No cursor smuggling

**Command:** `grep -in "cursor|checkpoint|scan_from|iter_prefixes|expire_by_" session-verdict/main.py`

**Result:** PASS. Three hits, all in prose (module docstring and one inline comment)
*explaining the deliberate absence* of the SDK's expiry cursors:
```
11: particular the two expiry cursors (`expire_by_key`'s per-key cursor,
12: `expire_by_partition`'s partition checkpoint) are deliberately **not** modelled: ...
164:  # expiry cursor gates it: every due session closes, every time.
```
No executable code implements a cursor, checkpoint, `scan_from` or `iter_prefixes`. R3 sweeps
unconditionally over every open session in scope on every record, exactly as spec-v2 §3's last
paragraph requires.

## Check 7 — Determinism + RNG independence

Drove `SessionGeneratorV2.run()` directly (env-free), stubbing `_emit` to append
`(key, seq, event_ms, held)` to a list instead of producing, and stubbing `time.sleep` to end
the tail idle loop. `KEY_COUNT=6, STREAMS_PER_KEY=3, SEED=42, OUT_OF_ORDER_PCT=15, LATE_PCT=5`.

**Result:** PASS both parts.
- Same `SEED`+params run twice (injection on): 53 events each run, lists **identical**
  (`(key, seq, event_ms, held)` compared element-wise).
- Same `SEED` with `OUT_OF_ORDER_PCT=0, LATE_PCT=0`: the `(key, seq, event_ms)` multiset (53
  items) is **identical** to the injected run's multiset — injection changes production order
  and the `held` label, never the schedule itself.
- Held kinds actually exercised in the driven schedule: `{'late', 'no', 'ooo'}` — both
  injection code paths fired, not just the no-op path.

## Check 8 — Injection bounds

**From the code (`app.yaml` defaults, `session-generator-v2/main.py:_offer`):**
```
GAP_MS=120000  GRACE_MS=0  bound = GAP_MS + GRACE_MS = 120000
OUT_OF_ORDER_DELAY_MS=90000   (< 120000 → admissible by construction)
LATE_DELAY_MS=400000          (> 120000 → must be dropped)
```
**Result:** PASS. `90000 < 120000` and `400000 > 120000` both hold. Check 7's driven schedule
independently confirms both the `ooo` and `late` code paths actually execute with exactly
these constant delays (delay is a fixed parameter per kind, not computed per event, so the
static inequality is the whole claim).

## Check 9 — YAML

**Command:** `yaml.safe_load` on `quix-v2-blocks.yaml`, `session-generator-v2/app.yaml`,
`session-verdict/app.yaml`, plus structural assertions.

**Result:** PASS, all sub-assertions:
- `safe_load` succeeds; top-level keys are `deployments` and `topics`.
- Every variable name declared in `session-generator-v2/app.yaml` (25 vars) appears in each of
  the generator's deployment blocks, and vice versa (no extras). Same for `session-verdict/app.yaml`
  (11 vars) against the `Session Verdict V2` block.
- No new deployment name collides with `Session Probe Key`, `Session Probe Partition`,
  `Session Probe Current` or `Session Generator` (the five new names all carry a ` V2` suffix).
- `session-v2-in` has `partitions: 2`; `session-v2-out-key`, `session-v2-out-partition`,
  `session-v2-out-current`, `session-v2-verdict` all have `partitions: 1`.
- All three v2 probe deployment blocks (`Session Probe Key V2`, `Session Probe Partition V2`,
  `Session Probe Current V2`) carry exactly `state: {enabled: true, size: 1}`.
- No variable name in either `app.yaml` or any deployment block contains a hyphen.

## Check 10 — Dockerfiles + pin

**Result:** PASS.
- Both `dockerfile`s' first line is exactly `FROM python:3.13-slim-trixie`.
- Both install `git` via `apt-get install -y --no-install-recommends git`.
- Both `requirements.txt` contain exactly one line:
  `quixstreams @ git+https://github.com/quixio/quix-streams.git@74275f72ddd54ea6698c7acb8bff1e5663ec2c75`
  — matches the spec's pinned commit and phase 1's pin; nothing else pinned to a branch or
  `refs/pull`.

## Check 11 — File length note

`session-verdict/main.py`: **563 lines** (matches ArchDev's stated count). Over the ~500-line
soft ceiling. Recorded per instructions; **not treated as a failure**.
`session-generator-v2/main.py`: 397 lines, under the ceiling.

---

## Sanity print

- Checks 1–11: **all PASS**.
- Self-test counts: `6 key, 7 partition, 24 current` — independently confirmed against
  spec-v2 §7.2/7.3/7.4 field by field, not merely accepted from the script's own output.
- Check 5 counter-example: key `A` (partition 0, one session, no closer) vs key `B` (partition
  1, watermark driven to 4,500,000 ms) under `scope="partition"` → A's session **not** closed
  (`[]`), confirming the watermark is per-partition, not global.
- Check 7: determinism PASS (identical `(key,seq,event_ms,held)` lists across two runs of the
  same `SEED`); RNG independence PASS (identical `(key,seq,event_ms)` multiset with injection
  on vs off).
- Check 9 YAML summary: `safe_load` clean; variable-name coverage exact both directions;
  no name collisions; `session-v2-in` partitions=2, four other v2 topics partitions=1; all
  three v2 probes carry `state: {enabled: true, size: 1}`; zero hyphenated variable names.
