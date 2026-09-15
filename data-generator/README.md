# data-generator

Produces keyed JSON sensor records to `sensor-data`, starting immediately — **before any
configuration exists anywhere** — and running forever.

Kafka key: the `device_id` string. Payload:

```json
{
  "device_id": "device-042",
  "seq": 137,
  "value": 24.2,
  "timestamp": 1789234567890,
  "produced_ms": 1789234567890,
  "run_id": "r1"
}
```

`seq` is per-device and monotonic. It is the join key of the paired buffered-vs-control
comparison and the only reason a row-level comparison is possible at all.

## Two things that must not be changed casually

**It is a `Service`, never a `Job`.** `LookupBuffer` has no timer: withheld records are
released, and deadlines settled, only by the arrival of further records. Stop the
generator and every buffered record freezes in RocksDB — the pipeline stays green and
the table simply stops growing. Do not stop it before the verification queries have run.

**It cycles device ids round-robin, never randomly.** A device's withheld records are
flushed by the *next record for that same key*, so round-robin fixes the per-key revisit
interval at exactly `DEVICE_COUNT × SLEEP_SECONDS`. Random selection over 100 keys has a
long tail and would leave some devices unreleased for tens of seconds.

## Sizing — redo this before changing `DEVICE_COUNT` or `SLEEP_SECONDS`

With `D` devices, total rate `R` msg/s and the buffer's `grace_ms = G`:

| quantity | formula | at D=100, R=20, G=300 s |
|---|---|---|
| per-device rate | `R / D` | 0.2 msg/s (one every 5 s) |
| per-key buffer depth, unseeded | `(R/D) · G` | 60 records (buffer warns past 1000) |
| per-key depth, seeded pre-seed | `(R/D) · SEED_DELAY` | 24 records |
| full buffer sweep cycle | `D / (4·R)` | 1.25 s |
| timeout settle lag | `G + cycle` | ~301.3 s |

The sweep budget is 4 key-slots per incoming record, so raising `D` or lowering `R`
lengthens the cycle quadratically in effect: at `D=10 000, R=1` a full cycle is 41
minutes and the timeout outcome lands 41 minutes after `grace_ms`, which looks exactly
like the buffer hanging.

`TIMESTAMP_SKEW_MS` is an escape hatch, not a tuning knob — see spec §2.4.
