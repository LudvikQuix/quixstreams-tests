# Negative tests — `LookupBuffer` guards

Local checks that PR1110's build- and construction-time guards actually fire. Not a
deployment: `tools/` is not referenced by `quix.yaml`.

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r tools/negative-tests/requirements.txt
python tools/negative-tests/test_negative.py
```

Exit code 0 means all six guards fired. No broker is needed.

| id | what | expected |
|---|---|---|
| NT1 | a field with no `default=` under a buffer | `ValueError` at `join_lookup()` build time, naming the field |
| NT2 | `grace_ms=0` | `ValueError` at construction |
| NT4 | `is_resolved="nope"` | `ValueError` |
| NT5 | `on_timeout="discard"` | `ValueError` |
| NT6 | `on_overflow="drop-oldest"` | `ValueError` |
| NT7 | `max_buffered_per_key=0` | `ValueError` |

NT1 passes a stub `BaseLookup` rather than a real `QuixConfigurationService`. The real
lookup starts a Kafka consumer thread in its constructor and then **blocks** waiting for
that thread to report the configuration topic drained — without a live broker it never
returns, so it cannot be constructed on a workstation. `join_lookup` calls
`buffer.validate_fields(fields)` before it touches the lookup at all, so the stub
exercises the same code path.

## The cases this script cannot cover

They need a running pipeline. Run them **last and one at a time**, each with a fresh
`RUN_ID` and fresh `CONSUMER_GROUP`s — NT3a kills the application, so it must not run
before the main measurement.

| id | change | expected |
|---|---|---|
| NT3a | `MAX_BUFFERED_PER_KEY=5`, `ON_OVERFLOW=raise` on `Lookup Sink - Buffered` | `LookupBufferOverflowError`, the deployment dies. A key reaches depth 5 within ~25 s of an unseeded device's first record. |
| NT3b | `MAX_BUFFERED_PER_KEY=5`, `ON_OVERFLOW=drop-newest` | App survives; `SELECT device_id, COUNT(*) ... WHERE arm='buffered' AND device_id>='device-050' GROUP BY 1` shows ≤5 per device instead of ~60 |
| NT8 | remove the `state:` block from `Lookup Sink - Buffered` and deploy | Fails loudly at startup naming the missing state directory — *not* a silent in-memory fallback |

**Variant run V-DROP** (a third semantic, not a negative test): set `ON_TIMEOUT=drop` on
the buffered arm, bump `RUN_ID` and both `CONSUMER_GROUP`s, re-run. Timed-out rows must
vanish entirely — `SELECT COUNT(*) ... WHERE run_id='<R2>' AND arm='buffered' AND
resolved=false` must be `0` — while the control arm's unseeded count is unchanged, and a
rate-limited warning naming the key must appear in the log.
