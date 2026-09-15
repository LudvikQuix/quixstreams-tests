# lake-sink

Sinks `enriched-sensor-data` — the output of **both** `lookup-sink` arms — into the
lakehouse as the Iceberg-registered table `pr1110_grace_v1`, using the built-in
`QuixTSDataLakeSink`.

The rig's claim is a row-level paired join across two arms over thousands of rows. That
is a SQL question; the Portal's message viewer cannot answer it. This deployment exists
so the verification queries can.

## Why it is a separate deployment

The service under test must not carry `blobStorage: {bind: true}`. If the bind fails, a
broken lake arm would block the experiment itself rather than just its reporting surface.
With the split, `sensor-data → enriched-sensor-data` stays fully working and eyeballable
in the Portal. The output topic is also the ten-second liveness check, long before the
first parquet flush.

Sink latency does not contaminate the measurement: `dwell_ms` is computed in-process
inside `lookup-sink` from `ingest_ms`/`emit_ms`, both stamped before anything is written.

## Layout

| setting | value | why |
|---|---|---|
| `HIVE_COLUMNS` | `year,month,day,hour,arm` | `arm` is the primary analysis slice, cardinality 2. `run_id` is deliberately a plain column — its cardinality grows with every run and would shatter the table into tiny parquet files. |
| `TIMESTAMP_COLUMN` | `timestamp` | absolute epoch ms from the generator; `year/month/day/hour` derive from it |
| `SORT_COLUMN` | `emit_ms` | emission order is the natural read order. Deliberately ≠ `TIMESTAMP_COLUMN`, because an unset sort column falls back to the timestamp column and setting the two equal is indistinguishable from unset. |
| `STATS_COLUMNS` | blank | per-file min/max for every numeric and timestamp column, which here includes `dwell_ms` — exactly what the verification queries prune on |

**`HIVE_COLUMNS`, `TIMESTAMP_COLUMN` and `SORT_COLUMN` are not runtime tweaks.** The sink
validates the partition set against the catalog spec and the on-disk Hive paths at
`setup()` and raises on a mismatch, and table properties are written only at CREATE.
Changing any of them is a migration: bump `TABLE_VERSION`.

## Dependencies

`quixstreams[quixdatalake]` at the pinned PR SHA, **plus `quixportal[all]`**. The
`quixdatalake` extra pulls a bare `quixportal>=0.1.0`, whose cloud filesystem backends
(`s3fs` / `adlfs` / `gcsfs`) live behind quixportal's own `all` extra — without it the
sink imports fine and cannot write a byte. `pandas` and `pyarrow` are never hand-listed;
the `quixdatalake` extra carries their version floors.

Blob credentials are never passed explicitly. `blobStorage: {bind: true}` injects
`Quix__BlobStorage__Connection__Json` (which `quixportal` reads, extracting the bucket
itself) along with `Quix__Lakehouse__Catalog__Url` and `Quix__Lakehouse__Catalog__AuthToken`.
A startup log line reports whether the catalog URL resolved.
