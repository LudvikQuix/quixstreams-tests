# Session Generator

A Job that produces one phase of a 49-row scripted event table into `session-in`, then
exits 0. It is the only writer in the rig; every expectation in
[`../dev-planning/session-windows-live/spec.md`](../dev-planning/session-windows-live/spec.md)
is derived arithmetically from this table.

## The three runs

| `PHASE` | rows | when |
|---|---|---|
| `s7a` | 1-5 (`r1-s7` seq 0-4) | on Job creation |
| `s7b` | 6-11 (`r1-s7` seq 5-9 + closer) | **after** the three probes have been stopped and restarted |
| `main` | 12-49 (scenarios s1-s5b) | last |

`BASE_MS` must be the same epoch-millisecond value on all three runs. `s7a` and `s7b` are
minutes apart in wall clock but their events are 60 s apart in event time; a per-run
`now` would put seq 4 and seq 5 more than one inactivity gap apart and split the session
the restart test exists to observe. Blank `BASE_MS` means "use now" and is logged at
WARNING — valid only if you are running a single phase.

## Why a Source and not a producer loop

`app.add_source(...)` + `app.run()` rather than `app.get_producer()`: `Application._run_sources`
loops `while run_tracker.running and source_manager.is_alive()`, so when `run()` returns the
application returns and the Job exits 0 — the Job semantics the runbook needs. The resolved
broker topic name is logged at the start and end of `run()`; it must carry the workspace
prefix.

## Emit order is the independent variable

The table is emitted in list order and `self.flush()` runs after every message. On the
partition probe one watermark governs every key, so a row emitted early is dropped as late
and a row emitted late closes another scenario's session a step too soon. Two rows are
deliberately out of order (pos 27 and pos 31) and one is deliberately late (pos 49); pos 31
has only 20 s of margin. Each row is logged as `pos= key= seq= offset_ms= event_ms=`, so the
produced order is auditable after the fact. See spec §7 for the proof.

## Variables

`output`, `PHASE`, `BASE_MS`, `RUN_ID` — described in `app.yaml`.
