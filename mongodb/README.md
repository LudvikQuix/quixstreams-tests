# mongodb

Backing store for the Dynamic Configuration Manager's configuration documents. A plain
`mongo:8.0.21` image behind `init.sh`, exposed in-cluster as `serviceName: mongodb` on
27017 — which is the `MONGO_HOST` the `mongodb-connection` variable group supplies to the
DCM. Changing that service name breaks the DCM's connection.

`dockerfile` and `init.sh` are copied **verbatim** from
`comma-car-segments-ingest/mongodb/`. Do not rewrite `init.sh`. It solves two problems
that have already destroyed a database once:

- **Ownership.** mongod runs with an explicit `--dbpath` under `/app/state`, the Quix
  state volume, which may be empty or provisioned root-owned. Files inside it were
  written by a previous container; if their owner does not match the UID mongod runs as,
  mongod can rename `WiredTiger.wt` but cannot open it and dies with Fatal assertion
  28595. The script chowns recursively on every start. The official
  `docker-entrypoint.sh` only chowns the hardcoded `/data/db`, never a custom `--dbpath`.
- **Clean shutdown.** It hands over with `gosu`, not `su -c`: gosu execs the target
  directly, so PID 1 becomes mongod and the SIGTERM Kubernetes sends on pod termination
  reaches it, letting WiredTiger checkpoint and close its files. Under `su -c` the signal
  went to the intervening shell, mongod was SIGKILLed at the end of the grace period, and
  every stop was an unclean shutdown.

The script never deletes data. A dbpath WiredTiger cannot recover is abandoned, not
repaired: point `MONGO_DBPATH` at an unused sibling directory (`mongodb-v3`, …) and
mongod initialises cleanly, with no shell in the container and no manual wipe. The old
directory stays on the volume until a human removes it.

## The reset caveat

`config-updates` outlives the content store, and the SDK rebuilds configuration versions
from topic events **with no liveness check**. If this volume is wiped while
`config-updates` still holds events, every lookup resolves to a version whose content
fetch 404s — and with `fallback="default"` that returns the field defaults instead of
crashing, which looks *exactly* like a buffer timeout and would silently corrupt the
result.

**If Mongo is ever reset, delete and recreate `config-updates` in the same action, and
bump `RUN_ID`.**
