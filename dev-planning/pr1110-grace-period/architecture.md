# PR1110 grace-period rig — architecture

Implementation notes for `dev-planning/pr1110-grace-period/spec.md`. Written for the
engineer who has to change this in six months: what was built, why it is shaped this way,
every deviation from the spec with its reason, and every point the spec left to the
implementer.

Feature under test: quix-streams PR #1110, pinned at
`6632b46cdb2d493e4facf6c00b78c608ae70af87`. Every API claim below was read from that SHA,
not from the PR description.

---

## 1. What this is

Seven Quix Cloud deployments that make one thing observable: a record whose configuration
does not exist yet. The generator produces into a world with no configuration for 120
wall-clock seconds; the seeder then creates configurations for half the key space. Two
deployments of the *same* application consume the *same* topic at the *same* moment —
one with a `LookupBuffer` attached, one with `buffer=None` — and both write to one output
topic tagged with an `arm` column. A third deployment sinks that topic to an Iceberg
table, where the claim becomes a self-join on `(run_id, device_id, seq)`.

Everything else in the repo exists to keep that comparison honest.

---

## 2. File manifest

| path | lines | purpose |
|---|---|---|
| `quix.yaml` | 414 | Pipeline descriptor: 7 deployments, 3 topics. The only file that decides what actually runs. |
| `README.md` | 211 | What the rig proves, the 9-step run procedure, the five verification queries. |
| `mongodb/dockerfile` | 8 | `FROM mongo:8.0.21`, `EXPOSE 27017`, `ENTRYPOINT ["/init.sh"]`. Verbatim copy. |
| `mongodb/init.sh` | 136 | chown-then-`gosu` entry point. Verbatim copy, md5-identical to the source. |
| `mongodb/app.yaml` | 33 | Portal-visible vars: root user/password, `MONGO_DBPATH`. |
| `mongodb/README.md` | 39 | Why `init.sh` must not be rewritten; the Mongo-reset caveat. |
| `data-generator/main.py` | 160 | `Source` subclass, round-robin over `DEVICE_COUNT` keys, forever. |
| `data-generator/app.yaml` | 63 | 7 vars; descriptions carry the sizing coupling. |
| `data-generator/dockerfile` | 21 | Shared shape: python 3.13-slim + `git` + pip. |
| `data-generator/requirements.txt` | 1 | `quixstreams @ git+…@<SHA>`. |
| `data-generator/README.md` | 51 | Service-not-Job, round-robin-not-random, the sizing table. |
| `config-seeder/main.py` | 241 | Sleep → POST 50 configs with backdated `valid_from` → readback assertion → sanity table. |
| `config-seeder/app.yaml` | 73 | 8 vars; `SEED_DELAY_SECONDS` description states both of its bounds. |
| `config-seeder/dockerfile` | 21 | Shared shape. |
| `config-seeder/requirements.txt` | 2 | Pinned quixstreams + `requests==2.32.3`. |
| `config-seeder/README.md` | 79 | The `valid_from` trap and why the readback assertion is load-bearing. |
| `lookup-sink/main.py` | 170 | The service under test. `stamp_ingest` → `join_lookup(buffer=…)` → `stamp_emit` → `to_topic`. |
| `lookup-sink/app.yaml` | 124 | 14 vars — every `LookupBuffer` parameter is Portal-retunable without a rebuild. |
| `lookup-sink/dockerfile` | 21 | Shared shape. |
| `lookup-sink/requirements.txt` | 1 | Pinned quixstreams. |
| `lookup-sink/README.md` | 87 | The two-arm table, the four non-stylistic settings, expanded-transform consequences. |
| `lake-sink/main.py` | 174 | `QuixTSDataLakeSink` → `pr1110_grace_v1`. Adapted from the canonical sample. |
| `lake-sink/app.yaml` | 118 | 15 vars; `TABLE_VERSION` description states the migration rule. |
| `lake-sink/dockerfile` | 21 | Shared shape. |
| `lake-sink/requirements.txt` | 8 | `quixstreams[quixdatalake]@<SHA>` **plus `quixportal[all]`** — see §5.1. |
| `lake-sink/README.md` | 47 | Why it is a separate deployment; the layout-is-a-migration rule. |
| `tools/negative-tests/test_negative.py` | 136 | Six build/construction guards, no broker needed. |
| `tools/negative-tests/requirements.txt` | 1 | Pinned quixstreams. |
| `tools/negative-tests/README.md` | 46 | How to run; the three deployment-only variants and V-DROP. |

`tools/` is not referenced by `quix.yaml` and is therefore not an app.

## 3. Deployments

| deployment | type | consumes | produces | state | replicas |
|---|---|---|---|---|---|
| MongoDB | Service (`mongodb`) | — | — (TCP 27017, `serviceName: mongodb`) | **enabled, 1 GB** | 1 |
| Dynamic Configuration Manager | Service (managed, `libraryItemId: dynamic-configuration`) | HTTP POST from the seeder | `config-updates` | via Mongo | 1 |
| Data Generator | Service (`data-generator`) | — | `sensor-data` | no | 1 |
| Config Seeder | **Job** (`config-seeder`) | — | HTTP POST to the DCM | no | 1 |
| Lookup Sink - Buffered | Service (`lookup-sink`) | `sensor-data`, `config-updates` | `enriched-sensor-data` | **enabled, 1 GB** | 1 |
| Lookup Sink - Control | Service (`lookup-sink`) | `sensor-data`, `config-updates` | `enriched-sensor-data` | **none (deliberate)** | 1 |
| Lake Sink | Service (`lake-sink`) | `enriched-sensor-data` | Iceberg `pr1110_grace_v1` | no (`blobStorage.bind`) | 1 |

Topics: `sensor-data` (4 partitions, 1 d), `config-updates` (1 partition, 30 d),
`enriched-sensor-data` (4 partitions, 1 d). The buffer's changelog topic is created
automatically by QuixStreams and is deliberately **not** declared.

The control arm's missing `state:` block is itself a check: `buffer=None` registers no
store, so if that deployment ever fails at startup demanding state, the buffer is
registering a store when it is disabled.

---

## 4. Data flow

```
data-generator (Source.run loop, forever)
  └─ key = device_id (str), value = {device_id, seq, value, timestamp, produced_ms, run_id}
     timestamp_ms = produced_ms + TIMESTAMP_SKEW_MS
     └─> sensor-data (4 partitions, hashed over 100 round-robin keys)

lookup-sink  (x2, distinct consumer groups, same topic)
  app.topic(input, key_deserializer="str")        # key must be str, not bytes
  sdf.apply(stamp_ingest)                          # ingest_ms = wall clock  <-- grace zero
  sdf.join_lookup(lookup, fields, on="device_id", buffer=buffer or None)
        │  QuixConfigurationService(fallback="default",
        │                           unresolved_types_field="__unresolved__")
        │  fields: threshold(default=None), region(default="unknown")
        │
        ├─ buffer=None (control):  update() in place, 1:1, always emits
        └─ buffer=LookupBuffer:    add_transform(expand=True), 1:N
              1. join + is_resolved(value)
              2. sweep <=4 other keys on THIS partition past their deadline
              3. buffer / release / pass through
  sdf.apply(stamp_emit)                            # emit_ms, dwell_ms, resolved,
                                                   # unresolved_types, seeded_expected,
                                                   # arm, buffer_enabled, grace_ms,
                                                   # on_timeout; deletes __unresolved__
  sdf.to_topic(output, key_serializer="str")
     └─> enriched-sensor-data

lake-sink
  sdf.sink(QuixTSDataLakeSink(hive=[year,month,day,hour,arm], sort=emit_ms))
     └─> pr1110_grace_v1
```

`ingest_ms` lives in the record **value**, which is what the buffer serialises into
RocksDB and reads back, so it survives the round-trip. `emit_ms` is stamped after the
join so a released record carries its own emission time, not the triggering record's.
`dwell_ms = emit_ms - ingest_ms` is computed in-process before anything is written, so no
topic, sink or lakehouse latency can contaminate the measurement.

### Nothing downstream may assume 1:1

With a buffer, `join_lookup` appends an **expanded transform**. One input record can emit
up to `SWEEP_EMIT_BUDGET = 256` sweep emissions plus an entire same-key release queue,
each output carrying its own key, timestamp and headers — and an output may belong to a
different key than the record that triggered it. Output order is not input order. No code
here counts outputs against inputs; no verification query relies on offset order.

### There is no timer

`BufferOperator` runs only when a record arrives. Releases and timeouts are both driven
by traffic: `SWEEP_BUDGET = 4` key-slots settled per incoming record, on that record's
partition only. Two consequences shaped the design:

* the generator is a `Service` that runs forever — stop it and every buffered record
  freezes in RocksDB while the pipeline stays green;
* it round-robins, so every key is revisited every `DEVICE_COUNT × SLEEP_SECONDS`, which
  is what triggers the enriched releases (the sweep settles *timeouts*, not releases).

---

## 5. Deviations from the spec

Each with the reason. Nothing here was changed for taste.

### 5.1 `lake-sink/requirements.txt` adds `quixportal[all]>=2.0.2`

Spec §6.5 says `quixstreams[quixdatalake] @ git+…@<SHA>` and "never hand-list
pandas/pyarrow". The pandas/pyarrow half is right; the rest is not sufficient. At the
pinned SHA, `pyproject.toml:78` defines

```
quixdatalake = ["pandas>=1.0.0,<3.0", "pyarrow>=17.0.0", "quixportal>=0.1.0"]
```

— a **bare** `quixportal`, and quixportal's cloud filesystem backends (`s3fs`, `adlfs`,
`gcsfs`) live behind its own `all` extra (PyPI metadata: `provides_extra: [all, azure,
gcp, s3]`). The sink would import cleanly and be unable to write a byte. The canonical
`quix-samples/python/destinations/lakehouse-sink/requirements.txt` and the working
`TTL_test_environment/lake-sink` both pin `quixportal[all]>=2.0.2` for exactly this
reason, together with the Quix package index; both lines are copied verbatim.
(quixportal 2.0.6 is also on public PyPI, so the index line is belt-and-braces rather
than strictly required.)

### 5.2 `Data Generator` ships `desiredStatus: Stopped`

Spec §7.3's block says `Running`, but spec §9 step 8 says to start the generator *after*
the sinks are Running, and to treat that minute as T+0. Both cannot be true on a single
sync. Stopped is the one that preserves the experiment: the generator's start time is
T+0, and risk R-c (a sink starting after the seed ⇒ every record resolves immediately ⇒
the buffered arm is indistinguishable from the control) is the failure this ordering
exists to prevent. The README's step 7 says to start it by hand.

Related: a Job auto-runs the moment it is first created, so `Config Seeder` will fire on
the first sync — probably while MongoDB and the DCM are still building — and exit
non-zero on connection refused. That is recoverable (`replace: true` is idempotent, just
restart it from the Portal at the real T+0) and is documented in README step 7, but it is
worth knowing before it happens rather than after.

### 5.3 `DCM_API_URL` defaults to `http://config-api-svc`, not `http://dcm`

Resolved against the live cluster by the operator before implementation: every managed
DCM in the org carries `network.serviceName: config-api-svc`, so in-cluster DNS works and
no token is needed. `urlPrefix: dcm` is the *public* prefix, which is what spec §7.4's
`http://dcm` conflated. `DCM_API_TOKEN` is kept as an optional fallback for reaching the
public URL, sent as a bearer only when non-empty.

### 5.4 The DCM block is hand-written into `quix.yaml`

Spec §7.2 says to create it through the Portal and `git pull` the block back. Overridden
by the build brief: the block is written directly in the shape §7.2 shows, with
`deploymentType: Service` + `libraryItemId: dynamic-configuration` (the shape managed
items sync as), plus the `network` and `publicAccess` blocks. If the platform rejects a
key at sync it is fixed in place.

### 5.5 The seeder's readback compares **instants**, not strings

Spec §7.4 step 5 says "if the stored `valid_from` does not match what was sent". A literal
string comparison would abort the run on a cosmetic difference — `Z` vs `+00:00`,
re-serialised microseconds — which is not what the assertion is about. `same_instant()`
parses both sides with `datetime.fromisoformat` and compares as instants, and the value
sent is truncated to whole seconds so there is no sub-second precision available to lose.
The guard still fires on a DCM that drops or rewrites `valid_from`, which is its purpose.

### 5.6 `key_serializer="str"` added on the output topic

Not in the spec. The input topic declares `key_deserializer="str"`, so the key inside the
SDF is a `str`; `app.topic`'s default key serializer is `BytesSerializer`, which passes
the value through unchanged. It happens to work (confluent-kafka encodes a `str` key), but
declaring the serializer is what the SDK's own docstring shows for this case.

### 5.7 `LOGLEVEL` is not a variable on `data-generator`

Spec §7.3's variable table does not list it, so the generator hardcodes `INFO` rather than
reading an undeclared env var. `lookup-sink` and `lake-sink` do have it, per their tables.

### 5.8 `SEEDED_DEVICE_COUNT` on `data-generator` is logged, not merely declared

The spec declares it on the generator and calls it "informational". A declared variable
the code never reads is a trap — someone changes it and nothing happens. It is read and
printed in the startup banner, beside a line naming the services it must agree with.

---

## 6. Things the spec got wrong, or left to the implementer

The highest-value section. Each of these was found by trying to implement the spec.

### 6.1 NT1 cannot be run the way spec §11 describes — **spec error**

§11 states: *"NT1, NT2 and NT4–NT7 all raise before any I/O, so
`Application(broker_address="localhost:9092")` constructs fine and no broker is needed."*

True for NT2 and NT4–NT7. **False for NT1.** NT1 needs a `fields` mapping, and building
one the documented way means constructing `QuixConfigurationService`. Its `__init__` ends
with `self._start_consumer_thread()` (`lookup.py:156`), which is:

```python
threading.Thread(target=self._consumer_thread, daemon=True).start()
self._started.wait()            # <-- blocks
```

The worker's first act is `self._topic.broker_config.num_partitions`, and
`Topic.broker_config` raises `TopicConfigurationError` when the broker config has not been
fetched — which it has not, before `app.run()`. The worker's `except Exception:` logs and
returns **without setting `_started`**, so the constructor blocks forever. Not "raises
before any I/O": hangs, with no output after one log line.

Resolution in `tools/negative-tests/test_negative.py`: NT1 builds the field directly as
`QuixConfigurationServiceJSONField(type="device", jsonpath="$.threshold")` (no `default=`
⇒ `RAISE_ON_MISSING`) and passes a 10-line `_StubLookup(BaseLookup)` to a real
`sdf.join_lookup(..., buffer=…)`. This is faithful, not a shortcut: `join_lookup` calls
`buffer.validate_fields(fields)` as its *first* statement in the buffered branch
(`dataframe.py:1993`), before `register_store` and before the lookup is touched at all.
`Application`, `app.topic` and `app.dataframe` are broker-free — the broker availability
check lives in `run()`.

### 6.2 `on="device_id"` makes the `key_deserializer="str"` rationale wrong — **spec error, harmless here**

Spec §7.5 and risk R-h say the data topic needs `key_deserializer="str"` because "the
lookup resolves configs by message key" and without it "the key stays bytes and never
matches a `target_key`". With `on="device_id"` (which §7.5 step 6 also specifies), the
lookup key is `value["device_id"]` — a `str` straight out of the JSON payload — and the
message key plays no part in resolution at all. R-h as written cannot happen.

`key_deserializer="str"` is still correct and still required, for a different reason: the
buffer keys its RocksDB bookkeeping on the **message key** (`prefix_for_key(key)` in
`buffer_operator.py`), and the released record is re-emitted under that key. Both settings
are kept; the rationale in `lookup-sink/README.md` is the corrected one.

### 6.3 `run_id` has two contradictory sources — **spec inconsistency**

§5.3 lists `run_id` as "set by: env `RUN_ID`, passed through from payload". §7.5's
variable table has no `RUN_ID` on `lookup-sink` at all. Resolved in favour of §7.5:
`run_id` is produced by the generator and passes through untouched. This is also the
correct choice — a `RUN_ID` on the sink could disagree with the one in the payload and
silently relabel rows.

### 6.4 The spec's own §12.3 Q4 (pytest vs plain script) — resolved: plain script

Six `raises` assertions do not justify a test dependency in a repo whose only other Python
is four deployed services. The script prints `PASS`/`FAIL` per case with the actual
message and exits non-zero on any failure, which is what a human or a CI step needs.

### 6.5 Underspecified: the generator's `value` field

§5.1 shows `"value": 23.7` with no generation rule. Implemented as
`round(20.0 + (index % 100) * 0.1, 2)` — deterministic, no RNG, no seed variable, and
nothing in the rig reads it. If a future phase needs a signal, this is the line to change.

### 6.6 Underspecified: how far to backdate vs. readback precision

§7.4 asks for `datetime.now(timezone.utc) - timedelta(...)` with `.isoformat()`, which
carries microseconds. Combined with "assert the readback matches", that is a coin flip on
whatever precision the store keeps. Sent value is truncated to whole seconds (§5.5).

### 6.7 Underspecified: the DCM's `network`/`publicAccess` port

§7.2 gives `port: 80` as a DCM variable but no `network.ports` block. Written as
`port: 80, targetPort: 80` under `serviceName: config-api-svc`. If the platform rejects
`ports` on a managed item, drop that sub-block — `serviceName` alone is what the live
deployments in the org carry.

### 6.8 Worth knowing: `QuixConfigurationService` mangles the consumer group

`lookup.py:100` prepends the workspace id and, when `QUIX_REPLICA_NAME` is set, appends
the replica ordinal: `consumer_group = f"{app_config.consumer_group_prefix}-{consumer_group}"`
on top of the default `"enrich"`. The two arms therefore do **not** share the lookup's own
consumer group even though neither sets it — but that group is irrelevant anyway, because
the lookup `assign()`s every partition from offset 0 and bypasses the group protocol
entirely. Only the `Application`-level `CONSUMER_GROUP` needs to differ between arms.

---

## 7. Integration points

- **`mongodb-connection` variable group** (org-level, operator-created): supplies
  `MONGO_HOST=mongodb` / `MONGO_PORT` / `MONGO_USER` / `MONGO_PASSWORD` to the DCM's
  required `mongoConnectionGroup`. `mongodb`'s `network.serviceName` **must stay
  `mongodb`** or that host does not resolve, and `MONGO_INITDB_ROOT_USERNAME` /
  `_PASSWORD` must match the group's user/password.
- **The cluster-scoped lakehouse** in `testrigorg-global` injects
  `Quix__Lakehouse__Catalog__Url` / `__AuthToken` into any deployment carrying
  `blobStorage: {bind: true}` — here, `lake-sink` alone.
- **`CONFIG_TYPE`** must be identical in `config-seeder` and both `lookup-sink` arms. The
  type is fixed at pipeline-build time and the lookup does not check it; a mismatch means
  everything resolves to defaults forever.
- **`SEEDED_DEVICE_COUNT`** must be identical in `config-seeder`, `lookup-sink` (both
  arms) and `data-generator`, or the `seeded_expected` column lies.
- **`config-updates` retention (30 d)** is coupled to the Mongo content store: the SDK
  rebuilds configuration versions from that topic on every restart with no liveness check,
  so truncating it un-configures every device, and wiping Mongo while it is retained makes
  every lookup resolve to a version whose content 404s — which, under
  `fallback="default"`, is indistinguishable from a buffer timeout. Reset the two
  together and bump `RUN_ID`.

---

## 8. Sizing, reproduced here because the variable descriptions point at it

`D = DEVICE_COUNT = 100`, `R = 20` msg/s (`SLEEP_SECONDS = 0.05`), `G = GRACE_MS = 300` s,
`S = SEED_DELAY = 120` s, `P = 4` partitions, seeded `= 50`.

| quantity | formula | value | budget | headroom |
|---|---|---|---|---|
| per-device rate | `R/D` | 0.2 msg/s (one per 5 s) | — | — |
| per-key depth, unseeded | `(R/D)·G` | 60 records | `RELEASE_WARN_RECORDS = 1000` | 16× |
| per-key depth, seeded pre-seed | `(R/D)·S` | 24 records | 1000 | 42× |
| peak total held | `D·60` | ~6 000 records | `max_buffered_per_key = 10 000`/key | vast |
| peak state size | 6 000 × ~300 B | ~1.8 MB | `state.size: 1` GB | vast |
| full sweep cycle | `D/(4·R)` | 1.25 s | — | — |
| timeout settle lag | `G + cycle` | ~301.3 s | — | — |
| sweep emit demand | `(D−seeded)/D` | 0.5 records per incoming | `SWEEP_EMIT_BUDGET = 256` | 512× |

Per-partition check: partition `p` receives `R/P = 5` records/s, each settling 4 key-slots
⇒ 20 key-slot visits/s over `D/P = 25` keys ⇒ the same 1.25 s cycle. Every partition
carries traffic because round-robin over 100 keys hashes across all four.

Verification therefore asserts `dwell_ms >= GRACE_MS`, never `≈ GRACE_MS`.
