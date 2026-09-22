# Architecture notes — session-windows-live

Implementation notes for the rig specified in `spec.md`. Pin:
`quixstreams @ git+https://github.com/quixio/quix-streams.git@74275f72ddd54ea6698c7acb8bff1e5663ec2c75`,
confirmed as the head of `refs/heads/fix/pr-994` with `git ls-remote` before it was
written into the three `requirements.txt` files.

## Generator — order and timestamps

`session-generator/main.py` holds `EVENTS`, a module-level list of
`(pos, phase, scenario, offset_ms, seq)` in emit order, and `rows_for_phase(phase)`
returning its slice. Every environment variable is read inside `main()` and the
`Application` is built under `if __name__ == "__main__":`, so the table can be imported
and printed without a broker.

Offsets are written as import-time constant expressions (`MAIN_BAND_MS + 60000`, closers
as `... + 3 * GAP_MS`) rather than as the literals of spec §5. The values are frozen at
import, so there is still no arithmetic at run time, but `GAP_MS` and `MAIN_BAND_MS` stay
load-bearing instead of being decorative constants, and each closer's derivation is
visible next to it. The sanity print of the resolved table is the check against §15.

Per row the source calls `serialize(key=f"{RUN_ID}-{scenario}", value=payload,
timestamp_ms=base_ms + offset_ms)` and then
`produce(key=…, value=…, headers=…, timestamp=message.timestamp)` —
`Source.produce` takes a `timestamp` argument (`sources/base/source.py:319-328`), so the
Kafka timestamp is the scripted event time, not wall clock. `self.flush()` runs after
every row: emit order is the experiment's independent variable and a retried librdkafka
batch must not reorder it. Each row logs `pos= key= seq= offset_ms= event_ms=`, and the
resolved broker topic name is logged at the start and end of `run()`.

The Job semantics come from `Application._run_sources`, which loops while the source is
alive: when `run()` returns, `app.run()` returns and the process exits 0.

## Probe — aggregations per mode, and the state directory

`session-probe/main.py` reads its ten variables at module level and builds the pipeline in
`main()`. `EMIT_MODE` selects both the aggregations and the emitter:

| `EMIT_MODE` | aggregations | emitter |
|---|---|---|
| `final` | `count=Count(), seqs=Collect("seq")` | `.final(closing_strategy=CLOSING_STRATEGY)` |
| `current` | `count=Count(), first_seq=Earliest("seq"), last_seq=Latest("seq")` | `.current(closing_strategy=CLOSING_STRATEGY)` |

The split is forced by `Window.current()`, which raises
`InvalidOperation("BaseCollectors are not supported by 'current' windows")` at
`dataframe/windows/base.py:170` — a single shared aggregation set would crash the Current
probe at build time. `final()`/`current()` take `closing_strategy` on `TimeWindow`
(`dataframe/windows/time_based.py:60` and `:92`), which `SessionWindow` extends.

`Application(...)` is constructed **without** `state_dir`. `Application.__init__` at
`app.py:282-286` reads

```python
if state_dir is None:
    state_dir = QUIX_ENVIRONMENT.state_dir or ("/app/state" if is_quix_deployment() else "state")
```

so any value passed — including `os.environ.get("Quix__State__Dir", "state")` — wins over
`Quix__Deployment__State__Path` and silently moves the store to ephemeral container disk.
The resolved value is logged as `app.config.state_dir`: `Application.config`
(`app.py:463`) returns the `ApplicationConfig` whose `state_dir` field (`app.py:1416`) is
populated from that same resolution at `app.py:375`. The startup block prints it next to
`Quix__Deployment__State__Path` and `Quix__Deployment__State__Enabled`; runbook step 5
stops the run if they differ.

`LOGLEVEL` is passed to `Application(loglevel=...)` rather than to `logging.basicConfig`,
which is why the whole startup block is emitted after the `Application` is constructed.

`describe()` (an `apply(..., metadata=True)`) splits the message key on its first hyphen
into `run_id` and `scenario` and stamps `probe`, `emit_mode`, `closing_strategy`, `key`
and `span_seconds` onto the window result. `on_late` logs
`LATE key=… ts=… late_by=… would_be=[start,end)` and returns `True`, so
`TimeWindow._on_expired_window` (`time_based.py:148`) also emits the library's own
warning — two independent log lines for the single S4 drop.

## Collector — how a record is matched

`tools/collect_results.py` registers one no-op dataframe per output topic and calls
`app.run(timeout=30, metadata=True)`. The returned records are **flat**:
`RunCollector.add_value_and_metadata` (`runtracker.py:24-47`) merges the deserialized
value into the record and prefixes the metadata (`_key`, `_topic`, `_timestamp`,
`_partition`, `_offset`). So the matching reads `record["probe"]` and
`record["scenario"]`, not `record["value"][...]` as spec §9 phrases it.

Pipeline: group by `record["probe"]` → write the raw JSONL per probe → drop records whose
`run_id` differs from `--run-id` → drop closer-only records (`seqs == [-1]`, or
`count == 1 and last_seq == -1` in current mode) → derive `base_ms` → normalise to
`(start - base_ms, end - base_ms, count[, tuple(seqs)])` → compare per
`(scenario, probe)` cell against `EXPECTED`.

`base_ms` is the **minimum** `start` of the key probe's `s7` records, not the only one:
if S7 fails by fragmenting, several `s7` records exist and the earliest still carries the
true origin, so the failure surfaces as an extra record rather than as a shifted table.
The `s1 start - 1200000` fallback and the hard FAIL are as specified.

Final probes compare a multiset per scenario (`Counter` difference gives MISSING,
UNEXPECTED and DUPLICATE separately, with DUPLICATE distinguished by the record also
being expected); the current probe compares the ordered list, because S3's supersession
is only evidence if the `start=1400000` update arrives before the `start=1200000` one
that replaces it. `.env` is parsed with `os.environ.setdefault`, so an exported value
wins over the file; nothing is ever printed except the workspace id.

## Deviations from the spec

1. **`on_late` returns `True`** (spec §8.1), not `False` as the build brief restated it.
   `True` makes the library log its own warning in addition to the `LATE` line.
2. **The collector uses the §9 route** (`app.dataframe` + `app.run(timeout=…)`), not the
   brief's `app.get_consumer()`: a manual poll loop over three topics is exactly the
   hand-rolled consumer the house rules forbid when a native primitive exists.
3. **Record shape.** Records are flat, as described above; §9's `value["probe"]` is
   `record["probe"]` in practice.
4. **Spec risk R2 is inaccurate at this commit.** `TopicManager.topic()`
   (`models/topics/manager.py:179-181`) calls `_get_or_create_broker_topic` and
   `_configure_topic` immediately, so `app.topic(name).name` is already the
   workspace-prefixed broker name — prefixing does not wait for `app.run()`. The `Source`
   pattern is kept regardless, for R2's second reason (a source's `run()` returning is
   what makes the Job exit 0), and the logged topic names are meaningful at startup.
5. **`.gitignore` also gained `.ruff_cache/`**, so the lint gate does not dirty the tree.
