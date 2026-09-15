# PR1110 LookupBuffer Grace-Period Test Rig

**Status:** Draft
**Project:** quixstreams-tests
**Created:** 2026-09-15
**Planned with:** Buddy
**Target environment:** `testrigorg-quixstreamstests-quixstr-df57e12b` ("quixstreams tests - QuixStream Pipeline"), currently empty
**Feature under test:** quix-streams PR #1110, pinned at SHA `6632b46cdb2d493e4facf6c00b78c608ae70af87`

---

## 1. Goal

Build a Quix Cloud pipeline that deliberately delivers configuration **after** the data it
applies to, and proves by observation that `LookupBuffer` (PR #1110) changes the outcome:
records that the current library would have emitted immediately with their `default=` values
are instead withheld, and — if their configuration lands within `grace_ms` of real time —
emitted **enriched**.

The rig must make three outcomes simultaneously visible in one output table, and must carry a
**control arm running at the same wall-clock moment on the same input stream** so the
before/after is a paired comparison rather than two runs an operator has to trust are
comparable.

### 1.1 Non-goals

- No performance tuning, no `grace_ms`/rate sweeps, no CI integration.
- No test of `on="..."` callables, wildcard (`"*"`) configs, `bytes_field`, or binary content.
- No multi-replica / rebalance test of the buffer's changelog recovery. (Noted in §11 as the
  obvious Phase 2.)
- No attempt to measure the *cost* of buffering (throughput, RocksDB growth). Phase 1 is
  "does it do the thing", not "what does it cost".
- No upstream contribution, no PR review comments. This rig produces evidence, nothing else.

---

## 2. Feature under test — the PR1110 API contract

Everything in this section was read from the source at the pinned SHA. Do not re-derive it,
and do not trust the PR description over this section.

### 2.1 Public API

```python
from quixstreams.dataframe.joins.lookups import LookupBuffer, LookupBufferOverflowError

OnTimeout  = Literal["emit", "drop"]
OnOverflow = Literal["drop-newest", "raise"]

class LookupBuffer:
    def __init__(
        self,
        grace_ms: Union[int, timedelta],
        is_resolved: Callable[[dict[str, Any]], bool],
        on_timeout: OnTimeout = "emit",
        max_buffered_per_key: int = 10_000,
        on_overflow: OnOverflow = "drop-newest",
        store_name: str = "lookup-buffer",
    ) -> None: ...
```

```python
def join_lookup(
    self,
    lookup: BaseLookup,
    fields: dict[str, BaseField],
    on: Optional[Union[str, Callable[[dict[str, Any], Any], str]]] = None,
    buffer: Optional[LookupBuffer] = None,      # NEW in PR1110
) -> "StreamingDataFrame": ...
```

`QuixConfigurationService.__init__` at the same SHA (the parameters this rig depends on):

```python
def __init__(
    self,
    topic: Topic,
    app_config: Optional["ApplicationConfig"] = None,
    broker_address: Optional[Union[str, ConnectionConfig]] = None,
    consumer_poll_timeout: Optional[float] = None,
    consumer_group: str = DEFAULT_CONSUMER_GROUP,        # "enrich"
    consumer_extra_config: Optional[dict] = None,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    quix_sdk_token: Optional[str] = None,
    cache_size: int = 1000,
    fallback: Literal["error", "default"] = "error",
    unresolved_types_field: Optional[str] = None,        # the is_resolved handle
): ...

def json_field(self, jsonpath: str, type: str,
               first_match_only: bool = True,
               default: Any = RAISE_ON_MISSING) -> JSONField: ...
```

### 2.2 The nine semantics this rig is built around

| # | Semantic | Consequence for the rig |
|---|---|---|
| S1 | **`grace_ms` is wall-clock real time**, measured from the moment the record enters the operator. Source: `cutoff = int(time.time()*1000) - grace_ms`. Not event time, not the record timestamp. | The rig must control *real* arrival-vs-seed time (§4.2), not record timestamps. |
| S2 | **`is_resolved` is called on the ENRICHED value**, after the join ran. | Predicate inspects output fields, never the input payload. See S9 for the correct one. |
| S3 | **`on_timeout="emit"`** (default) releases the record with each field's declared `default=` — byte-identical to what unbuffered `join_lookup` would have produced. **`"drop"`** discards it with a rate-limited warning. | Outcome B is *indistinguishable from the control arm on field values alone*. `dwell_ms` is the only thing that separates them. This is why §5 stamps it. |
| S4 | **Every field must declare `default=`** when a buffer is attached; `LookupBuffer.validate_fields()` raises `ValueError` at pipeline-build time otherwise. | Negative test NT1 (§10). |
| S5 | **`on_overflow`**: `"drop-newest"` silently discards new records once a key holds `max_buffered_per_key`; `"raise"` throws `LookupBufferOverflowError` and kills the app. | Negative tests NT3a / NT3b (§10). |
| S6 | Construction-time `ValueError`s: `grace_ms <= 0`; `is_resolved` not callable; bad `on_timeout`; bad `on_overflow`; `max_buffered_per_key < 1`. | Negative tests NT2, NT4-NT7 (§10), all local. |
| S7 | **The buffer is a changelog-backed timestamped state store** (`store_name`, default `"lookup-buffer"`). PR adds `TimestampedPartitionTransaction.delete_interval()`. | **The buffered deployment MUST carry `state: {enabled: true}` in `quix.yaml`.** A misconfigured deployment fails loudly at startup. The control arm must NOT have it, so its absence is itself a check. |
| S8 | `buffer=None` (default) preserves today's behaviour exactly. | That *is* the control arm (§4.4). |
| S9 | `unresolved_types_field="<name>"` makes the lookup write `sorted(unresolved_types)` into that field on **every** joined record — a type lands in the list exactly when `_find_version()` returned `None`. Empty list ⇒ everything resolved. | `is_resolved=lambda value: not value["__unresolved__"]`. **Do not** use `lambda v: v["threshold"] is not None` — it cannot tell "no config" from "config present, threshold null", and would silently misclassify outcome B as A. |

### 2.3 The release mechanism — read this before designing anything

`BufferOperator.__call__` runs, **per incoming record**, in this order:

1. **Join** the incoming record, then evaluate `is_resolved` on the enriched value.
2. **Sweep** — settle a bounded slice of *other keys on the same partition* that are past
   their deadline. Source comment: *"Every record drives the sweep — the traffic of healthy
   keys is what settles the deadlines of unconfigured ones."* Budgets: `SWEEP_BUDGET = 4`
   keys and `SWEEP_EMIT_BUDGET = 256` records per incoming record. The cursor is a
   per-partition round-robin over pending prefixes.
3. **Buffer / release / pass through** — a record whose key already has withheld records
   joins the queue *behind* them even if its own lookup resolved (within-key arrival order is
   preserved); if it resolved, it then releases the survivors ahead of itself and goes out
   last.

Four consequences, each of which constrains a later section:

- **R1 — There is no timer. If the generator stops producing, nothing is released and nothing
  ever times out.** Buffered records sit in RocksDB indefinitely. The generator is therefore a
  `Service` that runs forever, never a burst-then-exit `Job`. *This is the single failure mode
  that would make the whole rig show nothing.*
- **R2 — Outcome A (enriched release) needs same-key traffic.** A device's withheld records are
  flushed by the *next record for that same `device_id`*, after its config lands. Hence the
  generator round-robins over `DEVICE_COUNT`, guaranteeing every key is revisited on a fixed
  interval (§6.3). The sweep alone settles *timeouts*, not enriched releases.
- **R3 — Outcome B's settle lag is `grace_ms` PLUS one sweep cycle, not exactly `grace_ms`.**
  With `DEVICE_COUNT = D` keys and total rate `R` msg/s, a full sweep cycle takes
  `D / (4·R)` seconds (each record settles 4 key-slots; the per-partition split cancels out).
  §6.3 sizes this to ~1.25 s, so B lands at ≈ `grace_ms + 1.3 s`. Verification asserts
  `dwell_ms >= GRACE_MS`, never `≈ GRACE_MS`.
- **R4 — The sweep is per-partition.** Keys are only settled by traffic landing on *their own*
  partition. A partition holding buffered keys but receiving no records never settles them.
  Round-robin over 100 keys hashes across all 4 partitions of `sensor-data`, so every
  partition has traffic; see §8.

Two more, from the same source:

- **R5 — This is an expanded transform** (`add_transform(..., expand=True)`). One input record
  can emit N outputs, **each with its own key, timestamp and headers**, and an output may
  belong to a *different* key than the record that triggered it. Output order is not input
  order. Nothing downstream may assume 1:1, and no verification query may rely on offset order.
- **R6 — `RELEASE_WARN_RECORDS = 1000`.** A release deserializes a key's entire surviving
  buffer inside one callback and warns past 1000 records. Steady-state per-key depth ≈
  `per-key rate × grace_ms`; §6.3 shows this rig peaks at 60.

### 2.4 The trap that would make this rig a no-op — version selection

`Configuration.find_valid_version(timestamp)` selects the version whose **`valid_from` is at
or before the record's timestamp**. `valid_from` is populated by `ConfigurationVersion.from_event()`
from the DCM event's ISO8601 timestamp.

So, with a naive seeder:

> Record produced at T=0. Config POSTed at T=120 s, so `valid_from` = 120 s.
> The record's timestamp (0) is **before** `valid_from` (120 s) ⇒ `find_valid_version` returns
> `None` ⇒ the record is **unresolvable forever**, even though its config is sitting in the
> cache. The buffer holds it for the full `grace_ms` and then emits defaults.

Under that seeder, **every pre-seed record becomes outcome B and outcome A never occurs**, while
every post-seed record resolves on the first attempt and never enters the buffer. The rig would
run green, produce a full table, and demonstrate nothing.

**Mitigation (primary, load-bearing):** the DCM's `POST /api/v1/configurations` accepts an
optional `metadata.valid_from` (ISO8601). The seeder **must** backdate it to well before the
generator started:

```
valid_from = (utcnow() - VALID_FROM_BACKDATE_SECONDS).isoformat()   # default 86400 s = 24 h
```

This is also the honest production scenario: a device that came online before anyone registered
its configuration, whose config is valid from the device's birth.

**The seeder must read the config back and assert the stored `valid_from` matches what it sent,
and exit non-zero if it does not** (§6.4 step 5). Without that guard, a DCM that ignores
`metadata.valid_from` turns the rig into a silent no-op.

**Escape hatch (only if the readback assertion fails):** have the generator produce with
`timestamp_ms = now + TIMESTAMP_SKEW_MS`, where `TIMESTAMP_SKEW_MS > SEED_DELAY_SECONDS·1000`.
Future-dating the record makes `valid_from <= record_ts` hold without backdating the config.
This does **not** weaken the test — `grace_ms` is wall-clock (S1), so record timestamps have no
bearing on the grace period; they only decide which config version applies. If the hatch is used,
set the lake sink's `TIMESTAMP_COLUMN` to `produced_ms` instead of `timestamp`, or every row
lands in a future hour partition. Default `TIMESTAMP_SKEW_MS = 0` (hatch off).

---

## 3. Proposed design

A single environment running two **simultaneous** arms of the same application over the same
input topic:

- **Arm "buffered"** — `lookup-sink` with `BUFFER_ENABLED=true`, `state: {enabled: true}`.
- **Arm "control"** — the *same application directory*, second deployment, `BUFFER_ENABLED=`
  (blank ⇒ `buffer=None`), no state block, different consumer group.

Both write to one output topic; a third deployment sinks that topic to the lakehouse. Every
output row carries `arm`, `run_id`, `device_id`, `seq` and `dwell_ms`, so the comparison is a
SQL self-join on `(run_id, device_id, seq)` — the *same record*, seen by both arms, in the same
wall-clock window.

Why simultaneous rather than a redeploy: a redeploy with `BUFFER_ENABLED=false` would run *after*
the seeder had already published every config, so the control arm would resolve everything on the
first attempt and demonstrate nothing. Producing a valid control by redeploy requires deleting the
configs, deleting `config-updates`, and re-running the whole seed cycle — three manual steps that
must not be got wrong, in a rig whose entire value is trustworthiness. Two deployments cost one
extra `quix.yaml` block.

### 3.1 The knob that produces all three outcomes at once

`SEEDED_DEVICE_COUNT < DEVICE_COUNT`. The generator cycles `device-000 … device-099`; the seeder
seeds only `device-000 … device-049`. The other fifty are permanently unresolvable. One run,
one wall-clock window, all three outcomes.

---

## 4. Test design — the three outcomes

### 4.1 Timeline

```
T+0     sync: all deployments start. Generator begins producing to sensor-data
        immediately, with no configuration in existence anywhere.
        Both lookup-sink arms start consuming (AUTO_OFFSET_RESET=latest).
T+0..120  Pre-seed window. Every record is unresolvable.
          buffered arm: withholds all of them.
          control arm:  emits all of them immediately with defaults.
T+120   config-seeder Job wakes, POSTs 50 configs with valid_from = T-24h,
        reads two of them back and asserts valid_from.
        DCM publishes 50 events to config-updates; both arms' lookups ingest them.
T+120..125  Each seeded device's next record (per-device interval 5 s) arrives,
            resolves, and releases that device's whole withheld queue ENRICHED.
            ==> OUTCOME A
T+120..  Records for seeded devices now resolve on the first attempt, dwell ~0.
         ==> OUTCOME A0 (the "config is genuinely reachable" baseline)
T+300..  Unseeded devices' records reach grace_ms and the sweep settles them,
         emitting with defaults.  ==> OUTCOME B
         (first B emissions at ~T+300; steady state thereafter)
T+600   Operator has ≥5 min of steady state in all three outcomes. Run the
        verification queries (§9). Stop the generator.
```

`SEED_DELAY_SECONDS = 120` and `GRACE_MS = 300000` are chosen so that
`SEED_DELAY < GRACE`. That guarantees **every** pre-seed record for a seeded device is still
inside its grace window when its config lands, so outcome A covers the entire pre-seed set with
no ragged edge. The 120 s also buys slack for image build + pod start, so the sink is certainly
consuming before the seeder fires — if the sink started *after* the seed, every record would
resolve immediately and nothing would buffer.

### 4.2 Why this tests the grace period and not something else

`grace_ms` is wall-clock from operator entry (S1). The rig controls exactly that: configuration
does not exist in the world for the first 120 wall-clock seconds of the run. Nothing about the
test depends on manipulating record timestamps — record timestamps appear only in §2.4, and only
to make the *right config version* applicable, which is a different mechanism.

### 4.3 Outcome definitions and their signatures

| Outcome | Arm | Devices | Window | Output-column signature |
|---|---|---|---|---|
| **A** — buffered, then resolved | `buffered` | `device-000`..`device-049` | `ingest_ms < seed_ms` | `resolved = true` **AND** `dwell_ms >= 1000` **AND** `threshold IS NOT NULL` **AND** `region <> 'unknown'` |
| **A0** — resolved first attempt (baseline) | `buffered` | `device-000`..`device-049` | `ingest_ms > seed_ms` | `resolved = true` **AND** `dwell_ms < 100` |
| **B** — buffered, then timed out | `buffered` | `device-050`..`device-099` | any | `resolved = false` **AND** `dwell_ms >= GRACE_MS` **AND** `threshold IS NULL` **AND** `region = 'unknown'` |
| **C** — control, no buffer | `control` | all | any | `dwell_ms < 100` for **every** row, resolved or not |

Expected dwell for outcome A: `(seed_ms − ingest_ms) + (time to the next same-key record)`, i.e.
roughly 5 s to 125 s given a 5 s per-device interval. Always comfortably above the 1000 ms floor.

A0 exists to separate two failure stories that otherwise look identical: "the buffer did not
work" versus "the DCM never delivered the config to this consumer at all". If A0 is empty, the
problem is delivery, not the buffer.

### 4.4 The claim, stated as a falsifiable assertion

> For the set of `(run_id, device_id, seq)` triples where `device_id < device-050` and
> `ingest_ms < seed_ms`, the **control** arm emits `resolved = false, threshold = NULL,
> dwell_ms ≈ 0`, and the **buffered** arm emits `resolved = true, threshold = <the seeded value>,
> dwell_ms > 1000` — **for the same triple**.

That paired row-level comparison, not an aggregate count, is what the rig exists to produce.
Query V3 in §9 returns it directly.

---

## 5. Data & interface contracts

### 5.1 `sensor-data` message (produced by `data-generator`)

Kafka **key**: the `device_id` string, e.g. `device-042`. Serialized as `str` — the consumer
declares `app.topic(name=..., key_deserializer="str")` because the lookup resolves configs by
message key.

```json
{
  "device_id": "device-042",
  "seq": 137,
  "value": 23.7,
  "timestamp": 1789234567890,
  "produced_ms": 1789234567890,
  "run_id": "r1"
}
```

- `seq` — per-device monotonically increasing integer starting at 0. **Load-bearing**: it is the
  join key for the paired A-vs-C comparison, and the only reason a row-level comparison is
  possible at all.
- `timestamp` — epoch ms, absolute. Equals `produced_ms + TIMESTAMP_SKEW_MS` (skew is 0 by
  default; see §2.4 escape hatch). Also set as the Kafka message timestamp.
- `run_id` — carried in the payload so a lake table accumulating several runs stays separable.

### 5.2 DCM configuration document (written by `config-seeder`)

```
POST {DCM_API_URL}/api/v1/configurations
{
  "metadata": {
    "type": "device",
    "target_key": "device-042",
    "valid_from": "2026-09-14T09:00:00+00:00",     // utcnow() - VALID_FROM_BACKDATE_SECONDS
    "category": "pr1110-rig"
  },
  "content": {
    "threshold": 52.0,                              // 10.0 + index, so the value identifies the device
    "region": "ap-south",                           // ["eu-west","us-east","ap-south"][index % 3]
    "device": {"name": "sensor-042"}
  },
  "replace": true
}
```

Config id = `sha1(f"{type}-{target_key}").hexdigest()`. `replace: true` creates **or** versions,
so re-running the Job is idempotent. `PUT /api/v1/configurations/{id}` only updates and 404s on
an unknown id — do not use it.

### 5.3 `enriched-sensor-data` message (produced by both `lookup-sink` arms)

Kafka key: unchanged, the `device_id` (R5 — a released record keeps its own key, which may differ
from the key of the record that triggered the release).

| column | type | set by | purpose |
|---|---|---|---|
| `run_id` | str | env `RUN_ID`, passed through from payload | isolates runs in one lake table |
| `arm` | str | env `ARM` (`buffered` \| `control`) | which deployment emitted it |
| `device_id` | str | generator | key |
| `seq` | int | generator | paired-join key (§4.4) |
| `value` | float | generator | payload |
| `timestamp` | int (epoch ms) | generator | event time; lake `TIMESTAMP_COLUMN` |
| `produced_ms` | int | generator | wall clock at produce |
| `ingest_ms` | int | `apply()` **before** `join_lookup` | wall clock at operator entry — the grace clock's zero |
| `emit_ms` | int | `apply()` **after** `join_lookup` | wall clock at emission |
| `dwell_ms` | int | `emit_ms - ingest_ms` | **the headline measurement** |
| `resolved` | bool | `not value["__unresolved__"]` | did the configuration resolve |
| `unresolved_types` | str | `",".join(value["__unresolved__"])` | which config types missed |
| `threshold` | float \| null | lookup field, `default=None` | enriched value; null ⇒ the default was used |
| `region` | str | lookup field, `default="unknown"` | enriched value; `"unknown"` ⇒ the default was used |
| `seeded_expected` | bool | `int(device_id[-3:]) < SEEDED_DEVICE_COUNT` | a-priori expectation, so verification asserts without consulting the seeder |
| `buffer_enabled` | bool | env | makes each row self-describing |
| `grace_ms` | int | env | makes each row self-describing |
| `on_timeout` | str | env | makes each row self-describing |

**`ingest_ms` must be stamped before the join and must survive the buffer round-trip.** It lives
in the record value, which is what the buffer stores and reads back, so it does. **`emit_ms` must
be stamped after the join**, so each released record gets its own emission time rather than the
triggering record's.

`__unresolved__` is deleted from the value in the post-join `apply()`, after `unresolved_types`
and `resolved` are derived from it. A list-typed column is awkward in the lake, and the raw field
is not needed downstream. `is_resolved` reads it *inside* `join_lookup`, before that cleanup runs.

---

## 6. Pipeline topology

```
                              ┌──────────────┐
                              │  mongodb     │  Service, state 1 GB
                              │  :27017      │  serviceName: mongodb
                              └──────┬───────┘
                                     │ variable group "mongodb-connection"
                                     │ (MONGO_HOST/PORT/USER/PASSWORD)
                                     ▼
  ┌───────────────┐   POST    ┌──────────────────────────┐   events   ┌───────────────┐
  │ config-seeder │──────────▶│ Dynamic Config Manager   │───────────▶│ config-updates│
  │ Job, sleeps   │  /api/v1/ │ Managed, contentStore=   │            │  1 partition  │
  │ 120 s first   │  configs  │ mongo, port 80           │            └───────┬───────┘
  └───────────────┘           └──────────────────────────┘                    │
                                                                              │
  ┌────────────────┐  keyed JSON   ┌──────────────┐                           │
  │ data-generator │──────────────▶│ sensor-data  │                           │
  │ Service,       │  key=device_id│ 4 partitions │                           │
  │ round-robin    │               └──────┬───────┘                           │
  │ 100 devices    │                      │                                   │
  │ 20 msg/s       │          ┌───────────┴────────────┐                      │
  └────────────────┘          │                        │                      │
                              ▼                        ▼                      │
              ┌───────────────────────────┐  ┌──────────────────────┐         │
              │ Lookup Sink - Buffered    │  │ Lookup Sink - Control│◀────────┤
              │ ARM=buffered              │  │ ARM=control          │         │
              │ BUFFER_ENABLED=true       │  │ BUFFER_ENABLED=      │◀────────┘
              │ state: enabled ✔          │  │ (no state block)     │
              └──────────────┬────────────┘  └──────────┬───────────┘
                             │                          │
                             └────────────┬─────────────┘
                                          ▼
                              ┌───────────────────────────┐
                              │  enriched-sensor-data     │
                              │  4 partitions             │
                              └────────────┬──────────────┘
                                           ▼
                              ┌───────────────────────────┐
                              │ Lake Sink                 │  blobStorage.bind: true
                              │ QuixTSDataLakeSink        │  table pr1110_grace_v1
                              └───────────────────────────┘
```

| service | consumes | produces |
|---|---|---|
| `mongodb` | — | — (TCP 27017, internal) |
| Dynamic Config Manager (managed) | HTTP POST from seeder | `config-updates` |
| `data-generator` | — | `sensor-data` |
| `config-seeder` | — | HTTP POST to the DCM |
| `lookup-sink` × 2 | `sensor-data` + `config-updates` | `enriched-sensor-data` |
| `lake-sink` | `enriched-sensor-data` | Iceberg table `pr1110_grace_v1` |

### 6.1 Sink choice

**`QuixTSDataLakeSink`, in a separate `lake-sink` deployment, plus a plain output topic as the
live tap.**

- The rig's claim (§4.4) is a **row-level paired join across two arms over thousands of rows**.
  That is a SQL question. Eyeballing a topic in the Portal cannot answer "for every
  `(device_id, seq)` in the pre-seed window, did arm A resolve where arm C did not" — the
  verification queries in §9 can, in one statement each.
- The lakehouse + DataLake catalogs are deployed and `Running`, cluster-scoped in
  `testrigorg-global`, so `Quix__Lakehouse__Catalog__Url` / `__AuthToken` inject into any
  deployment here carrying `blobStorage: {bind: true}`. This is an available path, not a
  theoretical one.
- **The sink is a separate deployment, not `sdf.sink()` inside `lookup-sink`.** The service under
  test must not carry a `blobStorage` bind: if the bind fails (the testrig Storage Gateway has
  aborted deploys before), the *service under test* would not start and the experiment would be
  blocked by an unrelated dependency. With the split, a broken lake arm leaves `sensor-data` →
  `enriched-sensor-data` fully working and eyeballable in the Portal.
- The output topic is one line (`sdf.to_topic`) and gives a ten-second smoke check that the
  pipeline is alive long before the first parquet flush.
- Sink latency does not contaminate the measurement: `dwell_ms` is computed in-process from
  `ingest_ms`/`emit_ms`, both stamped inside `lookup-sink`, before anything is written.

### 6.2 `contentStore`: `mongo`, not `file`

`contentStore: mongo` (the library-item default) stores configuration content inside the Mongo
document. Chosen because:

- The configs here are <1 KB of JSON, three orders of magnitude under Mongo's 16 MB cap.
- It keeps blob storage off the DCM's critical path. `file` mode makes the Storage Gateway a
  hard dependency of config delivery, i.e. of the thing under test.
- Durability is already provided: `mongodb` runs with `state: {enabled: true, size: 1}` and
  `MONGO_DBPATH=/app/state/mongodb-v2`, so content survives pod replacement. The usual warning
  that `mongo` mode "does not survive a pod replacement" applies to a Mongo with no persistent
  volume, which is not this one.
- Recovery is one click: the seeder is a committed, idempotent Job (`replace: true`).

**The caveat that must be understood before anyone resets Mongo:** `config-updates` outlives the
content store, and the SDK rebuilds config versions from topic events **with no liveness check**.
If Mongo's volume is wiped while `config-updates` still holds events, every lookup resolves to a
version whose content fetch 404s. With `fallback="default"` that returns the field defaults
instead of crashing — which looks **exactly like outcome B** and would silently corrupt the
result. **If Mongo is ever reset, delete and recreate `config-updates` in the same action, and
bump `RUN_ID`.**

### 6.3 Sizing arithmetic

Parameters: `DEVICE_COUNT D = 100`, total rate `R = 20` msg/s (`SLEEP_SECONDS = 0.05`),
`GRACE_MS G = 300` s, `SEED_DELAY S = 120` s, `sensor-data` partitions `P = 4`,
`SEEDED_DEVICE_COUNT = 50`.

| quantity | formula | value | budget | headroom |
|---|---|---|---|---|
| per-device rate | `R / D` | 0.2 msg/s (one every 5 s) | — | — |
| per-key depth, unseeded | `(R/D) · G` | **60 records** | `RELEASE_WARN_RECORDS = 1000` | 16× |
| per-key depth, seeded (pre-seed) | `(R/D) · S` | **24 records** | 1000 | 42× |
| peak total held | `D · 60` | ~6 000 records | `max_buffered_per_key = 10 000` per key | vast |
| peak state size | 6 000 × ~300 B | ~1.8 MB | `state.size: 1` (GB) | vast |
| full sweep cycle | `D / (4·R)` | **1.25 s** | — | — |
| outcome-B settle lag | `G + cycle` | ~301.3 s | — | — |
| sweep emit demand | `R·(D−seeded)/D ÷ R` | 0.5 records per incoming record | `SWEEP_EMIT_BUDGET = 256` | 512× |
| keys per partition | `D / P` | 25 | — | — |

Per-partition check for R4: partition `p` receives `R/P = 5` records/s, each settling
`SWEEP_BUDGET = 4` key-slots ⇒ 20 key-slot visits/s over 25 keys ⇒ 1.25 s cycle, matching the
partition-independent formula. Every partition carries traffic because round-robin over 100 keys
hashes across all four.

Release cost check for R6: the largest single release is a seeded device's 24 withheld records
deserialized in one callback — 2.4% of the warn threshold.

### 6.4 Repo layout

```
C:\repos\quixstreams-tests\
  quix.yaml
  README.md
  .env                     (exists, gitignored — operator pastes Quix__Pat__Token)
  .gitignore               (exists; .tmp/ added)
  dev-planning\pr1110-grace-period\spec.md      <- this file
  mongodb\                 dockerfile, init.sh, app.yaml
  data-generator\          main.py, requirements.txt, dockerfile, app.yaml, README.md
  config-seeder\           main.py, requirements.txt, dockerfile, app.yaml, README.md
  lookup-sink\             main.py, requirements.txt, dockerfile, app.yaml, README.md
  lake-sink\               main.py, requirements.txt, dockerfile, app.yaml, README.md
  tools\negative-tests\    test_negative.py, requirements.txt, README.md   (local only,
                                                                            not a deployment)
```

One `main.py` per service. None is near 300 lines of code; do not add helper modules.
`tools/` is not referenced by `quix.yaml` and is therefore not an app.

### 6.5 Common `requirements.txt` / `dockerfile`

Pin the 40-char SHA, never the branch name `feature/sc-72821/...` — a moving ref silently ships a
different library on any rebuild, and every deploy rebuilds the image.

```
quixstreams @ git+https://github.com/quixio/quix-streams.git@6632b46cdb2d493e4facf6c00b78c608ae70af87
```

`lake-sink` adds the extra (same SHA, so there is exactly one library version in the pipeline):

```
quixstreams[quixdatalake] @ git+https://github.com/quixio/quix-streams.git@6632b46cdb2d493e4facf6c00b78c608ae70af87
```

`config-seeder` adds `requests==2.32.3`. Never hand-list `pandas`/`pyarrow` — the
`[quixdatalake]` extra carries the version floors.

Because requirements name a git ref, the dockerfile needs `git`:

```dockerfile
FROM python:3.13-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONIOENCODING=UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ./requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT ["python3", "main.py"]
```

Identical in `data-generator`, `config-seeder`, `lookup-sink`, `lake-sink`. `mongodb` has its own
(§7.1).

---

## 7. Per-service specifications

### 7.1 `mongodb/`

Backing store for the DCM. **Copy `dockerfile`, `init.sh` and `app.yaml` verbatim** from
`C:\repos\comma-car-segments-ingest\mongodb\`. Do not rewrite `init.sh` — it solves the
chown/gosu problem (gosu execs mongod as PID 1 so SIGTERM reaches it and WiredTiger checkpoints
cleanly; `su -c` leaves a shell in between and every stop becomes an unclean shutdown, which is
how a previous dbpath was destroyed).

Files: `dockerfile` (`FROM mongo:8.0.21`, `EXPOSE 27017`, `ENTRYPOINT ["/init.sh"]`), `init.sh`,
`app.yaml`.

| name | inputType | value | description |
|---|---|---|---|
| `MONGO_INITDB_ROOT_USERNAME` | FreeText | `admin` | Root username MongoDB is initialised with. Must match the `MONGO_USER` member of the `mongodb-connection` variable group. |
| `MONGO_INITDB_ROOT_PASSWORD` | Secret | `mongo_password` | Root password. Must match the group's `MONGO_PASSWORD` member. |
| `MONGO_DBPATH` | FreeText | `/app/state/mongodb-v2` | Absolute mongod data directory, required to be inside `/app/state`. Bump to an unused sibling (`mongodb-v3`) to force a clean database with no shell in the container; the entry point never deletes the old directory. |

`quix.yaml` block:

```yaml
  - name: MongoDB
    application: mongodb
    version: latest
    deploymentType: Service
    resources:
      limits:
        cpu: 200
        memory: 800
    state:
      enabled: true
      size: 1
    network:
      serviceName: mongodb
      ports:
        - port: 27017
          targetPort: 27017
    variables:
      - name: MONGO_INITDB_ROOT_USERNAME
        inputType: FreeText
        required: true
        value: admin
      - name: MONGO_INITDB_ROOT_PASSWORD
        inputType: Secret
        required: true
        value: mongo_password
      - name: MONGO_DBPATH
        inputType: FreeText
        required: true
        value: /app/state/mongodb-v2
```

`serviceName: mongodb` is what makes `MONGO_HOST=mongodb` resolve for the DCM.

### 7.2 Dynamic Configuration Manager — managed deployment

No repo directory. `libraryItemId: dynamic-configuration`, `DeploymentType: Managed`,
500 mCPU / 1000 MB / 1 replica, `PublicAccess: false`, `ValidateConnection: false`.

**The live Portal contract differs from the sample README** — use these names:

| name | inputType | value | description |
|---|---|---|---|
| `topic` | OutputTopic | `config-updates` | Topic the DCM publishes configuration change events to. Consumed by `QuixConfigurationService` in both `lookup-sink` arms. |
| `consumerGroup` | FreeText | `config-api-v1` | DCM's own Kafka consumer group. Library default; left as-is. |
| `mongoConnectionGroup` | **VariableGroup** | `mongodb-connection` | **Required, no default.** Reference to an org-level variable group, not a free-text string. Supplies `MONGO_HOST` / `MONGO_PORT` / `MONGO_USER` / `MONGO_PASSWORD`. The group must exist and be assigned to the workspace before this deploys — see §8 step 1. |
| `mongoDatabase` | FreeText | `dcm` | Mongo database holding configuration documents. |
| `mongoCollection` | FreeText | `configurations` | Mongo collection holding configuration documents. |
| `workers` | FreeText | `1` | API worker processes. One is ample for 50 POSTs. |
| `port` | FreeText | `80` | API port. `DCM_API_URL` in `config-seeder` must agree with this. |
| `contentStore` | FreeText | `mongo` | Content storage backend. See §6.2 for why `mongo` and not `file`, and for the wipe caveat. |

**Do not hand-write this `quix.yaml` block.** Managed library items encode platform-side keys
(`libraryItemId` vs `application`, connector metadata) that are easy to get subtly wrong and that
fail at sync rather than at review. Create the DCM once through the Portal
(**Connectors → Add connector → Dynamic Configuration Manager**), then `git pull` the block the
Portal writes back into `quix.yaml` and commit it. Expected shape, for review only:

```yaml
  - name: Dynamic Configuration Manager
    libraryItemId: dynamic-configuration
    deploymentType: Service
    version: latest
    resources:
      limits:
        cpu: 500
        memory: 1000
      replicas: 1
    variables:
      - name: topic
        inputType: OutputTopic
        required: true
        value: config-updates
      - name: mongoConnectionGroup
        inputType: VariableGroup
        required: true
        value: mongodb-connection
      - name: mongoDatabase
        inputType: FreeText
        required: true
        value: dcm
      - name: mongoCollection
        inputType: FreeText
        required: true
        value: configurations
      - name: contentStore
        inputType: FreeText
        value: mongo
      - name: consumerGroup
        inputType: FreeText
        value: config-api-v1
      - name: workers
        inputType: FreeText
        value: "1"
      - name: port
        inputType: FreeText
        value: "80"
```

**Reachability of the API from `config-seeder` is an open question** — see §11 Q1. Enable
`publicAccess` with `urlPrefix: dcm` regardless: the DCM's embedded UI is the fastest way for the
operator to confirm by eye that 50 configs exist with the backdated `valid_from`.

### 7.3 `data-generator/`

Produces keyed JSON to `sensor-data`, **starting immediately, before any configuration exists** —
that is the whole point — and **running forever** (R1).

`main.py` responsibilities:

1. `Application()`; `output_topic = app.topic(name=os.environ["output"], key_serializer="str")`.
2. A `quixstreams.sources.Source` subclass whose `run()` loops while `self.running`:
   round-robin `i = n % DEVICE_COUNT`, `device_id = f"device-{i:03d}"`, increment that device's
   `seq`, build the §5.1 payload, `self.serialize(key=device_id, value=payload,
   timestamp_ms=produced_ms + TIMESTAMP_SKEW_MS)`, `self.produce(...)`, `sleep(SLEEP_SECONDS)`.
   Round-robin, not random: it guarantees every key is revisited every `DEVICE_COUNT ×
   SLEEP_SECONDS` seconds, which is what triggers outcome A's releases (R2). Random selection
   over 100 keys has a long tail and would leave some devices' buffers unreleased for tens of
   seconds.
3. `app.add_source(source, topic=output_topic)`; `app.run()`.
4. Log one line every `LOG_EVERY` messages: count, current device, per-device interval.

Use the `Source` primitive rather than `app.get_producer()`: it is the native QuixStreams
producer idiom and it is what gives per-message `timestamp_ms` control for the §2.4 escape hatch.
Verify the exact `Source` method signatures against the pinned SHA (step 3 of
`quix-service-create`) before writing.

| name | inputType | value | description |
|---|---|---|---|
| `output` | OutputTopic | `sensor-data` | Topic to produce keyed sensor records to. Read with `os.environ["output"]`, never `.get()` with a fallback — a hardcoded fallback boots fine but leaves the Portal pipeline graph with no edge. |
| `DEVICE_COUNT` | FreeText | `100` | Size of the key space. Devices are `device-000`..`device-(N-1)`, cycled round-robin. Raising it lengthens the sweep cycle (`D/(4R)` s) and delays outcome B; see §6.3. |
| `SEEDED_DEVICE_COUNT` | FreeText | `50` | Informational here — the generator does not seed. Stamped onto each record's expectation flag downstream. **Must equal the seeder's value**, or `seeded_expected` lies. |
| `SLEEP_SECONDS` | FreeText | `0.05` | Delay between messages. 0.05 = 20 msg/s total = one record per device every 5 s. Lowering it raises per-key buffer depth toward `RELEASE_WARN_RECORDS`; see §6.3. |
| `TIMESTAMP_SKEW_MS` | FreeText | `0` | Milliseconds added to the Kafka message timestamp. **Escape hatch only** (§2.4): set above `SEED_DELAY_SECONDS·1000` if the DCM ignores `metadata.valid_from`. 0 = off. |
| `RUN_ID` | FreeText | `r1` | Opaque run label stamped into every record. Bump for every fresh run so one lake table holds several runs separably. |
| `LOG_EVERY` | FreeText | `200` | Messages between progress log lines. |

```yaml
  - name: Data Generator
    application: data-generator
    version: latest
    deploymentType: Service
    resources:
      limits:
        cpu: 200
        memory: 300
    desiredStatus: Running
    variables:
      - name: output
        inputType: OutputTopic
        required: true
        value: sensor-data
      - name: DEVICE_COUNT
        inputType: FreeText
        value: "100"
      - name: SEEDED_DEVICE_COUNT
        inputType: FreeText
        value: "50"
      - name: SLEEP_SECONDS
        inputType: FreeText
        value: "0.05"
      - name: TIMESTAMP_SKEW_MS
        inputType: FreeText
        value: "0"
      - name: RUN_ID
        inputType: FreeText
        value: r1
      - name: LOG_EVERY
        inputType: FreeText
        value: "200"
```

### 7.4 `config-seeder/`

`deploymentType: Job`. The instrument that makes the grace period observable.

`main.py` responsibilities:

1. Read env. Log the full plan before sleeping: delay, device range to seed, `valid_from`,
   target URL.
2. Sleep `SEED_DELAY_SECONDS`, logging a heartbeat every 30 s so the operator can see it is
   alive and can time the run against it.
3. Compute `valid_from = (datetime.now(timezone.utc) - timedelta(seconds=VALID_FROM_BACKDATE_SECONDS)).isoformat()`.
4. For `i` in `0 .. SEEDED_DEVICE_COUNT-1`: `POST {DCM_API_URL}/api/v1/configurations` with the
   §5.2 body, `target_key = f"device-{i:03d}"`, `threshold = 10.0 + i`,
   `region = ["eu-west","us-east","ap-south"][i % 3]`. Send
   `Authorization: Bearer {DCM_API_TOKEN}` only when that variable is non-empty. Raise on any
   non-2xx.
5. **Readback assertion (load-bearing, §2.4).** `GET /api/v1/configurations/{sha1(f"device-device-000")}`
   and the same for the last seeded key. Log the returned `valid_from` and `version`. **If the
   stored `valid_from` does not match what was sent, log the discrepancy loudly, print the
   escape-hatch instruction (`set TIMESTAMP_SKEW_MS`), and `sys.exit(1)`.** A DCM that silently
   ignores `metadata.valid_from` turns the entire rig into a no-op that still produces a
   full-looking table.
6. Print the §9 sanity table and exit 0.

No Kafka client, no polling loop, no config cache — the DCM's REST API is the only interface this
service has, and it is a plain HTTP POST.

| name | inputType | value | description |
|---|---|---|---|
| `DCM_API_URL` | FreeText | `http://dcm` | Base URL of the Dynamic Configuration Manager REST API, no trailing slash. Internal service DNS if the managed deployment exposes one, otherwise the `publicAccess` URL — see §11 Q1. |
| `DCM_API_TOKEN` | Secret | *(blank)* | Bearer token for the DCM API. Required only when reaching it over the public URL; leave blank for internal DNS. |
| `SEED_DELAY_SECONDS` | FreeText | `120` | Wall-clock seconds to wait before POSTing anything. **This is the instrument.** It must be long enough for both `lookup-sink` arms to be consuming before the configs land (otherwise every record resolves on the first attempt and nothing buffers) and shorter than `GRACE_MS/1000` (otherwise the earliest pre-seed records time out before their config arrives and outcome A gets a ragged edge). |
| `DEVICE_COUNT` | FreeText | `100` | Total key space. Must equal the generator's value. |
| `SEEDED_DEVICE_COUNT` | FreeText | `50` | How many of the devices get a configuration, from `device-000` upward. The rest are permanently unresolvable and produce outcome B. **This single knob yields all three outcomes in one run.** |
| `CONFIG_TYPE` | FreeText | `device` | DCM configuration `type`. Must equal the `type=` passed to every `json_field` in `lookup-sink` — the type is fixed at pipeline-build time and the lookup will not check it. |
| `VALID_FROM_BACKDATE_SECONDS` | FreeText | `86400` | Seconds to backdate `metadata.valid_from`. **Load-bearing**: `find_valid_version()` matches versions whose `valid_from <= record timestamp`, so a config stamped "now" can never apply to a record produced a minute ago. 24 h is far more than any plausible run length. See §2.4. |
| `RUN_ID` | FreeText | `r1` | Run label; written into `metadata.category` and logged, so the DCM UI shows which run seeded a config. |

```yaml
  - name: Config Seeder
    application: config-seeder
    version: latest
    deploymentType: Job
    resources:
      limits:
        cpu: 200
        memory: 300
    variables:
      - name: DCM_API_URL
        inputType: FreeText
        required: true
        value: http://dcm
      - name: DCM_API_TOKEN
        inputType: Secret
        value: ""
      - name: SEED_DELAY_SECONDS
        inputType: FreeText
        value: "120"
      - name: DEVICE_COUNT
        inputType: FreeText
        value: "100"
      - name: SEEDED_DEVICE_COUNT
        inputType: FreeText
        value: "50"
      - name: CONFIG_TYPE
        inputType: FreeText
        value: device
      - name: VALID_FROM_BACKDATE_SECONDS
        inputType: FreeText
        value: "86400"
      - name: RUN_ID
        inputType: FreeText
        value: r1
```

A Job block carries no `desiredStatus`, `state` or `network`. Only a **newly created** Job
auto-runs on sync; converting an existing Service to a Job leaves it Stopped. To re-seed, restart
the Job from the Portal — `replace: true` makes it idempotent.

### 7.5 `lookup-sink/` — the service under test

**One application directory, two deployments.** Every `LookupBuffer` parameter is an env var so
the operator can retune from the Portal without a rebuild.

`main.py` responsibilities:

1. `app = Application(consumer_group=os.environ["CONSUMER_GROUP"], auto_offset_reset=os.environ["AUTO_OFFSET_RESET"])`.
2. `data_topic = app.topic(name=os.environ["input"], key_deserializer="str")` — **mandatory**:
   the lookup resolves configs by message key, and without `key_deserializer="str"` the key is
   bytes and never matches a `target_key`.
   `config_topic = app.topic(name=os.environ["config_topic"])`,
   `output_topic = app.topic(name=os.environ["output"])`.
3. ```python
   lookup = QuixConfigurationService(
       topic=config_topic,
       app_config=app.config,
       fallback="default",
       unresolved_types_field="__unresolved__",
   )
   ```
   `fallback="default"` is **not optional**. The SDK default is `"error"`, which re-raises inside
   `join()` and kills the application when content cannot be fetched. A per-field `default=` does
   not cover this: `default` is consulted when the config or content is *absent*, never when the
   HTTP call throws.
4. ```python
   fields = {
       "threshold": lookup.json_field("$.threshold", type=CONFIG_TYPE, default=None),
       "region":    lookup.json_field("$.region",    type=CONFIG_TYPE, default="unknown"),
   }
   ```
   Both `default=` are mandatory under a buffer (S4). Two narrow-leaf fields, never
   `jsonpath="$"` — a whole-document field deep-copies the config on every message.
5. `sdf = app.dataframe(data_topic)`, then `sdf = sdf.apply(stamp_ingest)` which sets
   `ingest_ms = int(time.time()*1000)`. **Before** the join.
6. Build the buffer, or not:
   ```python
   buffer = None
   if os.environ.get("BUFFER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
       buffer = LookupBuffer(
           grace_ms=int(os.environ["GRACE_MS"]),
           is_resolved=lambda value: not value["__unresolved__"],
           on_timeout=os.environ["ON_TIMEOUT"],
           max_buffered_per_key=int(os.environ["MAX_BUFFERED_PER_KEY"]),
           on_overflow=os.environ["ON_OVERFLOW"],
           store_name=os.environ["STORE_NAME"],
       )
   sdf = sdf.join_lookup(lookup, fields, on="device_id", buffer=buffer)
   ```
   Blank / `false` / `0` / `no` ⇒ `buffer=None`, which is exactly today's behaviour (S8) and is
   the control arm.
7. `sdf = sdf.apply(stamp_emit)` — **after** the join. Sets `emit_ms`, `dwell_ms = emit_ms -
   ingest_ms`, `resolved = not value["__unresolved__"]`,
   `unresolved_types = ",".join(value["__unresolved__"])`, `seeded_expected`,
   `arm` / `buffer_enabled` / `grace_ms` / `on_timeout` from env, then `del
   value["__unresolved__"]`. Produces the §5.3 row.
8. `sdf.to_topic(output_topic)`; `app.run()`.

Do not assume 1:1 input-to-output (R5): a single incoming record can drive up to
`SWEEP_EMIT_BUDGET = 256` sweep emissions plus a same-key release queue, each with its own key
and timestamp. `to_topic` handles this natively; nothing in `main.py` may count outputs against
inputs.

| name | inputType | value (buffered arm) | value (control arm) | description |
|---|---|---|---|---|
| `input` | InputTopic | `sensor-data` | `sensor-data` | Keyed sensor records. Both arms read the same topic so the comparison is paired. |
| `config_topic` | InputTopic | `config-updates` | `config-updates` | DCM change events, consumed by `QuixConfigurationService`. The lookup **assigns** all partitions from offset 0 on every start, bypassing the consumer-group protocol, so it always rebuilds the full config history. |
| `output` | OutputTopic | `enriched-sensor-data` | `enriched-sensor-data` | Enriched records from both arms, separated by the `arm` column. |
| `ARM` | FreeText | `buffered` | `control` | Label stamped on every emitted row. The verification queries slice on it. |
| `BUFFER_ENABLED` | FreeText | `true` | *(blank)* | `true`/`1`/`yes`/`on` builds a `LookupBuffer`; anything else (including blank) passes `buffer=None`, which is today's unbuffered behaviour. **Blank is the control arm.** |
| `GRACE_MS` | FreeText | `300000` | `300000` | Wall-clock milliseconds a record may wait for its configuration, measured from operator entry. Must exceed `SEED_DELAY_SECONDS·1000` (§4.1). Ignored when `BUFFER_ENABLED` is off; kept on the control arm only so the column is populated identically. |
| `ON_TIMEOUT` | FreeText | `emit` | `emit` | `emit` releases the record with each field's `default=`; `drop` discards it with a rate-limited warning. Flip to `drop` for variant run V-DROP (§10). |
| `MAX_BUFFERED_PER_KEY` | FreeText | `10000` | `10000` | Per-key buffer cap. Steady state here is 60 (§6.3). Drop to `5` for the overflow variants NT3a/NT3b. |
| `ON_OVERFLOW` | FreeText | `drop-newest` | `drop-newest` | `drop-newest` silently discards new records once the cap is hit; `raise` throws `LookupBufferOverflowError` and kills the app. |
| `STORE_NAME` | FreeText | `lookup-buffer` | `lookup-buffer` | Name of the changelog-backed timestamped state store holding withheld records. Bump it to force a clean store without touching the consumer group. |
| `CONFIG_TYPE` | FreeText | `device` | `device` | Configuration `type` passed to every `json_field`. Fixed at pipeline-build time; must equal the seeder's `CONFIG_TYPE`. |
| `SEEDED_DEVICE_COUNT` | FreeText | `50` | `50` | Used only to compute `seeded_expected` on each row, so verification asserts against an a-priori expectation rather than against the seeder's own output. |
| `CONSUMER_GROUP` | FreeText | `pr1110_buffered_r1` | `pr1110_control_r1` | Kafka consumer group. **Must differ between arms** or they split the partitions instead of both seeing every record. Bump the `_r1` suffix in step with `RUN_ID` for a clean run; it also gives the buffer a fresh changelog. |
| `AUTO_OFFSET_RESET` | FreeText | `latest` | `latest` | `latest`, so a restart resumes at the live edge. With `earliest`, a restart after seeding would re-drain the whole backlog into a world where every config already exists — every record would resolve on the first attempt and the replayed rows would contaminate the table. |
| `LOGLEVEL` | FreeText | `INFO` | `INFO` | `DEBUG` surfaces the lookup's per-key "No configuration found" lines and the buffer's release/overflow warnings. |

```yaml
  - name: Lookup Sink - Buffered
    application: lookup-sink
    version: latest
    deploymentType: Service
    resources:
      limits:
        cpu: 500
        memory: 1000
    state:
      enabled: true
      size: 1
    desiredStatus: Running
    variables:
      - name: input
        inputType: InputTopic
        required: true
        value: sensor-data
      - name: config_topic
        inputType: InputTopic
        required: true
        value: config-updates
      - name: output
        inputType: OutputTopic
        required: true
        value: enriched-sensor-data
      - name: ARM
        inputType: FreeText
        value: buffered
      - name: BUFFER_ENABLED
        inputType: FreeText
        value: "true"
      - name: GRACE_MS
        inputType: FreeText
        value: "300000"
      - name: ON_TIMEOUT
        inputType: FreeText
        value: emit
      - name: MAX_BUFFERED_PER_KEY
        inputType: FreeText
        value: "10000"
      - name: ON_OVERFLOW
        inputType: FreeText
        value: drop-newest
      - name: STORE_NAME
        inputType: FreeText
        value: lookup-buffer
      - name: CONFIG_TYPE
        inputType: FreeText
        value: device
      - name: SEEDED_DEVICE_COUNT
        inputType: FreeText
        value: "50"
      - name: CONSUMER_GROUP
        inputType: FreeText
        value: pr1110_buffered_r1
      - name: AUTO_OFFSET_RESET
        inputType: FreeText
        value: latest
      - name: LOGLEVEL
        inputType: FreeText
        value: INFO

  - name: Lookup Sink - Control
    application: lookup-sink
    version: latest
    deploymentType: Service
    resources:
      limits:
        cpu: 300
        memory: 500
    desiredStatus: Running
    variables:
      # identical to the buffered block except for these four:
      - name: ARM
        inputType: FreeText
        value: control
      - name: BUFFER_ENABLED
        inputType: FreeText
        value: ""
      - name: CONSUMER_GROUP
        inputType: FreeText
        value: pr1110_control_r1
      # ... and NO `state:` block at all.
```

**The control arm deliberately has no `state:` block.** Since `buffer=None` registers no store,
the deployment must run without one; if it fails at startup demanding state, that is itself a
finding (the buffer would be registering a store when disabled).

### 7.6 `lake-sink/`

Copy `C:\repos\TTL_test_environment\lake-sink\` essentially verbatim. Only variable *values*
change. Never hand-roll a parquet writer or catalog client — `QuixTSDataLakeSink` is the
sanctioned path.

```python
from quixstreams.sinks.core.quix_ts_datalake_sink import QuixTSDataLakeSink
sink = QuixTSDataLakeSink(
    s3_prefix="data-lake/time-series",
    table_name=f"{TABLE_NAME}_{TABLE_VERSION}",
    workspace_id=os.getenv("Quix__Workspace__Id", ""),
    hive_columns=hive_columns,
    timestamp_column=os.environ["TIMESTAMP_COLUMN"],
    catalog_url=os.getenv("Quix__Lakehouse__Catalog__Url") or os.getenv("CATALOG_URL"),
    catalog_auth_token=os.getenv("Quix__Lakehouse__Catalog__AuthToken"),
    auto_discover=True, namespace=os.getenv("CATALOG_NAMESPACE", "default"),
    auto_create_bucket=True, max_workers=int(os.environ["MAX_WRITE_WORKERS"]),
    on_client_connect_success=..., on_client_connect_failure=...,
)
sdf.sink(sink)
```

| name | inputType | value | description |
|---|---|---|---|
| `input` | InputTopic | `enriched-sensor-data` | Enriched records from both `lookup-sink` arms. |
| `TABLE_NAME` | FreeText | `pr1110_grace` | Base table name. Combined with `TABLE_VERSION` into the effective name. |
| `TABLE_VERSION` | FreeText | `v1` | Suffix appended to `TABLE_NAME`. **Bump this whenever `HIVE_COLUMNS`, `TIMESTAMP_COLUMN` or `SORT_COLUMN` changes** — `hive_columns` is immutable once the table registers, the sink validates the partition set against the catalog spec at startup and raises on a mismatch, and table properties are written only at CREATE. A layout change is a migration, not a tweak. |
| `HIVE_COLUMNS` | FreeText | `year,month,day,hour,arm` | Hive partition columns. `year/month/day/hour` derive from `TIMESTAMP_COLUMN`; `arm` is the primary analysis slice and has cardinality 2. `run_id` is deliberately *not* partitioned — it is low-volume and its cardinality grows with every run, which would shatter the table into tiny parquet files. |
| `TIMESTAMP_COLUMN` | FreeText | `timestamp` | Absolute epoch-ms event time from the generator. **If the §2.4 escape hatch is used (`TIMESTAMP_SKEW_MS > 0`), change this to `produced_ms`**, or every row lands in a future hour partition. |
| `SORT_COLUMN` | FreeText | `emit_ms` | Recorded as `properties.sort_column` at table creation; lakehouse compaction orders files by it. Emission order is the natural read order for this table. |
| `STATS_COLUMNS` | FreeText | *(blank)* | Blank = per-file min/max for every numeric and timestamp column, which here includes `dwell_ms` — exactly the column the verification queries prune on. |
| `AUTO_DISCOVER` | FreeText | `true` | Register the table in the REST catalog on first write. `false` leaves `partition_spec`, `sort_column` and `column_stats` unrecorded. |
| `CATALOG_NAMESPACE` | FreeText | `default` | Catalog namespace for table registration. |
| `BATCH_SIZE` | FreeText | `1000` | Input messages batched before a write. At 40 rows/s (two arms × 20) this is ~25 s of data. |
| `COMMIT_INTERVAL` | FreeText | `30` | Seconds between commits; also the flush cadence. With `BATCH_SIZE` it sets parquet file size and bounds how stale a query can be to ~30 s. |
| `CONSUMER_GROUP` | FreeText | `pr1110_lake_v1` | Kafka consumer group. Bump it to re-sink the topic from `AUTO_OFFSET_RESET`; pair with a `TABLE_VERSION` bump to avoid duplicate rows. |
| `AUTO_OFFSET_RESET` | FreeText | `earliest` | `earliest`, so the table holds the run from its first record even if this deployment starts late. Unlike `lookup-sink`, replay here is harmless — the rows are already stamped. |
| `MAX_WRITE_WORKERS` | FreeText | `10` | Parallel file writes. |
| `LOGLEVEL` | FreeText | `INFO` | `DEBUG` logs each uploaded data file and sidecar. |

```yaml
  - name: Lake Sink
    application: lake-sink
    version: latest
    deploymentType: Service
    resources:
      limits:
        cpu: 500
        memory: 1000
    desiredStatus: Running
    variables:
      # ... all of the above, name / inputType / value ...
    blobStorage:
      bind: true
```

`blobStorage: {bind: true}` is what injects `Quix__Lakehouse__Catalog__Url` and
`Quix__Lakehouse__Catalog__AuthToken`; credentials arrive via
`Quix__BlobStorage__Connection__Json` and are never passed explicitly.

---

## 8. `quix.yaml` topics

```yaml
topics:
  # Keyed sensor records. 4 partitions: both lookup-sink arms are single-replica so
  # one consumer takes all 4, but the buffer's sweep cursor is PER-PARTITION (R4), so
  # >1 partition is what exercises the round-robin across partitions at all. 4 also
  # leaves room to raise replicas in Phase 2 without recreating the topic - a
  # 1-partition topic can only ever have one consumer, no matter how many replicas
  # are added. Round-robin over 100 device keys hashes across all four, so no
  # partition is ever starved of the traffic that drives its own sweep.
  - name: sensor-data
    configuration:
      partitions: 4
      retentionInMinutes: 1440

  # DCM configuration change events. 1 partition: ordering of version events for a
  # config must be total, the volume is 50 messages per run, and the lookup ASSIGNS
  # every partition from offset 0 on each start regardless. Retention 30 d, not the
  # 1 d default: the SDK rebuilds the whole config version history from this topic on
  # every restart, so truncating it silently un-configures every device.
  - name: config-updates
    configuration:
      partitions: 1
      retentionInMinutes: 43200

  # Enriched output from both arms, distinguished by the `arm` column. 4 partitions to
  # match sensor-data so the lake sink can be scaled out later.
  - name: enriched-sensor-data
    configuration:
      partitions: 4
      retentionInMinutes: 1440
```

The buffer's changelog topic (`changelog__pr1110_buffered_r1--lookup-buffer` or similar) is
created automatically by QuixStreams and must **not** be declared here. Bumping `CONSUMER_GROUP`
or `STORE_NAME` yields a fresh one.

---

## 9. Deployment procedure

**Step 1 — Create the `mongodb-connection` variable group. This blocks everything else.**
The environment currently has zero variable groups (`GET /organisations/variable-groups` → `[]`),
and `mongoConnectionGroup` is required with no default, so the DCM cannot be deployed at all
until this exists. The operator does this by hand.

`POST /organisations/variable-groups` with `id`, `displayName`, `description`, `valueSets`:

| member | value | notes |
|---|---|---|
| `MONGO_HOST` | `mongodb` | The k8s service name from `network.serviceName` in §7.1. **Host only, no port** — the DCM takes them separately. |
| `MONGO_PORT` | `27017` | |
| `MONGO_USER` | `admin` | Must equal `MONGO_INITDB_ROOT_USERNAME`. |
| `MONGO_PASSWORD` | `mongo_password` | Secret. Must equal `MONGO_INITDB_ROOT_PASSWORD`. |

```
id:          mongodb-connection
displayName: MongoDB Connection
description: Connection for the Dynamic Configuration Manager's content store.
```

Then `POST /organisations/variable-groups/mongodb-connection/assignments` to bind it to workspace
`testrigorg-quixstreamstests-quixstr-df57e12b`. Verify with a re-read before proceeding.

**Step 2 — Paste the PAT.** `Quix__Pat__Token` in `C:\repos\quixstreams-tests\.env` is currently
blank. `.env` is gitignored.

**Step 3 — Commit and push everything.** Quix builds from the pushed repo on branch `dev`; local
edits do nothing. `quix.yaml` must be in the same commit as the app directories — app-only commits
are rejected with "Reference should have affected the workspace descriptor".

**Step 4 — `POST /workspaces/testrigorg-quixstreamstests-quixstr-df57e12b/pull`, then sync.**
Without the pull, the sync compares the old commit to itself and no-ops.

**Step 5 — Deploy MongoDB alone and wait for Running.** `serviceName: mongodb` must resolve
before the DCM's first connection attempt.

**Step 6 — Create the DCM through the Portal** (Connectors → Add connector → Dynamic
Configuration Manager) with the §7.2 values. Then `git pull` the `quix.yaml` block the Portal
wrote and commit it, so the descriptor is reproducible. Confirm the DCM reaches Running and its
UI lists zero configurations.

**Portal race warning:** editing any deployment in the Portal UI commits a lossy rewrite of
`quix.yaml` to `dev`, stripping descriptions and `inputType`. After step 6, `git fetch` + rebase
before pushing anything, and expect to restore whole blocks rather than patch lines.

**Step 7 — Deploy `Lookup Sink - Buffered`, `Lookup Sink - Control` and `Lake Sink`. Wait for all
three to reach Running.** They must be consuming before any data exists, and certainly before the
seeder fires.

**Step 8 — Deploy `Data Generator` and `Config Seeder` together.** Note the wall-clock minute;
this is T+0. The generator starts producing immediately into a world with no configuration. The
seeder sleeps 120 s, then seeds.

**Step 9 — Watch the seeder's log for the readback assertion.** If it exits non-zero on a
`valid_from` mismatch, stop: apply the §2.4 escape hatch (`TIMESTAMP_SKEW_MS = 300000` on the
generator), bump `RUN_ID` and all three `CONSUMER_GROUP`s, and restart from step 8.

**Step 10 — Let it run 10 minutes past T+0**, then run the §10 verification. Stop the Data
Generator when finished; stopping it earlier freezes every still-buffered record in place (R1).

---

## 10. Verification procedure

`<T>` = `pr1110_grace_v1`, `<R>` = the `RUN_ID`, `<G>` = `GRACE_MS` (300000).

### V0 — Liveness, before waiting for parquet

Open `enriched-sensor-data` in the Portal and confirm rows are flowing with both
`"arm":"buffered"` and `"arm":"control"`. Takes ten seconds and rules out the whole class of
"nothing is running" failures before anyone waits for a flush.

### V1 — Outcome census

```sql
SELECT arm,
       CASE WHEN device_id < 'device-050' THEN 'seeded' ELSE 'unseeded' END AS cohort,
       resolved,
       COUNT(*)                        AS rows,
       MIN(dwell_ms)                   AS dwell_min,
       ROUND(AVG(dwell_ms))            AS dwell_avg,
       MAX(dwell_ms)                   AS dwell_max
FROM <T>
WHERE run_id = '<R>'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

Expected, after ~10 minutes:

| arm | cohort | resolved | rows | dwell_min | dwell_avg | dwell_max | = |
|---|---|---|---|---|---|---|---|
| buffered | seeded | true | ~6 000 | ~0 | — | 120 000–130 000 | **A + A0** |
| buffered | seeded | false | **0** | — | — | — | *must be zero* |
| buffered | unseeded | false | ~3 500 | **≥ 300 000** | ~301 000 | ~302 000 | **B** |
| buffered | unseeded | true | **0** | — | — | — | *must be zero* |
| control | seeded | true | ~4 800 | ~0 | < 100 | < 100 | post-seed |
| control | seeded | false | ~1 200 | ~0 | < 100 | < 100 | pre-seed, **defaults** |
| control | unseeded | false | ~6 000 | ~0 | < 100 | < 100 | **C** |

The three assertions: `buffered/unseeded` `dwell_min ≥ <G>`; `control` `dwell_max < 100` in every
row; both "must be zero" rows empty.

### V2 — Outcome A exists at all (the headline)

```sql
SELECT COUNT(*) AS outcome_a_rows,
       MIN(dwell_ms) AS min_dwell, MAX(dwell_ms) AS max_dwell,
       COUNT(DISTINCT device_id) AS devices
FROM <T>
WHERE run_id = '<R>' AND arm = 'buffered'
  AND resolved = true AND dwell_ms >= 1000 AND threshold IS NOT NULL;
```

Expected: `outcome_a_rows` ≈ 1 200 (50 seeded devices × 24 pre-seed records each),
`devices` = 50, `min_dwell` in the low thousands, `max_dwell` < `<G>`.
**`outcome_a_rows = 0` means the rig failed** — go to §11 R-a/R-b.

### V3 — The paired A-vs-C comparison (the claim of §4.4)

```sql
SELECT b.device_id, b.seq,
       c.resolved  AS control_resolved,  c.threshold AS control_threshold,  c.dwell_ms AS control_dwell,
       b.resolved  AS buffered_resolved, b.threshold AS buffered_threshold, b.dwell_ms AS buffered_dwell
FROM <T> b
JOIN <T> c
  ON  b.run_id = c.run_id AND b.device_id = c.device_id AND b.seq = c.seq
WHERE b.run_id = '<R>' AND b.arm = 'buffered' AND c.arm = 'control'
  AND b.device_id < 'device-050'
  AND c.resolved = false          -- the control arm gave up on this exact record
ORDER BY b.device_id, b.seq
LIMIT 50;
```

Every returned row must show `control_resolved = false, control_threshold = NULL,
control_dwell < 100` beside `buffered_resolved = true, buffered_threshold = 10.0 + device index,
buffered_dwell > 1000`. **That table is the deliverable.** It is the same record, in the same
wall-clock second, with and without the feature.

If the join returns zero rows, the two arms never overlapped in `seq` — check that both
`CONSUMER_GROUP`s are fresh and both started before T+0.

### V4 — Enrichment is real, not a constant

```sql
SELECT device_id, MIN(threshold) AS th, MIN(region) AS rg, COUNT(*) AS n
FROM <T>
WHERE run_id = '<R>' AND arm = 'buffered' AND resolved = true
GROUP BY device_id ORDER BY device_id LIMIT 10;
```

`threshold` must equal `10.0 + <device index>` and `region` must cycle
`eu-west, us-east, ap-south`. This rules out a lookup that resolves to a single cached document
for every key.

### V5 — Defaults are exactly the unbuffered behaviour (S3)

```sql
SELECT COUNT(*) AS violations
FROM <T>
WHERE run_id = '<R>' AND resolved = false
  AND NOT (threshold IS NULL AND region = 'unknown');
```

Must be `0`. A timed-out record must carry precisely the declared `default=` values, identical to
what the control arm produced.

### V6 — Sanity print for the seeder Job

The Job's last log lines:

```
seeded:          50 / 100 devices (device-000 .. device-049)
config type:     device
valid_from sent: 2026-09-14T09:00:00+00:00   (backdate 86400 s)
readback device-000: id=<sha1>  version=1  valid_from=2026-09-14T09:00:00+00:00  MATCH
readback device-049: id=<sha1>  version=1  valid_from=2026-09-14T09:00:00+00:00  MATCH
elapsed:         121.4 s  (delay 120 s + 1.4 s seeding)
```

Both `MATCH` lines are the guard from §2.4. A `MISMATCH` aborts the run.

---

## 11. Negative tests

`tools/negative-tests/test_negative.py` — run **locally** against the pinned SHA, not deployed.
NT1, NT2 and NT4-NT7 all raise before any I/O, so `Application(broker_address="localhost:9092")`
constructs fine and no broker is needed.

| id | What | Trigger | Expected | Where |
|---|---|---|---|---|
| **NT1** | A field with no `default=` under a buffer | `fields={"threshold": lookup.json_field("$.threshold", type="device")}` (no `default=`), passed with any `buffer=` | `ValueError` at `join_lookup()` **build time**, naming the field | **local** |
| **NT2** | `grace_ms = 0` | `LookupBuffer(grace_ms=0, is_resolved=lambda v: True)` | `ValueError` at construction | **local** |
| **NT3a** | Overflow, raise | `MAX_BUFFERED_PER_KEY=5`, `ON_OVERFLOW=raise` on the buffered deployment | `LookupBufferOverflowError`, application dies. Per-key depth reaches 5 within ~25 s of an unseeded device's first record, so the crash is guaranteed and fast. | **deployment variant** |
| **NT3b** | Overflow, drop-newest | `MAX_BUFFERED_PER_KEY=5`, `ON_OVERFLOW=drop-newest` | App survives; at most 5 records per key are ever released. `SELECT device_id, COUNT(*) FROM <T> WHERE arm='buffered' AND device_id>='device-050' GROUP BY 1` must show ≤5 per device instead of ~60. Quantitative, not just "it didn't crash". | **deployment variant** |
| **NT4** | `is_resolved` not callable | `LookupBuffer(grace_ms=1000, is_resolved="nope")` | `ValueError` | **local** |
| **NT5** | Bad `on_timeout` | `on_timeout="discard"` | `ValueError` | **local** |
| **NT6** | Bad `on_overflow` | `on_overflow="drop-oldest"` | `ValueError` | **local** |
| **NT7** | `max_buffered_per_key < 1` | `max_buffered_per_key=0` | `ValueError` | **local** |
| **NT8** | Missing state on a buffered deployment | remove `state:` from the buffered block and deploy | Fails loudly at startup, naming the missing state directory — *not* a silent in-memory fallback (S7) | **deployment variant** |

**Variant run V-DROP** (not a negative test, a third semantic): set `ON_TIMEOUT=drop` on the
buffered deployment, bump `RUN_ID` and both `CONSUMER_GROUP`s, re-run. Outcome B rows must vanish
entirely — `SELECT COUNT(*) FROM <T> WHERE run_id='<R2>' AND arm='buffered' AND resolved=false`
must be `0`, while the control arm's unseeded count is unchanged. A rate-limited warning naming
the key must appear in the log.

Run the deployment variants **last and one at a time**, each with a fresh `RUN_ID` and fresh
consumer groups. NT3a kills the application, so it must not run before the main measurement.

---

## 12. Risks, constraints and open questions

### 12.1 Risks that would make the rig fail to show the feature at all

| id | Risk | Why it is dangerous | Mitigation |
|---|---|---|---|
| **R-a** | **`valid_from` not backdated** (§2.4) | `find_valid_version` matches `valid_from <= record_ts`. A config stamped "now" never applies to an older record, so it resolves neither immediately nor after the buffer — outcome A becomes zero and every pre-seed record looks like outcome B. **The rig runs green and proves nothing.** | Seeder sends `metadata.valid_from` backdated 24 h **and asserts the readback**, exiting non-zero on mismatch. Escape hatch: `TIMESTAMP_SKEW_MS`. Detected by V2 returning 0. |
| **R-b** | **Generator stops, or is a Job** (R1) | There is no timer. Nothing is released and nothing times out; records sit in RocksDB forever. Silent — the pipeline looks healthy and the table simply stops growing. | Generator is a `Service` running forever. Operator does not stop it before running §10. |
| **R-c** | **`lookup-sink` starts after the seeder fires** | Every record then resolves on the first attempt, nothing ever enters the buffer, and the buffered arm is indistinguishable from the control. | `SEED_DELAY_SECONDS=120` plus the explicit deploy ordering in §9 (sinks in step 7, generator + seeder in step 8). Detected by V2 returning 0 with A0 populated. |
| **R-d** | **`AUTO_OFFSET_RESET=earliest` on `lookup-sink`** | A restart re-drains the pre-seed backlog into a world where all configs exist; every replayed record resolves instantly and the table is contaminated with rows that look like A0 but were never late. | `latest`, and a fresh `CONSUMER_GROUP` per run. |
| **R-e** | **Both arms share a consumer group** | They would split partitions instead of each seeing every record, and the V3 paired join would return almost nothing. | Distinct `CONSUMER_GROUP` per arm; V3 returning zero rows is the detector. |
| **R-f** | **Wrong `is_resolved`** | `lambda v: v["threshold"] is not None` cannot distinguish "no config" from "config present, threshold null", silently reclassifying B as A. | `unresolved_types_field="__unresolved__"` + `lambda v: not v["__unresolved__"]` (S9). |
| **R-g** | **Mongo wiped while `config-updates` retained** (§6.2) | Every lookup resolves to a version whose content 404s; with `fallback="default"` that returns defaults instead of crashing — **indistinguishable from outcome B**. | Reset Mongo and `config-updates` together; bump `RUN_ID`. V4 detects it (resolved rows with constant/absent thresholds). |
| **R-h** | **`key_deserializer="str"` omitted** on the data topic | The message key stays bytes and never matches a `target_key`; nothing ever resolves and the buffered arm shows 100% outcome B. | Explicit in §7.5 step 2. Detected by V1's `buffered/seeded/resolved=true` row being empty. |
| **R-i** | **`fallback` left at the SDK default `"error"`** | Re-raises inside `join()` and kills the application on the first content-fetch failure. A per-field `default=` does not cover it. | `fallback="default"` (§7.5 step 3). |
| **R-j** | **`DEVICE_COUNT` raised or rate lowered without redoing §6.3** | Sweep cycle is `D/(4R)`; at `D=10 000, R=1` it is 41 minutes, so outcome B lands ~41 min after `grace_ms` and looks like the buffer hanging. Per-key depth `(R/D)·G` can also cross `RELEASE_WARN_RECORDS=1000`. | §6.3 arithmetic reproduced in the `lookup-sink` README; both variable descriptions name the coupling. |
| **R-k** | **`sensor-data` reduced to 1 partition** | The sweep is per-partition (R4). With 1 partition it still works, but the topic can then only ever have one consumer, blocking any Phase 2 replica test without recreating it. | 4 partitions, decided before data exists. |

### 12.2 Constraints honoured

- QuixStreams-first throughout: `Source` for the generator, `app.topic` + SDF for both sinks,
  `join_lookup` + `QuixConfigurationService` for the lookup, `QuixTSDataLakeSink` for the lake. No
  hand-rolled consumer/producer loop, no manual JSON serde, no bespoke config cache, no REST
  polling loop against the DCM, no KTable-style join. The seeder's HTTP POST is not a workaround —
  the DCM's write path is a REST API and has no Kafka interface.
- `quixstreams` pinned to the 40-char SHA everywhere; dockerfiles install `git`.
- Required topics read with `os.environ[...]`, never `.get(..., fallback)`.
- Every env var appears in both `app.yaml` and the `quix.yaml` block. No hyphens in variable names.
- One `main.py` per service; no helper modules.
- Nothing outside `C:\repos\quixstreams-tests` is modified. `quix-samples`,
  `comma-car-segments-ingest` and `TTL_test_environment` are read-only references.
- No sleeps to "make it work" and no retry-until-green. `SEED_DELAY_SECONDS` is the instrument
  under test, not a fudge factor, and its two bounds are stated in its description.

### 12.3 Open questions

1. **How does `config-seeder` reach the managed DCM?** Managed deployments may not accept a
   `network.serviceName` block, in which case `http://dcm` will not resolve and the seeder must
   use the `publicAccess` URL with a bearer token in `DCM_API_TOKEN`. Resolve at deploy time by
   checking whether the created DCM deployment exposes internal DNS; `DCM_API_URL` and
   `DCM_API_TOKEN` are variables precisely so this is a Portal edit, not a rebuild.
2. **Does the DCM honour `metadata.valid_from` on POST, and does it propagate it into the Kafka
   event that `ConfigurationVersion.from_event()` parses?** The docs say it is "when this
   configuration becomes valid", which is the right field, but this has not been observed on this
   cluster. The seeder's readback assertion (§7.4 step 5) answers the first half; V2 answers the
   second. The §2.4 escape hatch covers a negative answer.
3. **Is `RUN_ID` worth a Hive partition?** Left as a plain column to avoid file shatter. If the
   rig ends up holding many runs, that is a `TABLE_VERSION` bump away — but `hive_columns` is
   immutable once the table registers, so decide before the first write.
4. **Should the negative tests be committed as pytest cases or a plain script?** Specified as
   `tools/negative-tests/test_negative.py`; pytest adds a dependency for seven `assertRaises`.
   Tester's call.

---

## 13. Alternatives considered

| Considered | Rejected because |
|---|---|
| **Control arm as a redeploy** of the same deployment with `BUFFER_ENABLED=false` | It would run after seeding, so every record would resolve on the first attempt and the control would show nothing. Producing a valid control needs the configs deleted, `config-updates` deleted, and the whole cycle re-run — three manual steps in a rig whose value is trustworthiness. Two simultaneous deployments cost one `quix.yaml` block and give a record-level paired comparison instead of a cross-run one. |
| **Output topic only, no lakehouse** | The claim is a row-level join across two arms over thousands of rows. That is a SQL question; the Portal's message viewer cannot answer it. The topic is kept anyway as V0. |
| **`sdf.sink(QuixTSDataLakeSink(...))` inside `lookup-sink`** | Puts `blobStorage: {bind: true}` on the service under test. The testrig Storage Gateway has aborted deploys before; a blob failure would then block the experiment itself rather than just its reporting surface. |
| **`contentStore: file`** | Makes the Storage Gateway a hard dependency of config *delivery*, i.e. of the thing under test. The configs are <1 KB, so the only advantage of `file` (large/binary content) does not apply. |
| **`is_resolved=lambda v: v["threshold"] is not None`** | Cannot tell "no config" from "config present, threshold null" — it would silently misclassify outcome B as outcome A, which is the exact error the rig exists to rule out. |
| **Random device selection in the generator** | Outcome A's release trigger is the *next record for the same key* (R2). Random selection over 100 keys has a long tail, leaving some devices' buffers unreleased for tens of seconds and making dwell times uninterpretable. Round-robin fixes the per-key interval at exactly `DEVICE_COUNT × SLEEP_SECONDS`. |
| **Burst-then-exit generator (`Job`)** | R1: with no traffic, nothing is released and nothing times out. The rig would produce an empty table and look like a broken deployment. |
| **Manipulating record timestamps to create the lateness** | `grace_ms` is wall-clock from operator entry (S1). Timestamp manipulation would test nothing about the grace period. Timestamps appear in this spec only as the §2.4 escape hatch, for version *applicability*. |
| **A custom `BaseLookup` to sidestep `valid_from`** | Would test a bespoke lookup, not `QuixConfigurationService` + DCM, which is the integration the PR is aimed at. `metadata.valid_from` is the sanctioned answer. |
| **Pinning the PR branch name instead of the SHA** | A moving ref silently ships a different library on any rebuild, and every deploy rebuilds the image. |

---

## 14. Summary tables

### 14.1 Services

| service | type | consumes | produces | state | key env vars |
|---|---|---|---|---|---|
| `mongodb` | Service | — | — (TCP 27017, `serviceName: mongodb`) | **yes**, 1 GB | `MONGO_DBPATH`, `MONGO_INITDB_ROOT_USERNAME`, `MONGO_INITDB_ROOT_PASSWORD` |
| Dynamic Configuration Manager | Managed Service | HTTP POST (seeder) | `config-updates` | via Mongo | `topic`, `mongoConnectionGroup`, `mongoDatabase`, `mongoCollection`, `contentStore` |
| `data-generator` | Service | — | `sensor-data` | no | `output`, `DEVICE_COUNT`, `SLEEP_SECONDS`, `RUN_ID`, `TIMESTAMP_SKEW_MS` |
| `config-seeder` | **Job** | — | HTTP POST (DCM) | no | `DCM_API_URL`, `SEED_DELAY_SECONDS`, `SEEDED_DEVICE_COUNT`, `VALID_FROM_BACKDATE_SECONDS` |
| `lookup-sink` — Buffered | Service | `sensor-data`, `config-updates` | `enriched-sensor-data` | **yes**, 1 GB | `BUFFER_ENABLED=true`, `GRACE_MS=300000`, `ON_TIMEOUT`, `MAX_BUFFERED_PER_KEY`, `ON_OVERFLOW`, `STORE_NAME`, `ARM=buffered`, `CONSUMER_GROUP` |
| `lookup-sink` — Control | Service | `sensor-data`, `config-updates` | `enriched-sensor-data` | **no** (deliberate) | `BUFFER_ENABLED=` (blank), `ARM=control`, `CONSUMER_GROUP` |
| `lake-sink` | Service | `enriched-sensor-data` | Iceberg `pr1110_grace_v1` | no (`blobStorage.bind`) | `TABLE_NAME`, `TABLE_VERSION`, `HIVE_COLUMNS`, `TIMESTAMP_COLUMN`, `BATCH_SIZE` |

### 14.2 Outcomes

| outcome | device-id range (and window) | output-column signature |
|---|---|---|
| **A** — buffered, then resolved | `device-000` .. `device-049`, records with `ingest_ms < seed_ms`, arm `buffered` | `arm='buffered' AND resolved=true AND dwell_ms >= 1000 AND dwell_ms < 300000 AND threshold IS NOT NULL AND region <> 'unknown'` |
| **B** — buffered, then timed out | `device-050` .. `device-099`, any window, arm `buffered` | `arm='buffered' AND resolved=false AND dwell_ms >= 300000 AND threshold IS NULL AND region = 'unknown'` |
| **C** — control, no buffer | all devices `device-000` .. `device-099`, any window, arm `control` | `arm='control' AND dwell_ms < 100` for **every** row; and for `device_id < 'device-050' AND ingest_ms < seed_ms`, `resolved=false AND threshold IS NULL` on the very `(device_id, seq)` pairs arm A resolved |

*(Supporting baseline, not one of the three: **A0** = `arm='buffered' AND resolved=true AND
dwell_ms < 100` for `device-000..049` after the seed — proves the configuration is genuinely
reachable, so an empty A can be attributed to the buffer rather than to delivery.)*

---

## 15. References

- PR #1110 — *feat(lookup): buffer late-config records instead of blocking the partition*:
  https://github.com/quixio/quix-streams/pull/1110
  Pinned SHA `6632b46cdb2d493e4facf6c00b78c608ae70af87`
  (branch `feature/sc-72821/qs-update-adding-grace-period-to-lookup`, OPEN, unreleased)
- `docs/joins.md` at that SHA — the "Late-arriving configuration (`LookupBuffer`)" section.
- Source files added by the PR: `buffer.py`, `buffer_bookkeeping.py`, `buffer_operator.py`,
  `buffer_state.py`, `buffer_sweep.py`, plus
  `quixstreams/state/rocksdb/timestamped.py` (`delete_interval()`).
- Dynamic Configuration Manager docs:
  https://quix.io/docs/quix-cloud/services/dynamic-configuration.html
- Managed library item: `C:\repos\quix-samples\managed\dynamic-configuration\`
  (`library.json`, `README.md`)
- `join_lookup` client sample:
  `C:\repos\quix-samples\python\transformations\quix_configuration_enricher\main.py`
- MongoDB as a repo app: `C:\repos\comma-car-segments-ingest\mongodb\`
  and `C:\repos\comma-car-segments-ingest\quix.yaml` lines 9-35
- Test-rig `quix.yaml` model and `lake-sink`: `C:\repos\TTL_test_environment\`
- Skills the implementer must load first: `quix-service-create` (mandatory 3-step procedure),
  `quix-dcm-join-lookup` (the DCM/lookup split and the key-resolution rule),
  `quixstreams-idioms` §0 (service file shape), `quix-lakehouse` §2b (table vs raw-file writes).
