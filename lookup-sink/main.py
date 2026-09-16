"""The service under test: `join_lookup` with and without PR1110's `LookupBuffer`.

One application directory, deployed twice against the same input topic at the same
wall-clock moment:

* ARM=buffered, BUFFER_ENABLED=true  -> a LookupBuffer withholds records whose
  configuration has not arrived yet.
* ARM=control,  BUFFER_ENABLED=      -> buffer=None, which is today's unbuffered
  behaviour exactly.

Both stamp `ingest_ms` before the join and `emit_ms` after it, so `dwell_ms` measures
time spent inside the operator and nothing else - no sink, topic or lakehouse latency
contaminates it. Running the arms simultaneously makes the comparison a row-level paired
join on (run_id, device_id, seq) rather than two runs an operator has to trust are
comparable.

Three settings here are not stylistic:

* The data topic's key deserializer, `KEY_DESERIALIZER`, defaulting to "str".
  Configurations are addressed by key, and the buffer's per-key bookkeeping prefixes on
  the message key; "int" is there to drive a non-string key through both.
* `fallback="default"` on the lookup. The SDK default is "error", which re-raises inside
  join() and kills the application when content cannot be fetched. A per-field
  `default=` does not cover that - `default` is consulted when the configuration is
  absent, never when the HTTP call throws.
* `is_resolved` reads `__unresolved__`, not `threshold is None`. The latter cannot tell
  "no configuration" from "configuration present, threshold null", and would silently
  reclassify a timed-out record as an enriched one - the exact error this rig exists to
  rule out.

The buffer is an EXPANDED transform: one input record can emit many outputs, each with
its own key, timestamp and headers, and an output may belong to a different key than the
record that triggered it. Nothing here may assume 1:1 or rely on offset order.

`STAMP_MESSAGE_CONTEXT` adds src_topic/src_partition/src_offset to every row, read from
`message_context()` after the join - the same accessor `sdf.sink()` uses to attribute a
record, so the columns are what a lakehouse sink wired in at this point would file the
record under. Off by default: with it off the emitted schema is unchanged.
"""

import logging
import os
import time

from quixstreams import Application, message_context
from quixstreams.dataframe.joins.lookups import LookupBuffer, QuixConfigurationService

logger = logging.getLogger(__name__)

# The lookup writes sorted(unresolved types) here on every joined record: empty list
# means everything resolved. It is deleted again in stamp_emit, after `resolved` and
# `unresolved_types` are derived from it, because a list-typed column is awkward in the
# lake and nothing downstream needs the raw field.
UNRESOLVED_FIELD = "__unresolved__"

TRUTHY = ("1", "true", "yes", "on")

KEY_DESERIALIZERS = ("str", "int")


def _env(name: str, default: str) -> str:
    """Read an env var, treating a blank Portal value as absent."""
    raw = os.getenv(name, "").strip()
    return raw if raw else default


ARM = _env("ARM", "buffered")
# Deliberately NOT read through `_env`: blank is the control arm, a meaningful value
# rather than a request for the default.
BUFFER_ENABLED = os.getenv("BUFFER_ENABLED", "").strip().lower() in TRUTHY
GRACE_MS = int(_env("GRACE_MS", "300000"))
ON_TIMEOUT = _env("ON_TIMEOUT", "emit")
MAX_BUFFERED_PER_KEY = int(_env("MAX_BUFFERED_PER_KEY", "10000"))
ON_OVERFLOW = _env("ON_OVERFLOW", "drop-newest")
STORE_NAME = _env("STORE_NAME", "lookup-buffer")
CONFIG_TYPE = _env("CONFIG_TYPE", "device")
SEEDED_DEVICE_COUNT = int(_env("SEEDED_DEVICE_COUNT", "50"))
KEY_DESERIALIZER = _env("KEY_DESERIALIZER", "str")
if KEY_DESERIALIZER not in KEY_DESERIALIZERS:
    raise ValueError(
        f"KEY_DESERIALIZER must be one of {KEY_DESERIALIZERS}, got {KEY_DESERIALIZER!r}"
    )
STAMP_MESSAGE_CONTEXT = _env("STAMP_MESSAGE_CONTEXT", "false").lower() in TRUTHY


def stamp_ingest(value: dict) -> dict:
    """Stamp operator-entry wall clock. This is the grace clock's zero."""
    value["ingest_ms"] = int(time.time() * 1000)
    return value


def stamp_source(value: dict) -> None:
    """Write the Kafka context this record is being processed under into
    src_topic/src_partition/src_offset. All three are None when no context is set."""
    try:
        ctx = message_context()
    except Exception:
        ctx = None
    value["src_topic"] = ctx.topic if ctx else None
    value["src_partition"] = int(ctx.partition) if ctx else None
    value["src_offset"] = int(ctx.offset) if ctx else None


def stamp_emit(value: dict) -> dict:
    """Shape the output row. Runs after the join, so a released record gets its own
    emission time rather than that of the record which triggered the release."""
    emit_ms = int(time.time() * 1000)
    unresolved = value.pop(UNRESOLVED_FIELD)
    value["emit_ms"] = emit_ms
    value["dwell_ms"] = emit_ms - value["ingest_ms"]
    value["resolved"] = not unresolved
    value["unresolved_types"] = ",".join(unresolved)
    # An a-priori expectation derived from the key space, so verification can assert
    # without consulting the seeder's own output.
    value["seeded_expected"] = int(value["device_id"][-3:]) < SEEDED_DEVICE_COUNT
    value["arm"] = ARM
    value["buffer_enabled"] = BUFFER_ENABLED
    value["grace_ms"] = GRACE_MS
    value["on_timeout"] = ON_TIMEOUT
    if STAMP_MESSAGE_CONTEXT:
        stamp_source(value)
    return value


def main() -> None:
    logging.basicConfig(
        level=_env("LOGLEVEL", "INFO"),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = Application(
        consumer_group=os.environ["CONSUMER_GROUP"],
        auto_offset_reset=_env("AUTO_OFFSET_RESET", "latest"),
    )

    data_topic = app.topic(name=os.environ["input"], key_deserializer=KEY_DESERIALIZER)
    config_topic = app.topic(name=os.environ["config_topic"])
    # Fixed at "str": the key reaching here is whatever data_topic deserialized, so under
    # KEY_DESERIALIZER=int an int arrives and StringSerializer rejects it.
    output_topic = app.topic(name=os.environ["output"], key_serializer="str")

    lookup = QuixConfigurationService(
        topic=config_topic,
        app_config=app.config,
        fallback="default",
        unresolved_types_field=UNRESOLVED_FIELD,
    )

    # Two narrow leaf paths, never jsonpath="$": a whole-document field deep-copies the
    # configuration on every message. Both defaults are mandatory under a buffer -
    # LookupBuffer.validate_fields() raises at build time otherwise - and they are
    # what a timed-out record is emitted with.
    fields = {
        "threshold": lookup.json_field("$.threshold", type=CONFIG_TYPE, default=None),
        "region": lookup.json_field("$.region", type=CONFIG_TYPE, default="unknown"),
    }

    buffer = None
    if BUFFER_ENABLED:
        buffer = LookupBuffer(
            grace_ms=GRACE_MS,
            is_resolved=lambda value: not value[UNRESOLVED_FIELD],
            on_timeout=ON_TIMEOUT,
            max_buffered_per_key=MAX_BUFFERED_PER_KEY,
            on_overflow=ON_OVERFLOW,
            store_name=STORE_NAME,
        )

    sdf = app.dataframe(topic=data_topic)
    sdf = sdf.apply(stamp_ingest)
    sdf = sdf.join_lookup(lookup, fields, on="device_id", buffer=buffer)
    sdf = sdf.apply(stamp_emit)
    sdf.to_topic(output_topic)

    logger.info("Starting Lookup Sink")
    logger.info("  Arm:            %s", ARM)
    logger.info(
        "  Input topic:    %s (key_deserializer=%s)",
        data_topic.name,
        KEY_DESERIALIZER,
    )
    logger.info("  Config topic:   %s", config_topic.name)
    logger.info("  Output topic:   %s", output_topic.name)
    logger.info("  Config type:    %s", CONFIG_TYPE)
    logger.info("  Consumer group: %s", os.environ["CONSUMER_GROUP"])
    logger.info("  Context stamp:  %s", STAMP_MESSAGE_CONTEXT)
    if buffer is None:
        logger.info("  Buffer:         DISABLED (buffer=None, unbuffered behaviour)")
    else:
        logger.info(
            "  Buffer:         grace_ms=%d on_timeout=%s max_per_key=%d "
            "on_overflow=%s store=%s",
            GRACE_MS,
            ON_TIMEOUT,
            MAX_BUFFERED_PER_KEY,
            ON_OVERFLOW,
            STORE_NAME,
        )

    app.run()


if __name__ == "__main__":
    main()
