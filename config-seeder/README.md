# config-seeder

`deploymentType: Job`. Sleeps `SEED_DELAY_SECONDS`, then creates `SEEDED_DEVICE_COUNT`
device configurations in the Dynamic Configuration Manager in one burst. That delay is
the instrument the whole rig is built around: it is the window in which configuration
does not exist in the world.

```
POST {DCM_API_URL}/api/v1/configurations
{
  "metadata": {
    "type": "device",
    "target_key": "device-042",
    "valid_from": "2026-09-14T09:00:00+00:00",
    "category": "pr1110-rig-r1"
  },
  "content": {
    "threshold": 52.0,
    "region": "ap-south",
    "device": {"name": "sensor-042"}
  },
  "replace": true
}
```

`threshold = 10.0 + index` and `region = ["eu-west","us-east","ap-south"][index % 3]`, so
the enriched value identifies the device — a lookup that resolved every key to one cached
document is visible at a glance.

`replace: true` creates **or** versions, which makes re-running the Job idempotent.
`PUT /api/v1/configurations/{id}` only updates and 404s on an unknown id; do not use it.
Configuration id = `sha1("<type>-<target_key>")`.

## Why `valid_from` is backdated, and why the readback assertion exists

`Configuration.find_valid_version(timestamp)` selects the version whose `valid_from` is
at or before the **record's** timestamp. A configuration stamped "now" therefore never
applies to a record produced a minute ago:

> Record produced at T=0. Config POSTed at T=120 s ⇒ `valid_from` = 120 s. The record's
> timestamp (0) is before `valid_from` ⇒ `find_valid_version` returns `None` ⇒ the
> record is unresolvable **forever**, even with its configuration sitting in the cache.

Under a naive seeder every pre-seed record becomes a timeout and the enriched-release
outcome never occurs — the rig runs green, produces a full table and demonstrates
nothing. Hence `VALID_FROM_BACKDATE_SECONDS`, defaulting to 24 h. This is also the honest
production scenario: a device that came online before anyone registered its
configuration.

The Job then **reads two configurations back** (first and last seeded) and compares the
stored `valid_from` with what it sent, as instants rather than as strings — the DCM is
free to normalise `Z` vs `+00:00`. On a mismatch it logs the escape-hatch instruction and
exits non-zero. Without that guard, a DCM that silently ignores `metadata.valid_from`
turns the rig into a no-op that still produces a full-looking table.

## Sanity print

The last log lines of a good run:

```
seeded:          50 / 100 devices (device-000 .. device-049)
config type:     device
valid_from sent: 2026-09-14T09:00:00+00:00   (backdate 86400 s)
readback device-000: id=<sha1>  version=1  valid_from=...  MATCH
readback device-049: id=<sha1>  version=1  valid_from=...  MATCH
elapsed:         121.4 s  (delay 120 s + 1.4 s seeding)
```

Both `MATCH` lines are the guard. A `MISMATCH` aborts the run.

## Reachability

`DCM_API_URL` defaults to `http://config-api-svc`, the managed DCM's in-cluster service
DNS on port 80, which needs no token. `DCM_API_TOKEN` is a fallback for reaching the DCM
over its `publicAccess` URL instead; it is sent as a bearer only when non-empty.

No Kafka client, no polling loop, no configuration cache: the DCM's write path is a REST
API with no Kafka interface, so the HTTP POSTs here are its documented seeding route, not
a workaround for a QuixStreams primitive.
