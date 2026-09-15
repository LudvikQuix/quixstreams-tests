# PR1110 `LookupBuffer` grace-period test rig

A Quix Cloud pipeline that deliberately delivers configuration **after** the data it
applies to, and proves by observation that quix-streams PR
[#1110](https://github.com/quixio/quix-streams/pull/1110) changes the outcome: records
that today's library would have emitted immediately with their `default=` values are
instead withheld, and — if their configuration lands within `grace_ms` of real time —
emitted **enriched**.

Pinned at SHA `6632b46cdb2d493e4facf6c00b78c608ae70af87`. Full design:
[`dev-planning/pr1110-grace-period/spec.md`](dev-planning/pr1110-grace-period/spec.md).
Implementation notes and deviations:
[`dev-planning/pr1110-grace-period/architecture.md`](dev-planning/pr1110-grace-period/architecture.md).

## What it proves

Two arms of the *same* application run simultaneously over the *same* input topic, so the
before/after is a paired row-level comparison rather than two runs an operator has to
trust are comparable:

- **buffered** — `BUFFER_ENABLED=true`, `state: {enabled: true}`
- **control** — `BUFFER_ENABLED=` (blank ⇒ `buffer=None`, today's behaviour exactly)

The generator cycles `device-000 … device-099`; the seeder configures only
`device-000 … device-049`. One knob, one wall-clock window, three outcomes at once:

| outcome | arm | devices | signature |
|---|---|---|---|
| **A** buffered, then resolved | `buffered` | `000`–`049`, pre-seed records | `resolved=true AND dwell_ms>=1000 AND threshold IS NOT NULL AND region<>'unknown'` |
| **B** buffered, then timed out | `buffered` | `050`–`099` | `resolved=false AND dwell_ms>=300000 AND threshold IS NULL AND region='unknown'` |
| **C** control, no buffer | `control` | all | `dwell_ms < 100` on **every** row |

Supporting baseline **A0** (`buffered`, `000`–`049`, post-seed, `dwell_ms < 100`) exists
to separate "the buffer did not work" from "the configuration never reached this consumer
at all". If A0 is empty, the problem is delivery, not the buffer.

**The falsifiable claim:** for the `(run_id, device_id, seq)` triples where
`device_id < 'device-050'` and `ingest_ms < seed_ms`, the control arm emits
`resolved=false, threshold=NULL, dwell_ms≈0` while the buffered arm emits
`resolved=true, threshold=<the seeded value>, dwell_ms>1000` — **for the same triple**.

## Pipeline

```
                              ┌──────────────┐
                              │  mongodb     │  Service, state 1 GB
                              │  :27017      │  serviceName: mongodb
                              └──────┬───────┘
                                     │ variable group "mongodb-connection"
                                     ▼
  ┌───────────────┐   POST    ┌──────────────────────────┐  events   ┌───────────────┐
  │ config-seeder │──────────▶│ Dynamic Config Manager   │──────────▶│ config-updates│
  │ Job, sleeps   │  /api/v1/ │ Managed, contentStore=   │           │  1 partition  │
  │ 120 s first   │  configs  │ mongo, port 80           │           └───────┬───────┘
  └───────────────┘           │ serviceName config-api-svc│                  │
                              └──────────────────────────┘                   │
  ┌────────────────┐  keyed JSON   ┌──────────────┐                          │
  │ data-generator │──────────────▶│ sensor-data  │                          │
  │ Service,       │  key=device_id│ 4 partitions │                          │
  │ 100 devices    │               └──────┬───────┘                          │
  │ 20 msg/s       │          ┌───────────┴────────────┐                     │
  └────────────────┘          ▼                        ▼                     │
              ┌───────────────────────────┐  ┌──────────────────────┐        │
              │ Lookup Sink - Buffered    │  │ Lookup Sink - Control│◀───────┤
              │ BUFFER_ENABLED=true       │  │ BUFFER_ENABLED=      │◀───────┘
              │ state: enabled            │  │ (no state block)     │
              └──────────────┬────────────┘  └──────────┬───────────┘
                             └────────────┬─────────────┘
                                          ▼
                              ┌───────────────────────────┐
                              │  enriched-sensor-data     │
                              └────────────┬──────────────┘
                                           ▼
                              ┌───────────────────────────┐
                              │ Lake Sink                 │  blobStorage.bind: true
                              │ QuixTSDataLakeSink        │  table pr1110_grace_v1
                              └───────────────────────────┘
```

## How to run it

1. **Create the `mongodb-connection` variable group** and assign it to the workspace.
   `mongoConnectionGroup` on the DCM is required with no default, so nothing else can
   deploy until this exists. Members: `MONGO_HOST=mongodb`, `MONGO_PORT=27017`,
   `MONGO_USER=admin`, `MONGO_PASSWORD=mongo_password` (secret). The user and password
   must match the MongoDB deployment's `MONGO_INITDB_ROOT_*` values.
2. **Paste the PAT** into `Quix__Pat__Token` in `.env` (gitignored).
3. **Commit and push.** Quix builds from the pushed repo; local edits do nothing.
   `quix.yaml` must be in the same commit as the app directories, or the sync is rejected
   with "Reference should have affected the workspace descriptor".
4. **`POST /workspaces/{ws}/pull`, then sync.** Without the pull, the sync compares the
   old commit to itself and no-ops.
5. **Bring up MongoDB first and wait for Running** — `serviceName: mongodb` must resolve
   before the DCM's first connection attempt. Then the DCM; confirm its UI lists zero
   configurations.
6. **Start `Lookup Sink - Buffered`, `Lookup Sink - Control` and `Lake Sink`, and wait for
   all three to be Running.** They must be consuming before any data exists, and
   certainly before the seeder fires. If a sink starts *after* the seed, every record
   resolves on the first attempt and nothing buffers.
7. **Start `Data Generator`** (it ships `desiredStatus: Stopped` so this moment is under
   your control) and **run the `Config Seeder` Job**. Note the wall-clock minute: this is
   T+0. The generator produces immediately into a world with no configuration; the seeder
   sleeps 120 s, then seeds.
   *The Job auto-runs the first time it is created, so it may already have fired and
   failed while MongoDB and the DCM were still building. Restart it from the Portal —
   `replace: true` makes it idempotent.*
8. **Watch the seeder log for the readback assertion.** Two `MATCH` lines mean the
   backdated `valid_from` was stored. A `MISMATCH` exits non-zero — stop, apply the
   escape hatch (`TIMESTAMP_SKEW_MS=300000` on the generator, `TIMESTAMP_COLUMN=produced_ms`
   on the lake sink), bump `RUN_ID` and all three consumer groups, and restart from step 7.
9. **Let it run 10 minutes past T+0**, then verify. Stop the generator only when finished
   — stopping it earlier freezes every still-buffered record in place.

### Timeline

```
T+0       Generator starts. No configuration exists anywhere.
T+0..120  Pre-seed window. buffered withholds everything; control emits defaults.
T+120     Seeder POSTs 50 configs with valid_from = T-24h, reads two back, asserts.
T+120..125 Each seeded device's next record releases its whole withheld queue ENRICHED.
                                                              ==> OUTCOME A
T+120..   Seeded devices now resolve on the first attempt, dwell ~0.   ==> A0
T+300..   Unseeded devices reach grace_ms; the sweep settles them with defaults.
                                                              ==> OUTCOME B
T+600     ≥5 min of steady state in all three outcomes. Run the queries.
```

`SEED_DELAY_SECONDS=120 < GRACE_MS=300000` is deliberate: every pre-seed record for a
seeded device is still inside its grace window when its configuration lands, so outcome A
covers the whole pre-seed set with no ragged edge.

## How to read the result

**V0 — liveness, ten seconds.** Open `enriched-sensor-data` in the Portal; confirm rows
with both `"arm":"buffered"` and `"arm":"control"`. Rules out the whole "nothing is
running" class before anyone waits for a parquet flush.

**V1 — outcome census.**

```sql
SELECT arm,
       CASE WHEN device_id < 'device-050' THEN 'seeded' ELSE 'unseeded' END AS cohort,
       resolved, COUNT(*) AS rows,
       MIN(dwell_ms) AS dwell_min, ROUND(AVG(dwell_ms)) AS dwell_avg, MAX(dwell_ms) AS dwell_max
FROM pr1110_grace_v1 WHERE run_id = 'r1'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;
```

Three assertions: `buffered/unseeded` has `dwell_min >= 300000`; every `control` row has
`dwell_max < 100`; `buffered/seeded/resolved=false` and `buffered/unseeded/resolved=true`
are both **empty**.

**V2 — outcome A exists at all (the headline).**

```sql
SELECT COUNT(*) AS outcome_a_rows, MIN(dwell_ms), MAX(dwell_ms),
       COUNT(DISTINCT device_id) AS devices
FROM pr1110_grace_v1
WHERE run_id = 'r1' AND arm = 'buffered'
  AND resolved = true AND dwell_ms >= 1000 AND threshold IS NOT NULL;
```

Expect ~1 200 rows over 50 devices, `max_dwell < 300000`. **Zero means the rig failed** —
check the seeder's readback assertion and that both sinks were Running before T+120.

**V3 — the paired comparison. This table is the deliverable.**

```sql
SELECT b.device_id, b.seq,
       c.resolved AS control_resolved, c.threshold AS control_threshold, c.dwell_ms AS control_dwell,
       b.resolved AS buffered_resolved, b.threshold AS buffered_threshold, b.dwell_ms AS buffered_dwell
FROM pr1110_grace_v1 b
JOIN pr1110_grace_v1 c
  ON b.run_id = c.run_id AND b.device_id = c.device_id AND b.seq = c.seq
WHERE b.run_id = 'r1' AND b.arm = 'buffered' AND c.arm = 'control'
  AND b.device_id < 'device-050' AND c.resolved = false
ORDER BY b.device_id, b.seq LIMIT 50;
```

Every row must show `control_resolved=false, control_threshold=NULL, control_dwell<100`
beside `buffered_resolved=true, buffered_threshold=10.0+<device index>,
buffered_dwell>1000`. Zero rows means the arms never overlapped in `seq` — check that both
consumer groups were fresh and both started before T+0.

**V4 — enrichment is real, not a constant.** `threshold` must equal `10.0 + <device
index>` and `region` must cycle `eu-west, us-east, ap-south`. Rules out a lookup that
resolves to one cached document for every key.

**V5 — defaults are exactly the unbuffered behaviour.**
`SELECT COUNT(*) FROM pr1110_grace_v1 WHERE run_id='r1' AND resolved=false AND NOT
(threshold IS NULL AND region='unknown')` must be `0`.

## Negative tests

`tools/negative-tests/` — six construction- and build-time guards, runnable locally with
no broker. Three more (overflow raise / drop-newest, missing state block) are deployment
variants; see that directory's README. Run them **last and one at a time**, each with a
fresh `RUN_ID` and fresh consumer groups.

## Repo layout

```
quix.yaml                  pipeline descriptor: 7 deployments + 3 topics
mongodb/                   Mongo 8.0.21 behind init.sh (verbatim copy)
data-generator/            Service, forever, round-robin 100 devices
config-seeder/             Job, sleeps then POSTs 50 configs with backdated valid_from
lookup-sink/               the service under test, deployed twice
lake-sink/                 QuixTSDataLakeSink -> pr1110_grace_v1
tools/negative-tests/      local-only, not a deployment
dev-planning/pr1110-grace-period/   spec.md, architecture.md
```
