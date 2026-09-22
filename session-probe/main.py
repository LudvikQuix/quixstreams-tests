"""One session-window probe of the live rig, deployed three times.

The three deployments differ only by variables: `.final(closing_strategy="key")`,
`.final(closing_strategy="partition")` and `.current(closing_strategy="key")` over the
same input topic, which makes the key-vs-partition contrast a paired comparison rather
than two runs an operator has to trust are comparable.

The startup block logs the resolved `state_dir` next to the platform's
`Quix__Deployment__State__Path`. That line is the only evidence that the store is on the
mounted volume rather than on ephemeral container disk; the restart scenario would pass
either way, rebuilt from the changelog.
"""

import logging
import os
from importlib.metadata import version
from typing import Any

from quixstreams import Application
from quixstreams.dataframe.windows import Collect, Count, Earliest, Latest

logger = logging.getLogger(__name__)

PROBE = os.environ["PROBE"]
EMIT_MODE = os.environ["EMIT_MODE"]
CLOSING_STRATEGY = os.environ["CLOSING_STRATEGY"]
GAP_MS = int(os.environ["GAP_MS"])
GRACE_MS = int(os.environ["GRACE_MS"])
STORE_NAME = os.environ["STORE_NAME"]
CONSUMER_GROUP = os.environ["CONSUMER_GROUP"]
LOGLEVEL = os.environ["LOGLEVEL"]


def on_late(
    value: Any,
    key: Any,
    timestamp_ms: int,
    late_by_ms: int,
    start: int,
    end: int,
    store_name: str,
    topic: str,
    partition: int,
    offset: int,
) -> bool:
    """Log a dropped late event. Returns True, so the library also logs its warning."""
    logger.warning(
        "LATE key=%s ts=%d late_by=%d would_be=[%d,%d) store=%s %s[%d]@%d",
        key,
        timestamp_ms,
        late_by_ms,
        start,
        end,
        store_name,
        topic,
        partition,
        offset,
    )
    return True


def describe(value: dict, key: str | bytes, timestamp: int, headers: Any) -> dict:
    """Stamp a window result with the probe and the scenario it belongs to."""
    # Partition-mode expiry keys each result by the raw store prefix
    # (windows/session.py:293-295), so the key is bytes there and str in key mode.
    if isinstance(key, bytes):
        key = key.decode()
    run_id, _, scenario = key.partition("-")
    return {
        **value,
        "probe": PROBE,
        "emit_mode": EMIT_MODE,
        "closing_strategy": CLOSING_STRATEGY,
        "key": key,
        "run_id": run_id,
        "scenario": scenario,
        "span_seconds": round((value["end"] - value["start"]) / 1000, 1),
    }


def main() -> None:
    # No state_dir argument: Application.__init__ (app.py:282-286) consults
    # Quix__Deployment__State__Path only while state_dir is None.
    app = Application(
        consumer_group=CONSUMER_GROUP,
        auto_offset_reset="earliest",
        loglevel=LOGLEVEL,
    )

    input_topic = app.topic(
        os.environ["input"], value_deserializer="json", key_deserializer="str"
    )
    output_topic = app.topic(
        os.environ["output"], value_serializer="json", key_serializer="str"
    )

    logger.info("Starting Session Probe")
    logger.info("  quixstreams:      %s", version("quixstreams"))
    logger.info("  state_dir:        %s", app.config.state_dir)
    logger.info(
        "  Quix__Deployment__State__Path:    %s",
        os.environ.get("Quix__Deployment__State__Path"),
    )
    logger.info(
        "  Quix__Deployment__State__Enabled: %s",
        os.environ.get("Quix__Deployment__State__Enabled"),
    )
    logger.info("  PROBE:            %s", PROBE)
    logger.info("  EMIT_MODE:        %s", EMIT_MODE)
    logger.info("  CLOSING_STRATEGY: %s", CLOSING_STRATEGY)
    logger.info("  GAP_MS:           %d", GAP_MS)
    logger.info("  GRACE_MS:         %d", GRACE_MS)
    logger.info("  STORE_NAME:       %s", STORE_NAME)
    logger.info("  CONSUMER_GROUP:   %s", CONSUMER_GROUP)
    logger.info("  input topic:      %s", input_topic.name)
    logger.info("  output topic:     %s", output_topic.name)

    sdf = app.dataframe(input_topic)
    window = sdf.session_window(
        inactivity_gap_ms=GAP_MS,
        grace_ms=GRACE_MS,
        name=STORE_NAME,
        on_late=on_late,
    )
    if EMIT_MODE == "final":
        agg = window.agg(count=Count(), seqs=Collect("seq"))
        sdf = agg.final(closing_strategy=CLOSING_STRATEGY)
    else:
        # `current` windows reject collectors (windows/base.py:170), so the seq
        # evidence is the first and last seq instead of the whole list.
        agg = window.agg(
            count=Count(),
            first_seq=Earliest("seq"),
            last_seq=Latest("seq"),
        )
        sdf = agg.current(closing_strategy=CLOSING_STRATEGY)

    sdf = sdf.apply(describe, metadata=True)
    sdf.print(metadata=True)
    sdf.to_topic(output_topic)

    app.run()


if __name__ == "__main__":
    main()
