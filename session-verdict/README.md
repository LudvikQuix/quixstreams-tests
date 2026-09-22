# Session Verdict V2

A **Job** that reads the v2 input topic and the three v2 probe output topics, derives
what each probe must have emitted, compares, prints the verdict and exits non-zero on any
FAIL. Phase 2 of the rig specified in
[`../dev-planning/session-windows-live/spec-v2.md`](../dev-planning/session-windows-live/spec-v2.md).

## The oracle is a reimplementation, and that cuts both ways

> `expected_sessions()` implements the same three rules `session.py` implements. A
> disagreement is a finding **in one of them**, and a bug in the oracle is exactly as
> likely as a bug in the SDK until someone traces the disagreement by hand.

It is written from spec-v2 section 3, not ported from `session.py`. In particular the two
expiry cursors — `expire_by_key`'s per-key cursor and `expire_by_partition`'s partition
checkpoint — are **not** modelled. They are optimisations that must be invisible from the
outside, so the oracle closes every session whose `end <= close_before`, every time, and a
disagreement that traces back to a cursor is a finding by design.

Triage protocol for any FAIL: reduce to the single disagreeing key, hand-trace its events
against R1/R2/R3, only then say which side is wrong, and reproduce it as a red unit test in
quix-streams before any code change.

## Its own acceptance test

```
python main.py --selftest
```

replays the hand-derived run of spec-v2 section 7.1 — four keys, two partitions, one idle
key, the assumed `k000→P0, k001→P1, k002→P0, k003→P1` map — and asserts 6 records for the
key probe, 7 for the partition probe (the extra one being the idle key's session, closed by
its partition-mate's traffic) and 24 ordered updates for the current probe. No broker, no
environment. If the oracle disagrees with that table, the oracle is wrong.

## Why it reads the input topic and not the generator's manifest

It verifies what actually *landed*, including any reordering, duplication or loss between
the generator and the probes; it carries the real partition per record, so nothing has to
reimplement murmur2; and it needs only `GAP_MS` and `GRACE_MS`, which makes it independent
of `SEED`, `GAP_MODE`, `KEY_COUNT` and `STREAMS_PER_KEY`. A generator bug therefore cannot
hide itself inside the expectation. It is also exactly the byte stream the probes consumed,
so a disagreement cannot be blamed on "the verdict read something else".

## Completion rule

`app.run(timeout=IDLE_TIMEOUT_S, count=MAX_RECORDS, metadata=True)` over all four topics at
once. `timeout` is an **idle** timeout, so the call returns once nothing has arrived on any
of them. The probes emit only in response to input, so "input quiet and all three outputs
quiet" means either the probes are caught up or one is stuck — and a stuck probe shows up as
MISSING records with its lag visible in the per-topic read counts. `RunTracker` gives the
timeout a 60 s head start, so the Job prints nothing for about `IDLE_TIMEOUT_S + 60` s.

## What it prints

The run header (read counts per topic, distinct `instance_id` and `base_ms` values, the
keys that never received a closer), the observed `key → partition` map with a
`PARTITION SHAPE` line for any partition holding fewer than two keys, the verdict table one
row per `(probe, key)`, then every `MISSING`, `UNEXPECTED`, `DUPLICATE` and `ORDER` finding,
then the aggregates, then `PASS`/`FAIL` and the failing-check count.

It also produces one record per `(probe, key)` and one summary record to `session-v2-verdict`.

## Running it from a laptop

Unchanged. `Application()` with no arguments picks up `Quix__Sdk__Token` and
`Quix__Portal__Api` from the environment, and a PAT is accepted where an SDK token is
expected, so exporting the three `.env` values and running `python main.py` is the secondary
path — with no second copy of the oracle. That path is blocked today by the broker transport
failure recorded as spec-v2 risk R1, which is why the Job exists.

## Variables

Described in `app.yaml`. `GAP_MS` and `GRACE_MS` must equal the three probes'.
`CONSUMER_GROUP` must be fresh per run.
