# lookup-sink — the service under test

One application directory, **two simultaneous deployments** over the same input topic:

| deployment | `ARM` | `BUFFER_ENABLED` | `state:` | `CONSUMER_GROUP` |
|---|---|---|---|---|
| Lookup Sink - Buffered | `buffered` | `true` | `enabled: true, size: 1` | `pr1110_buffered_r1` |
| Lookup Sink - Control | `control` | *(blank)* | **none** | `pr1110_control_r1` |

`BUFFER_ENABLED` blank means `buffer=None`, which is today's unbuffered behaviour exactly
— that *is* the control arm. Running both at the same wall-clock moment makes the
comparison a paired row-level join on `(run_id, device_id, seq)` rather than two runs an
operator has to trust are comparable.

The consumer groups **must differ**, or the two arms split the partitions instead of each
seeing every record, and the paired join returns almost nothing.

## Topology

```python
sdf = app.dataframe(data_topic)
sdf = sdf.apply(stamp_ingest)                                  # ingest_ms, before the join
sdf = sdf.join_lookup(lookup, fields, on="device_id", buffer=buffer)
sdf = sdf.apply(stamp_emit)                                    # emit_ms, dwell_ms, after
sdf.to_topic(output_topic)
```

`ingest_ms` lives in the record value, which is what the buffer stores and reads back, so
it survives the buffer round-trip. `emit_ms` is stamped **after** the join so a released
record gets its own emission time rather than the triggering record's. `dwell_ms =
emit_ms - ingest_ms` is the headline measurement, computed in-process before anything is
written, so no sink or topic latency contaminates it.

## Four settings that are not stylistic

- **`key_deserializer="str"` on the data topic.** Configurations are addressed by key,
  and the buffer's per-key bookkeeping prefixes on the message key. Left as bytes, the
  key never matches a `target_key`.
- **`fallback="default"` on the lookup.** The SDK default is `"error"`, which re-raises
  inside `join()` and kills the application when content cannot be fetched. A per-field
  `default=` does not cover this — `default` is consulted when the configuration is
  *absent*, never when the HTTP call throws.
- **Every field declares `default=`.** `LookupBuffer.validate_fields()` raises at
  pipeline-build time otherwise (test NT1).
- **`is_resolved=lambda v: not v["__unresolved__"]`,** never
  `lambda v: v["threshold"] is not None`. The latter cannot tell "no configuration" from
  "configuration present, threshold null", and would silently reclassify a timed-out
  record as an enriched one — the exact error this rig exists to rule out.
  `unresolved_types_field="__unresolved__"` makes the lookup write
  `sorted(unresolved_types)` on every joined record; empty list means everything
  resolved.

## Output row

`run_id`, `arm`, `device_id`, `seq`, `value`, `timestamp`, `produced_ms`, `ingest_ms`,
`emit_ms`, `dwell_ms`, `resolved`, `unresolved_types`, `threshold`, `region`,
`seeded_expected`, `buffer_enabled`, `grace_ms`, `on_timeout`.

`__unresolved__` is removed after `resolved` and `unresolved_types` are derived from it:
a list-typed column is awkward in the lake and nothing downstream needs the raw field.
`is_resolved` reads it *inside* `join_lookup`, before that cleanup runs.

## The buffer is an expanded transform

One input record can emit many outputs — up to `SWEEP_EMIT_BUDGET = 256` sweep emissions
plus a same-key release queue — **each with its own key, timestamp and headers**, and an
output may belong to a different key than the record that triggered it. Output order is
not input order. Nothing here counts outputs against inputs, and no verification query
may rely on offset order.

Per incoming record the operator (1) joins and evaluates `is_resolved`, (2) sweeps a
bounded slice of other keys on the same partition past their deadline (`SWEEP_BUDGET = 4`
keys), then (3) buffers, releases or passes through. There is no timer: **the traffic of
healthy keys is what settles the deadlines of unconfigured ones.** The sweep is
per-partition, so a partition with buffered keys and no traffic never settles them.

## Grace period is wall-clock

`cutoff = int(time.time()*1000) - grace_ms`, measured from the moment the record enters
the operator. Not event time, not the record timestamp. `GRACE_MS` must exceed
`SEED_DELAY_SECONDS · 1000`, or the earliest pre-seed records time out before their
configuration arrives.

`AUTO_OFFSET_RESET=latest` matters: with `earliest`, a restart after seeding re-drains the
pre-seed backlog into a world where every configuration already exists, and every replayed
record resolves instantly, contaminating the table with rows that look late but never
were.
