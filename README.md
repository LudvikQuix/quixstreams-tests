# Session windows on a live Quix deployment

A Quix Cloud pipeline that proves, on a real deployment with a real RocksDB state volume
and a real mid-run restart, that `sdf.session_window()` behaves exactly as its docstring
and `docs/windowing.md` claim — by producing a scripted, arithmetically derived event
table into a one-partition topic and asserting the **exact** `(start, end, count, seqs)`
of every emitted session against a locally-run verdict table.

The rig is falsifiable in both directions: every expected record must appear **exactly
once**, and no record beyond the expected set may appear.

Pinned at quix-streams `fix/pr-994`, SHA
[`74275f72ddd54ea6698c7acb8bff1e5663ec2c75`](https://github.com/quixio/quix-streams/commit/74275f72ddd54ea6698c7acb8bff1e5663ec2c75).
Full design: [`dev-planning/session-windows-live/spec.md`](dev-planning/session-windows-live/spec.md).
Implementation notes and deviations:
[`dev-planning/session-windows-live/architecture.md`](dev-planning/session-windows-live/architecture.md).

## What it proves

Three rules, quoted from `quixstreams/dataframe/windows/session.py`, with `G = gap`,
`g = grace`, `W = watermark`:

| # | rule |
|---|---|
| R1 | **Join.** An event joins a stored session `[start, end)` iff `start - G <= ts` and `end + G > ts`. Matching two sessions merges them into `[min(start, ts), max(end, ts+1))`. |
| R2 | **Late.** An event is dropped iff `ts < W - G - g`. `W` is the max event timestamp seen for the key (`closing_strategy="key"`) or for the partition (`"partition"`). |
| R3 | **Close.** A session closes iff `end <= W - 2G - g`. |

`end` is `last_event + 1`, so a session whose last event is at `t` closes only once
`W >= t + 1 + 2G + g` — one millisecond later than a naive `t + 2G + g`. Two steps of the
event table sit exactly on that 1 ms.

## Pipeline

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

All three probes are **one application directory** (`session-probe/`) deployed three
times; they differ only by variables. That is what makes the key-vs-partition contrast a
paired comparison rather than two runs an operator has to trust are comparable.

## Scenarios

All times are offsets from `BASE_MS`; `M = 1200000`, `G = 120000`, `grace = 0`.

| scenario | key | probe | expected `(start, end, count)` |
|---|---|---|---|
| S1 continuous activity does not fragment | `r1-s1` | key / partition | `(1200000, 1740001, 10)` |
| | | current | 10 updates, `start=1200000`, `end` 1200001→1740001, count 1→10 |
| S2 in-gap out-of-order event joins | `r1-s2` | key / partition | `(1200000, 1320001, 4)` seqs `[0,1,3,2]` |
| | | current | `(1200000,1200001,1) (…,1260001,2) (…,1320001,3) (…,1320001,4)` |
| S3 bridging event merges two sessions | `r1-s3` | key / partition | `(1200000, 1400001, 4)` seqs `[0,1,3,2]` |
| | | current | `(1200000,1200001,1) (…,1260001,2) (1400000,1400001,1) (1200000,1400001,4)` |
| S4 late event dropped, never resurrects | `r1-s4` | key / partition | `(1200000, 1260001, 2)` + one `on_late`, no record at 1230000 |
| | | current | `(1200000,1200001,1) (1200000,1260001,2)` |
| S5 `closing_strategy` decides idle keys | `r1-s5a` | key | **no record** |
| | `r1-s5a` | partition | `(1200000, 1260001, 2)` |
| | `r1-s5a` | current | `(1200000,1200001,1) (1200000,1260001,2)` |
| | `r1-s5b` | key / partition | `(1200000, 1740001, 10)` |
| S7 session survives a process restart | `r1-s7` | key / partition | `(0, 540001, 10)` — once, across a restart |
| | | current | 10 updates, `start=0`, 5 before and 5 after the restart |

`seqs` is in **event-time** order, not arrival order: `Collect` stores values under
`id=timestamp_ms` and reads them back in id order, which is why S2 and S3 expect
`[0, 1, 3, 2]`.

S5 is the whole point of the pair: one record present on `session-out-partition` and
absent from `session-out-key`, from the same input. `grace_ms` is deliberately out of
scope — it shifts the late bound and the close bound by the same constant and never
changes which session an event joins, so it is left to the unit tests.

## Runbook

Prerequisites: `quix use testrig`. The deployments exist only after the first sync.

1. **Pick `BASE_MS`.** `python -c "import time; print(int(time.time()*1000))"`. Put it in
   the `Session Generator` block in `quix.yaml`. Keep it identical for all three phases.
   On a rerun take a fresh value at least `2100000` ms (35 min) above the previous run's.
2. **Commit and push `dev`.** Quix builds from the pushed branch; `quix.yaml` must be in
   the same commit as the app directories, or the sync is rejected with "Reference should
   have affected the workspace descriptor".
3. **Sync.** `POST /workspaces/{ws}/pull`, then `quix cloud environments sync <ws>` (or
   Portal → Pipeline → Sync). Without the pull, the sync compares the old commit to itself.
4. **Wait** for three probes Running and the `Session Generator` Job Completed. The Job
   auto-runs on creation with `PHASE=s7a`. The probes do not need to be up first:
   `auto_offset_reset="earliest"` with a fresh consumer group reads from the beginning.
5. **Verify the state volume** in each probe's log: `state_dir` must equal
   `Quix__Deployment__State__Path`, and `Quix__Deployment__State__Enabled` must be `true`.
   If `state_dir` is `state` or `/app/state` while the platform path differs, **stop
   here** — the rig would still pass S7 via changelog recovery and prove nothing about
   the volume.
6. **Restart.** Wait ~20 s after the Job completes so the probes' checkpoints commit
   (default `commit_interval` 5 s). Stop all three probes; poll until each reports
   Stopped (SIGTERM shutdown can take a minute or more); start all three; poll until
   Running and each log shows its state partitions opened / recovered.
7. **Phase `s7b`.** The generator is Completed, so a PATCH is accepted (a PATCH against a
   Running deployment is silently ignored). PATCH `PHASE=s7b`, start it, wait for
   Completed.
8. **Phase `main`.** PATCH `PHASE=main`, start it, wait for Completed.
9. **Collect.** Wait ~30 s, then `python tools/collect_results.py --run-id r1`
   (dependencies: `pip install -r tools/requirements.txt`; credentials come from the
   untracked root `.env`). For the S4 evidence also grep each probe's log for
   `LATE key=r1-s4`.

## Reading the verdict

The collector prints a `scenario × probe` table of expected-vs-observed counts with a
PASS or FAIL per cell, then every MISSING, UNEXPECTED, DUPLICATE and ORDER finding, then
the counts of dropped closer-only and other-run records. `echo $LASTEXITCODE` is the
machine-readable answer: 0 means every cell passed. Raw records land in
`dev-planning/session-windows-live/results/<run-id>/<probe>.jsonl` (gitignored).

Closer-only records are expected and dropped: the generator places an artificial event
(`seq = -1`) three gaps past a scenario's last real event, purely to close the real
session. That event forms a one-event session of its own, which the collector discards.

"The branch works" means all seven acceptance criteria in spec §12 hold — in short:
exactly 6 records on `session-out-key` and no `r1-s5a`; those 6 plus `r1-s5a` plus 4
closer-only on `session-out-partition`; 42 updates in the given per-key order plus 6
closer-only on `session-out-current`; the `r1-s7` record emitted exactly once across the
restart; exactly one `on_late` per probe; the state-dir log line; and exit code 0.

**S7 caveat:** S7 proves *state survives a restart*, not *state lived on the volume* — an
ephemeral store rebuilt from the changelog would also pass. The volume is verified
separately, by the startup log in runbook step 5.

## Resetting for a rerun

Bump `RUN_ID`, all three `CONSUMER_GROUP`s and `BASE_MS` together, then repeat from step
2. A new consumer group gives each probe a fresh state directory (`StateStoreManager`
roots the store at `state_dir / consumer_group`) and a fresh changelog; the new `RUN_ID`
keeps the replayed old-run keys disjoint; the new `BASE_MS` keeps the partition watermark
monotonic across the replay. A hard reset (delete and recreate the four topics) is only
needed if a phase was accidentally run twice within one `RUN_ID`.

Deployment names must be ones that have never existed in this workspace: deleting a
deployment orphans its state volume, and a name collision re-attaches the stale one.

## Layout

| path | what |
|---|---|
| `session-generator/` | the Job: 49-row scripted event table, one phase per run |
| `session-probe/` | the one probe application, deployed three times |
| `tools/collect_results.py` | the verdict script, run locally |
| `quix.yaml` | four deployments, four one-partition topics |
| `dev-planning/session-windows-live/` | spec, architecture notes, collector output |
| `dev-planning/pr1110-grace-period/` | the previous rig's design history (its code is gone) |
