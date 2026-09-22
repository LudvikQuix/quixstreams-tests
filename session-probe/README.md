# Session Probe

One `sdf.session_window()` over `session-in`, deployed three times from this directory.
The deployments differ only by variables:

| deployment | `PROBE` | `EMIT_MODE` | `CLOSING_STRATEGY` | aggregations | output |
|---|---|---|---|---|---|
| Session Probe Key | `key` | `final` | `key` | `Count` + `Collect("seq")` | `session-out-key` |
| Session Probe Partition | `partition` | `final` | `partition` | `Count` + `Collect("seq")` | `session-out-partition` |
| Session Probe Current | `current` | `current` | `key` | `Count` + `Earliest`/`Latest("seq")` | `session-out-current` |

`current` mode cannot use `Collect`: `Window.current()` raises
`InvalidOperation("BaseCollectors are not supported by 'current' windows")`
(`quixstreams/dataframe/windows/base.py:170`). If the Current probe will not start, that
is the first thing to check.

## The state volume

`Application(...)` is constructed **without** a `state_dir` argument. `Application.__init__`
(`quixstreams/app.py:282-286`) falls back to `Quix__Deployment__State__Path` only while
`state_dir is None`; passing anything, including the common
`os.environ.get("Quix__State__Dir", "state")`, overrides the platform volume and puts the
store on ephemeral container disk. The restart scenario would still pass in that case,
rebuilt from the changelog, and would prove nothing about the volume — which is why the
startup block logs `app.config.state_dir` next to `Quix__Deployment__State__Path` and the
runbook stops if they differ.

## Output records

Each emitted record is the window result (`start`, `end`, `count`, and either `seqs` or
`first_seq`/`last_seq`) plus the stamps added by `describe`: `probe`, `emit_mode`,
`closing_strategy`, `key`, `run_id`, `scenario`, `span_seconds`. `run_id` and `scenario`
are the message key split on its first hyphen, which is why `RUN_ID` must not contain one.

A record with `seqs == [-1]` (or `count == 1` and `last_seq == -1` in current mode) is a
closer session — the artificial event the generator places three gaps past a scenario's
last real event to close it. The collector drops those.

`on_late` logs `LATE key=… ts=… late_by=… would_be=[start,end)` and returns `True`, so the
library's own warning is emitted as well. Exactly one `LATE` line per probe is expected,
for `r1-s4`.

## Variables

All ten are described in `app.yaml`. `GAP_MS` must equal the generator's, `GRACE_MS` must
stay 0, and `CONSUMER_GROUP` must differ between the three deployments.
