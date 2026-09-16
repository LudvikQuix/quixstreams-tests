"""Keyed sensor records for the PR1110 LookupBuffer grace-period rig.

Two properties of this generator are load-bearing for the experiment and must not be
"optimised" away:

* It runs FOREVER, as a Service. `LookupBuffer` has no timer - withheld records are
  released and deadlines are settled only by the arrival of further records. A
  burst-then-exit Job would leave every buffered record frozen in RocksDB and the rig
  would show nothing.
* It cycles the device ids round-robin, never randomly. A device's withheld records
  are flushed by the next record carrying the same key, so round-robin fixes the
  per-key revisit interval at exactly DEVICE_COUNT x SLEEP_SECONDS. Random selection
  over 100 keys has a long tail and would leave dwell times uninterpretable.

It starts producing immediately, into a world where no configuration exists yet. That
gap - not any timestamp manipulation - is what the grace period is measured against.
"""

import logging
import os
import time

from quixstreams import Application
from quixstreams.sources import Source

logger = logging.getLogger(__name__)


KEY_TYPES = ("str", "int")


def _env(name: str, default: str) -> str:
    """Read an env var, treating a blank Portal value as absent.

    The Quix Portal stores a variable with an empty value happily, and
    `os.getenv(name, "100")` then returns "" rather than the default, so a bare
    `int(os.getenv(...))` misreads Portal config at import time.
    """
    raw = os.getenv(name, "").strip()
    return raw if raw else default


DEVICE_COUNT = int(_env("DEVICE_COUNT", "100"))
# Not used to produce anything - the generator never seeds. It is declared and logged
# so the Portal carries the coupling: lookup-sink derives `seeded_expected` from the
# same number, and config-seeder decides how many devices to configure from it. The
# three must agree or `seeded_expected` lies.
SEEDED_DEVICE_COUNT = int(_env("SEEDED_DEVICE_COUNT", "50"))
SLEEP_SECONDS = float(_env("SLEEP_SECONDS", "0.05"))
TIMESTAMP_SKEW_MS = int(_env("TIMESTAMP_SKEW_MS", "0"))
RUN_ID = _env("RUN_ID", "r1")
LOG_EVERY = int(_env("LOG_EVERY", "200"))
KEY_TYPE = _env("KEY_TYPE", "str")
if KEY_TYPE not in KEY_TYPES:
    raise ValueError(f"KEY_TYPE must be one of {KEY_TYPES}, got {KEY_TYPE!r}")


class SensorSource(Source):
    """Produce one record per SLEEP_SECONDS, cycling the device key space."""

    def __init__(
        self,
        name: str,
        device_count: int,
        sleep_seconds: float,
        timestamp_skew_ms: int,
        run_id: str,
        log_every: int,
        key_type: str,
    ) -> None:
        super().__init__(name=name)
        self._device_count = device_count
        self._sleep_seconds = sleep_seconds
        self._timestamp_skew_ms = timestamp_skew_ms
        self._run_id = run_id
        self._log_every = log_every
        self._key_type = key_type
        # Per-device monotonic counter. `seq` is the join key of the paired
        # buffered-vs-control comparison, so it must be per device, not global.
        self._seq = [0] * device_count

    def run(self) -> None:
        produced = 0
        while self.running:
            index = produced % self._device_count
            device_id = f"device-{index:03d}"
            # The message key follows KEY_TYPE; the payload's device_id stays the string
            # form either way, because lookup-sink joins on the value's device_id field.
            key = index if self._key_type == "int" else device_id
            seq = self._seq[index]
            self._seq[index] = seq + 1

            produced_ms = int(time.time() * 1000)
            timestamp_ms = produced_ms + self._timestamp_skew_ms
            payload = {
                "device_id": device_id,
                "seq": seq,
                "value": round(20.0 + (index % 100) * 0.1, 2),
                "timestamp": timestamp_ms,
                "produced_ms": produced_ms,
                "run_id": self._run_id,
            }

            message = self.serialize(key=key, value=payload, timestamp_ms=timestamp_ms)
            self.produce(
                key=message.key,
                value=message.value,
                headers=message.headers,
                timestamp=message.timestamp,
            )

            produced += 1
            if produced % self._log_every == 0:
                logger.info(
                    "produced %d messages, latest %s seq %d, per-device gap %.1f s",
                    produced,
                    device_id,
                    seq,
                    self._device_count * self._sleep_seconds,
                )

            time.sleep(self._sleep_seconds)


def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = Application()
    # KEY_TYPE doubles as the serializer name: "str" and "int" are both entries in the
    # SDK's SERIALIZERS registry, so "int" puts a real int32 on the wire, not digits.
    output_topic = app.topic(name=os.environ["output"], key_serializer=KEY_TYPE)

    logger.info("Starting Data Generator")
    logger.info("  Output topic:   %s", output_topic.name)
    logger.info(
        "  Device space:   %d (device-000 .. device-%03d)",
        DEVICE_COUNT,
        DEVICE_COUNT - 1,
    )
    logger.info(
        "  Rate:           %.3f s/msg (%.1f msg/s total, one per device every %.1f s)",
        SLEEP_SECONDS,
        1.0 / SLEEP_SECONDS,
        DEVICE_COUNT * SLEEP_SECONDS,
    )
    logger.info(
        "  Seeded (info):  %d - must equal config-seeder's SEEDED_DEVICE_COUNT",
        SEEDED_DEVICE_COUNT,
    )
    logger.info("  Timestamp skew: %d ms", TIMESTAMP_SKEW_MS)
    logger.info("  Key type:       %s", KEY_TYPE)
    logger.info("  Run id:         %s", RUN_ID)

    app.add_source(
        SensorSource(
            name="sensor-data-generator",
            device_count=DEVICE_COUNT,
            sleep_seconds=SLEEP_SECONDS,
            timestamp_skew_ms=TIMESTAMP_SKEW_MS,
            run_id=RUN_ID,
            log_every=LOG_EVERY,
            key_type=KEY_TYPE,
        ),
        topic=output_topic,
    )
    app.run()


if __name__ == "__main__":
    main()
