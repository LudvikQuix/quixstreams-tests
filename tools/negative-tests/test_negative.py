"""Local negative tests for PR1110's LookupBuffer build-time guards.

Run this on a workstation, against the pinned SHA. Nothing here touches a broker: every
case raises before any I/O, and the one case that needs a `join_lookup` call uses a stub
lookup (see `_StubLookup`).

    python tools/negative-tests/test_negative.py

Exit code 0 means every guard fired as specified. A plain script rather than pytest:
six `raises` assertions do not justify a test dependency on a repo whose only other
Python is four deployed services.

The three cases that CANNOT be checked here are deployment variants, because they need a
running pipeline. They are described in this directory's README:
NT3a (overflow -> raise), NT3b (overflow -> drop-newest) and NT8 (a buffered deployment
with no state block).
"""

import sys
from collections.abc import Mapping
from typing import Any

from quixstreams import Application
from quixstreams.dataframe.joins.lookups import (
    BaseLookup,
    LookupBuffer,
    QuixConfigurationServiceJSONField,
)
from quixstreams.models.types import HeadersMapping


class _StubLookup(BaseLookup):
    """Stand-in for QuixConfigurationService in the build-time test.

    The real lookup starts a Kafka consumer thread in its constructor and then BLOCKS on
    that thread reporting the configuration topic drained, so it cannot be constructed
    without a live broker and a real config topic. `join_lookup` calls
    `buffer.validate_fields(fields)` before it touches the lookup at all, so a stub
    exercises exactly the path under test.
    """

    def join(
        self,
        fields: Mapping[str, Any],
        on: str,
        value: dict[str, Any],
        key: Any,
        timestamp: int,
        headers: HeadersMapping,
    ) -> None:
        raise AssertionError("the stub lookup must not be called at build time")


def nt1_field_without_default() -> None:
    """A field with no `default=` must be rejected when a buffer is attached."""
    app = Application(broker_address="localhost:9092", consumer_group="nt1")
    sdf = app.dataframe(topic=app.topic("sensor-data", key_deserializer="str"))
    fields = {
        "threshold": QuixConfigurationServiceJSONField(
            type="device", jsonpath="$.threshold"
        )
    }
    buffer = LookupBuffer(grace_ms=1000, is_resolved=lambda value: True)
    sdf.join_lookup(_StubLookup(), fields, on="device_id", buffer=buffer)


def nt2_zero_grace() -> None:
    LookupBuffer(grace_ms=0, is_resolved=lambda value: True)


def nt4_is_resolved_not_callable() -> None:
    LookupBuffer(grace_ms=1000, is_resolved="nope")


def nt5_bad_on_timeout() -> None:
    LookupBuffer(grace_ms=1000, is_resolved=lambda value: True, on_timeout="discard")


def nt6_bad_on_overflow() -> None:
    LookupBuffer(
        grace_ms=1000, is_resolved=lambda value: True, on_overflow="drop-oldest"
    )


def nt7_zero_max_buffered() -> None:
    LookupBuffer(grace_ms=1000, is_resolved=lambda value: True, max_buffered_per_key=0)


CASES = (
    (
        "NT1",
        "field with no default= under a buffer",
        nt1_field_without_default,
        "threshold",
    ),
    ("NT2", "grace_ms = 0", nt2_zero_grace, "grace_ms"),
    ("NT4", "is_resolved not callable", nt4_is_resolved_not_callable, "is_resolved"),
    ("NT5", "on_timeout = 'discard'", nt5_bad_on_timeout, "on_timeout"),
    ("NT6", "on_overflow = 'drop-oldest'", nt6_bad_on_overflow, "on_overflow"),
    ("NT7", "max_buffered_per_key = 0", nt7_zero_max_buffered, "max_buffered_per_key"),
)


def main() -> int:
    failures = 0
    for test_id, description, case, expected_substring in CASES:
        try:
            case()
        except ValueError as error:
            message = str(error)
            if expected_substring in message:
                print(f"PASS  {test_id}  {description}")
                print(f"      ValueError: {message.splitlines()[0]}")
            else:
                failures += 1
                print(f"FAIL  {test_id}  {description}")
                print(f"      ValueError did not mention {expected_substring!r}:")
                print(f"      {message}")
        except Exception as error:
            # Deliberately broad: the point of this script is to REPORT what a guard did
            # instead of what it should have done, not to propagate it.
            failures += 1
            print(f"FAIL  {test_id}  {description}")
            print(f"      expected ValueError, got {type(error).__name__}: {error}")
        else:
            failures += 1
            print(f"FAIL  {test_id}  {description}")
            print("      no exception raised - the guard did not fire")

    print()
    print(f"{len(CASES) - failures} / {len(CASES)} guards fired as specified")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
