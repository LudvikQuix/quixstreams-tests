# PR1110 lookup buffer — deployment QA results

Scenarios T1–T5 run against a live Quix Cloud pipeline in workspace
`testrigorg-quixstreamstests-quixstr-df57e12b`, not in a test harness.

Scenario definitions: the deployment-test plan (T1–T5).
Test design and rig: [`spec.md`](spec.md) · build notes: [`architecture.md`](architecture.md) ·
defects filed: [`findings-brief.md`](findings-brief.md).

Published view: https://claude.ai/artifact/LdSeBXteUSogbEPG8BYdT3

---

## Result matrix

| Scenario | `6632b46c` | `ce3063b8` | `9df99ff6` | `178d4a06` | `683be529` |
|---|---|---|---|---|---|
| Functional — three outcomes | — | PASS | (D5 regression) | — | **PASS** |
| T1 idle-partition drain | FAIL | PASS | PASS | — | **PASS** |
| T1 changelog recovery | — | — | FAIL | — | **pending (r19)** |
| T2 content unreachable | — | — | FAIL (source) | claimed fixed | not run |
| T3 non-string key | — | — | FAIL | PASS | **PASS** |
| T4 large release pause | — | — | PASS | — | **PASS** |
| T5 sink attribution | — | — | FAIL (source) | PASS | **PASS** |

Every defect found on the early builds is fixed and verified on `683be529`, except
T1's recovery half (staged, not yet run) and T2 (deliberately not run on a
deployment — see below).

---

## Functional scenario — the three outcomes

**PASS on `683be529`** (run r20). The original end-to-end test: 10 devices, five
configured, seed at T+20 inside a 45 s grace, so all three outcomes occur in one
window. This is the check that the feature does its job; the numbered scenarios below
probe edge paths.

| arm | devices | resolved | rows | dwell_ms |
|---|---|---|---|---|
| buffered | unseeded | false | 737 | 45 000 – 45 170 |
| buffered | seeded | **true** | 1189 | 0 – 26 043 |
| control | unseeded | false | 1185 | 0 – 1 |
| control | seeded | false | **269** | 0 – 1 |
| control | seeded | true | 920 | 0 – 144 |

Control defaulted 269 seeded records; buffered defaulted **zero** and resolved exactly
269 more (1189 vs 920). The arithmetic closes, which is what makes the result readable
as the buffer's doing rather than a counting artefact. Timeouts fired at +0 to +170 ms
against the 45 000 ms grace.

`buffered/seeded/false` is **absent**, so the D5 regression seen on `9df99ff6` — the
first two records of every key never released — is gone.

### Flush after the producer stops

448 records held at 08:51:02; gap closed to zero by 08:52:17 — **75 s against a 45 s
grace** — ending at 2978 = 2978, lossless.

The flush completes *after* the grace period, not at the moment the producer stops:
each record waits out its own `grace_ms` from its own arrival, so the tail of the drain
is the tail of the arrivals. Confirmed correct behaviour. The operational rule is to
allow at least `grace_ms` after the last message before treating the output as
complete, and not to tear the consumer down sooner.

Measured across four runs, all lossless: 446 → 0 (~64 s), 2946 → 0 (5 m 11 s, 300 s
grace), 595 → 0 (~106 s), 448 → 0 (75 s).

---

## T1 — Buffer on a silent partition

### Idle drain — PASS on `683be529` (run r16)

One key, no configuration, `grace_ms=60000`, no traffic after the burst.

| arm | rows | resolved | dwell_ms |
|---|---|---|---|
| buffered | 604 | false | 60 004 – 60 998 |
| control | 604 | false | 0 – 1 |

Equal counts, so nothing was lost, and every record emitted within a second of its
deadline on a silent partition. One continuous process: `restarts 0`, no recovery
lines in the log.

History: on `6632b46c` nothing drained without traffic at all — the original
finding. `ce3063b8` added the ticker ("resolve buffered records at their deadline,
with or without traffic") and it has held on every build since.

### Changelog recovery — FAIL on `9df99ff6`, retest pending

Run r8. 140 records withheld, the arm restarted with no local store, recovery
succeeded unambiguously:

```
Recovery successful for RecoveryPartition "…changelog__…--sensor-data--lookup-buffer[2]"
Recovery successful for … [3]
Recovery process complete! Resuming normal processing...   06:23:46Z
```

Then nine minutes of zero emissions with the partition silent, while control had
emitted all 140 immediately. A later burst released everything including the
originals (`min_seq=0`, original `ingest_ms`), buffered reaching control exactly at
4514. The records were durable and intact — only the path that settles them without
traffic could not see them.

**Caveat that must travel with this result.** It was observed with **no state
volume**, which is the only way a restart forces the changelog-rebuild path — with a
volume, a restart reuses the store on disk. But the library states a buffered
`join_lookup` "needs a state directory and, on Quix Cloud, `state: {enabled: true}`".
So this is a real defect in a configuration you would not deploy, not a production
failure.

**Retest staged as r19, in the supported configuration.** State volume kept;
recovery forced the way production reaches it — scaling the buffered arm to two
replicas so partitions land on a pod with an empty local store and must rebuild from
the changelog. Four keys so partitions actually split. `683be529` lists "stale queue
entries survived repairs and blocked deadline visibility", which is plausibly the
remaining half of this.

---

## T2 — Configuration content unreachable

**FAIL on `9df99ff6`, proven in source. Not run on a deployment.**

`join()` populated the unresolved list in one place only:

```python
version = self._find_version(type_, on, timestamp)
if version is None:
    unresolved_types.append(type_)        # only here
```

while `_version_data()` returned the fields' defaults in **two** cases —
`version is None`, and `content is None` after a failed fetch under
`fallback="default"`. The second never reached `unresolved_types`, so a record whose
configuration existed but whose content could not be fetched left the join with
every field defaulted *and* `__unresolved__ == []`. The canonical predicate
`is_resolved=lambda v: not v["__unresolved__"]` therefore returned true and the
buffer passed it through as successfully enriched.

The retry half already existed (`version.failed()` sets `retry_at`); only the
accounting was missing.

**Why it was not run on a deployment.** Forcing it here means stopping MongoDB,
which backs the DCM's content store — that takes down shared infrastructure for
everything else in the workspace, and a total database outage is not the scenario.
The real one is a transient 5xx or timeout on a single `contentUrl`. This belongs in
a unit test that stubs the fetch. The source path is unambiguous and needs no
deployment to confirm.

`178d4a06` states it fixes this ("unresolved_types was appended only when no version
was found"). **Unverified here — treat as claimed, not proven.**

---

## T3 — Non-string message key

**PASS on `178d4a06` and `683be529`** (runs r11, r15).

Rejected when the pipeline is built, before any record is consumed:

```
File "/app/main.py", line 171, in main
  sdf = sdf.join_lookup(lookup, fields, on="device_id", buffer=buffer)
File ".../dataframe.py", line 1987, in join_lookup
  buffer.validate_key_deserializers(self._topics)
File ".../buffer.py", line 192, in validate_key_deserializers

ValueError: Topic '…-sensor-data' deserializes message keys with IntegerDeserializer.
`join_lookup(..., buffer=...)` stores withheld records under the message key, which
must be `bytes`, `str` or `None`. Declare the topic with `key_deserializer="bytes"`
or `key_deserializer="str"`.
```

Zero records consumed — no "processing incoming messages", no recovery, no commits.
The container still exits 1 and restarts, which is correct for a bad configuration;
what changed is that it never takes an offset it cannot release.

**Before, on `9df99ff6`:** both arms started Running, then record one raised the same
`ValueError` from `prefix_for_key` at `buffer_state.py:229`. The offset was never
committed, so the pod reprocessed it forever — restarts 0 → 7, backoff escalating
20 s → 5 m. The message was always good; only its timing was wrong.

Control comparison on that run: the `buffer=None` arm also crash-looped, but at
`to_topic` with `SerializationError: 'int' object has no attribute 'encode'` — the
rig's own output key serializer, downstream of the join. Distinct fault.

---

## T4 — Large release pause

**PASS on `9df99ff6` and `683be529`** (runs r10, r18).

One key at ~1000 msg/s fills `max_buffered_per_key=10000` in about ten seconds;
`grace_ms=300000` so nothing times out before configuration lands at T+60, making the
release one callback over a full buffer while production continues.

`683be529` (r18):

| arm | resolved | rows | dwell_ms |
|---|---|---|---|
| buffered | true | 122 227 | 0 – 65 053 |
| control | false | 49 833 | 0 – 5 |
| control | true | 112 240 | 0 – 122 |

**0 restarts**, and zero rebalance, revoke, eviction or `max.poll.interval.ms`
signals in the log. The buffered arm resolved ~10 000 more than control — the rescued
buffer. The remaining gap to control's total is `on_overflow="drop-newest"` discarding
everything past the cap during the pre-configuration window: expected, not a defect.

---

## T5 — Sink attribution of settled records

**PASS on `178d4a06` and `683be529`** (runs r13, r17).

Two keys; one configured so it resolves on arrival, one never configured so its
records buffer. While traffic flows they settle through the record path; once the
generator stops the remainder settle through the tick. `src_topic` / `src_partition` /
`src_offset` are stamped from `message_context()` at the point `sdf.sink()` reads it.

`683be529` (r17), buffered arm:

| key | path | rows | distinct offsets | offset range |
|---|---|---|---|---|
| device-000 | record | 2308 | 2308 | 185 674 – 187 981 |
| device-000 | tick | 8 | 8 | 187 982 – 187 989 |
| device-001 | record | 2008 | 2008 | 33 590 – 35 597 |
| device-001 | tick | 307 | 307 | 35 598 – 35 904 |

Three things confirm it: rows equal distinct offsets in every group, so no record
inherits another's; each tick range resumes exactly where its record range ends; and
the two keys occupy separate offset spaces, as their separate partitions should.

**Before, on `9df99ff6`:** `buffer_node.py` called `downstream(...)` with no
`set_message_context`, so a record released by another key's traffic ran under the
*trigger's* topic, partition and offset, while `buffer_tick.py` rebuilt a
`MessageContext` per emission. The code said so itself — "the extra two carry the
record's origin, which only the tick reads". Since `sdf.sink()` reads the same
accessor, a lakehouse sink would have filed those records under the wrong offset.

---

## Also verified

| property | measurement |
|---|---|
| Deadline accuracy, 10 keys | 45 004 – 45 048 ms vs 45 000 |
| Deadline accuracy, 100 keys | 300 000 – 300 084 ms vs 300 000 |
| Drain after source stop, process alive | 446 → 0, 2946 → 0, 595 → 0, 604 → 0; all lossless |
| Enrichment recovered vs unbuffered | 9420 resolved vs 8028; 1392 rescued from defaults |

Overshoot no longer scales with key count — ten times the keys moves it from 48 ms to
84 ms, against up to +204 s on the pre-ticker build.

### Regression seen on `9df99ff6`, not since

In run r7 every configured key emitted its `seq 0` and `seq 1` as a timeout carrying
defaults (dwell 60 001–60 835 ms) while everything from `seq 2` onward for those same
keys resolved and was released enriched — exactly 100 records across 50 devices. Runs
r5 and r6 on `ce3063b8` produced none under the same rig and shape, so it isolated to
that build. Not observed on `178d4a06` or `683be529`.

---

## Cautions for anyone rerunning this

Each of these produced a run that looked like a finding and was not.

- **Bump `CONFIG_TYPE` with `RUN_ID`.** The DCM keeps configurations forever, keyed by
  `(type, target_key)`. Reuse a type and every record resolves on its first attempt —
  no buffering, both outcomes empty, and a green run that proves nothing.
- **Backdate `valid_from`.** Version selection matches `valid_from <= record timestamp`,
  so a configuration created after its data never applies however long the buffer holds
  it. Without backdating, every record times out and it looks like the feature is broken.
- **Use a fresh consumer group per run.** Run r12 produced zero rows from the buffered
  arm with no error and no restart, which looked exactly like a T1 failure. Its group
  had been reused across a crash-looping build. **That run is void, not a result.**
- **Check which process you are reading.** Twice a log or status came from an outgoing
  pod mid-rollout and looked like a verdict — once showing `key_deserializer=str` while
  the incoming pod had `int`.
- **Let generator and seeder start together.** On a cold image rebuild the seeder's
  delay elapses before the generator's pod exists, so configuration lands before any
  data and nothing buffers. That voided run r4.
- **Bump `TABLE_VERSION` whenever the emitted schema changes.** Toggling
  `STAMP_MESSAGE_CONTEXT` within one table version left `pr1110_grace_v2` holding
  parquet both with and without `src_offset`; DuckDB's glob read then fails outright on
  the schema mismatch.

---

## Outstanding

- **T1 changelog recovery in the supported configuration** — staged as r19: state
  volume kept, recovery forced by scaling to two replicas rather than removing the
  volume.
- **`valid_from` variant** — `VALID_FROM_BACKDATE_SECONDS=0` so `valid_from` is the
  creation instant rather than backdated. Expected: pre-seed records can never resolve
  and must time out, which is correct behaviour but shows the operational cost — the
  buffer adds up to `grace_ms` of latency to records it can never help. Worth having
  beside the backdated run in the same table.
- **T2 as a unit test** that stubs a failing content fetch, rather than a deployment
  scenario.
