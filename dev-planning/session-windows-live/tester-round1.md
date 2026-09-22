# Tester round 1 — session-windows-live verification gate

**Spec:** C:\repos\quixstreams-tests\dev-planning\session-windows-live\spec.md
**Architecture notes:** C:\repos\quixstreams-tests\dev-planning\session-windows-live\architecture.md
**Scope:** static/lint verification only. No deployment, no Portal/broker calls except
`git ls-remote`. Repo: C:\repos\quixstreams-tests, branch `dev`, uncommitted working tree.

## Check 1 — Lint (ruff check + ruff format --check)

**PASS.**

```
python -m ruff check session-generator/main.py session-probe/main.py tools/collect_results.py
All checks passed!

python -m ruff format --check <same three files>
3 files already formatted
```

Note: `ruff` was absent from `C:\repos\quix-streams-wt-pr994\.venv` as the brief warned;
installed `ruff==0.16.8` into that venv only (no pinned lint config exists in this repo, per
the brief).

## Check 2 — Generator table (no env vars set)

**PASS.**

```
len(EVENTS) = 49
s7a = 5, s7b = 6, main = 38
```

Row tuple layout is `(pos, phase, scenario, offset_ms, seq)`, so `r[0]=pos, r[2]=scenario,
r[4]=seq, r[3]=offset_ms` — exactly the indices the brief assumed. Import raised no error
with no env vars set, and does not construct an `Application` (confirmed by reading
`main.py`: the `Application()` call is inside `main()`, guarded by `if __name__ ==
"__main__":`).

**49-row diff vs spec §15: identical, position for position.** No differing position found;
mechanical line-by-line comparison of all 49 `(pos, key, seq, offset_ms)` tuples matched
spec §15's emit-order block exactly, including both out-of-order rows (pos 27, pos 31), all
five closers, and the deliberately-late row 49.

## Check 3 — Syntax (py_compile)

**PASS.** `python -m py_compile session-generator/main.py session-probe/main.py
tools/collect_results.py` — exit 0, no output.

## Check 4 — Collector expectations dry check

**PASS.** Imported `tools/collect_results.py` (structure name is `EXPECTED`, a dict of
`{"key": ..., "partition": ..., "current": ...}`).

**Expectations count table:**

| probe | s1 | s2 | s3 | s4 | s5a | s5b | s7 | total |
|---|---|---|---|---|---|---|---|---|
| key | 1 | 1 | 1 | 1 | 0 | 1 | 1 | 6 |
| partition | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 7 |
| current | 10 | 4 | 4 | 2 | 2 | 10 | 10 | 42 |

All match §6.1 exactly (key: s1/s2/s3/s4/s5b/s7 = 1 each, s5a = 0; partition: all 7 = 1;
current: s1/s5b/s7 = 10, s2/s3 = 4, s4/s5a = 2, total 42).

**Exact tuples checked, all matched:**
- key `s1` = `(1200000, 1740001, 10, (0,1,2,3,4,5,6,7,8,9))`
- key `s2` = `(1200000, 1320001, 4, (0,1,3,2))`
- key `s3` = `(1200000, 1400001, 4, (0,1,3,2))`
- key `s7` = `(0, 540001, 10, (0,1,2,3,4,5,6,7,8,9))`
- partition `s5a` = `(1200000, 1260001, 2, (0,1))`
- current `s3` sequence = `(1200000,1200001,1) (1200000,1260001,2) (1400000,1400001,1)
  (1200000,1400001,4)` — the `(1400000,1400001,1)` entry is immediately followed by
  `(1200000,1400001,4)`, confirmed.

## Check 5 — YAML consistency

**PASS**, all sub-assertions:

- Exactly 4 deployments: `Session Probe Key`, `Session Probe Partition`,
  `Session Probe Current` (all `application: session-probe`, `deploymentType: Service`,
  `state: {enabled: true, size: 1}`), `Session Generator` (`application:
  session-generator`, `deploymentType: Job`).
- Every variable name declared in each `app.yaml` appears in every deployment block of
  that application, with no missing/extra names: probe 10 names × 3 deployments, generator
  4 names × 1 deployment.
- No variable name contains `-`.
- `BASE_MS` value is `''` (empty string). `PHASE` value is `s7a`.
- Probes' `CONSUMER_GROUP` = `{sw_key_r1, sw_part_r1, sw_curr_r1}` (pairwise distinct);
  `output` = `{session-out-key, session-out-partition, session-out-current}` (pairwise
  distinct); `(EMIT_MODE, CLOSING_STRATEGY)` = `(final,key)`, `(final,partition)`,
  `(current,key)` — exact match to spec order.
- Exactly 4 topics: `session-in`, `session-out-key`, `session-out-partition`,
  `session-out-current`, each `{partitions: 1, retentionInMinutes: 1440}`.
- No leftover reference anywhere in `quix.yaml` to `sensor-data`, `config-updates`,
  `enriched-sensor-data`, `lookup-sink`, `lake-sink`, `mongodb`, `DynamicConfiguration`.

## Check 6 — Dockerfiles

**PASS.** Both `session-generator/dockerfile` and `session-probe/dockerfile`: first line
exactly `FROM python:3.13-slim-trixie`; both contain `apt-get install -y
--no-install-recommends git`; both `COPY ./requirements.txt` before `COPY . .`.

## Check 7 — Pin

**PASS.** `git ls-remote https://github.com/quixio/quix-streams.git refs/heads/fix/pr-994`
→ `74275f72ddd54ea6698c7acb8bff1e5663ec2c75` (exact match). All three `requirements.txt`
(`session-generator`, `session-probe`, `tools`) contain exactly
`quixstreams @ git+https://github.com/quixio/quix-streams.git@74275f72ddd54ea6698c7acb8bff1e5663ec2c75`
and nothing else pinned to a branch or `refs/pull`. Confirmed `C:\repos\quix-streams-wt-fix994`
worktree HEAD is exactly that SHA.

## Check 8 — Probe/generator API use vs pinned source

**PASS — no kwarg mismatch.** All checked against
`C:\repos\quix-streams-wt-fix994` at commit `74275f72`:

- `session-probe/main.py` passes no `state_dir` to `Application(...)`. Confirmed
  `app.py:282-286` only consults `Quix__Deployment__State__Path` when `state_dir is None`.
- `Collect` used only when `EMIT_MODE == "final"`; `Earliest`/`Latest` used under
  `current`. Confirmed `windows/base.py:170-173` raises `InvalidOperation("BaseCollectors
  are not supported by \`current\` windows")` when `self.collect` is set.
- `session_window(inactivity_gap_ms, grace_ms, name, on_late)` — all four kwargs exist at
  `dataframe.py:1571-1577`, signature matches the probe's call exactly.
- `.final(closing_strategy=...)` / `.current(closing_strategy=...)` — both exist at
  `windows/time_based.py:59-61` and `:91-93`.
- Generator's `Source.produce(key=, value=, headers=, timestamp=)` — all present in
  `sources/base/source.py:319-328` (`produce(value, key, headers, partition, timestamp,
  poll_timeout, buffer_error_max_tries)`).
- Generator's `Source.serialize(key=, value=, timestamp_ms=)` — present at
  `sources/base/source.py:303-309` (`serialize(key, value, headers, timestamp_ms)`).
- Collector's `app.run(timeout=30, metadata=True)` — both kwargs exist at `app.py:862-869`
  (`run(dataframe, timeout, count, collect, metadata)`).

## Check 9 — Tree hygiene

**PASS.** `git status --short` shows only the expected changes from spec §11 (deletion of
the six PR #1110 directories, `quix.yaml`/`README.md`/`.gitignore` modified, the five new
paths added) — no `.ruff_cache`, no `__pycache__`, no `.env` staged or untracked-new.
`git status --short --ignored` confirms `.env`, `.ruff_cache/`, and the three
`__pycache__/` directories (created incidentally by running the lint/syntax checks above)
are all properly gitignored (`!!` = ignored), not untracked. `.gitignore` contains
`dev-planning/session-windows-live/results/` exactly as required, plus `.ruff_cache/` per
architecture.md's noted deviation #5.

## Sanity print

- Per-check line: **1 PASS · 2 PASS · 3 PASS · 4 PASS · 5 PASS · 6 PASS · 7 PASS · 8 PASS ·
  9 PASS**
- 49-row diff: **identical**, no differing position
- Expectations count table: see Check 4 above (6 / 7 / 42, matches §6.1)
- YAML assertion summary: 4 deployments, 4 topics, all variable/value/hygiene assertions
  passed — see Check 5 above

## Round verdict

**ALL GREEN.** No bugs filed this round — every static/lint check the brief specified
passed with no deviation from spec. No production code was touched; only `ruff` was
installed into the pre-existing `quix-streams-wt-pr994` venv (not this repo), and
`__pycache__`/`.ruff_cache` artifacts were left in place since they are already gitignored.

## Round 2 — partition-mode key fix

**Scope:** verify ArchDev's one-function fix to `session-probe/main.py`'s `describe()` so it
accepts `str` (key/current mode) or `bytes` (partition mode) keys and emits an identical
record schema either way. Read-only; no git writes, no deployment.

### Check 1 — `ruff check` / `ruff format --check`

**PASS.**

```
python -m ruff check session-probe/main.py
All checks passed!

python -m ruff format --check session-probe/main.py
1 file already formatted
```

### Check 2 — `py_compile`

**PASS.** `python -m py_compile session-probe/main.py` — exit 0, no output.

### Check 3 — behaviour: str key vs bytes key produce identical records

**PASS.** With `PROBE=partition EMIT_MODE=final CLOSING_STRATEGY=partition GAP_MS=120000
GRACE_MS=0 STORE_NAME=sess CONSUMER_GROUP=t LOGLEVEL=INFO` set before import, called
`describe(value={"start": 1000, "end": 5001, "count": 3, "seqs": [0,1,2]}, timestamp=5000,
headers=None)` once with `key="r2-s1"` and once with `key=b"r2-s1"`.

```
result a (str key)   = {'start': 1000, 'end': 5001, 'count': 3, 'seqs': [0, 1, 2],
  'probe': 'partition', 'emit_mode': 'final', 'closing_strategy': 'partition',
  'key': 'r2-s1', 'run_id': 'r2', 'scenario': 's1', 'span_seconds': 4.0}
result b (bytes key)  = {'start': 1000, 'end': 5001, 'count': 3, 'seqs': [0, 1, 2],
  'probe': 'partition', 'emit_mode': 'final', 'closing_strategy': 'partition',
  'key': 'r2-s1', 'run_id': 'r2', 'scenario': 's1', 'span_seconds': 4.0}
```

Both assert `run_id == "r2"`, `scenario == "s1"`, `key == "r2-s1"`,
`isinstance(result["key"], str)`, `span_seconds == 4.0`; `a == b` and `list(a) == list(b)`
(identical key ordering) both hold.

### Check 4 — regression on untouched paths

**PASS.**
- `key="r2-s5a"` → `scenario == "s5a"` (multi-char scenario name with digit+letter survives
  the `partition("-")` split unchanged).
- `key=b"r3-k007"` → `run_id == "r3"`, `scenario == "k007"` (v2 key shape, bytes path).

### Check 5 — diff review

**PASS — diff contains only the four expected edits**, verbatim:

```diff
diff --git a/session-probe/main.py b/session-probe/main.py
index 0d3a6a5..64cabd2 100644
--- a/session-probe/main.py
+++ b/session-probe/main.py
@@ -59,8 +59,12 @@ def on_late(
     return True


-def describe(value: dict, key: str, timestamp: int, headers: Any) -> dict:
+def describe(value: dict, key: str | bytes, timestamp: int, headers: Any) -> dict:
     """Stamp a window result with the probe and the scenario it belongs to."""
+    # Partition-mode expiry keys each result by the raw store prefix
+    # (windows/session.py:293-295), so the key is bytes there and str in key mode.
+    if isinstance(key, bytes):
+        key = key.decode()
     run_id, _, scenario = key.partition("-")
     return {
         **value,
```

`on_late`, the aggregation selection, the window construction, startup logging and
`to_topic` are confirmed byte-identical — nothing else in the diff.

### Check 6 — tree hygiene

**PASS.** `git status --short`:

```
 M dev-planning/session-windows-live/progress.md
 M session-probe/README.md
 M session-probe/main.py
?? dev-planning/session-windows-live/spec-v2.md
```

Exactly the three expected modified files, nothing staged. `spec-v2.md` is an untracked
file under `dev-planning/session-windows-live/`, which the brief names as an
expected-and-ignorable concurrent-agent path — left untouched, not judged. No files under
`session-generator-v2/` or `session-verdict/` were present at check time.

### Round 2 verdict

**ALL GREEN.** No bugs filed. `describe()` correctly normalizes `bytes` keys to `str` before
`partition("-")`, both key-mode and partition-mode inputs yield equal, identically-ordered
records, and the fix is minimal — no collateral changes.
