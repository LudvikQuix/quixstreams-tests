"""Seed the Dynamic Configuration Manager, late and on purpose.

This Job is the instrument of the PR1110 grace-period rig. It sleeps
SEED_DELAY_SECONDS while the generator produces into a world with no configuration at
all, then creates SEEDED_DEVICE_COUNT configurations in one burst. Everything the
buffered arm withheld during that window either resolves (its config arrived inside
grace_ms) or times out (its device was never seeded).

Two details are load-bearing:

* `metadata.valid_from` is BACKDATED. `Configuration.find_valid_version(timestamp)`
  selects the version whose valid_from is at or before the RECORD's timestamp, so a
  configuration stamped "now" can never apply to a record produced a minute ago. Without
  the backdate every pre-seed record stays unresolvable forever, the rig runs green and
  demonstrates nothing.
* The backdate is READ BACK and asserted. A DCM that silently ignored
  metadata.valid_from would produce exactly the same full-looking, meaningless table, so
  this Job exits non-zero rather than let the run proceed.

The DCM's write path is a REST API with no Kafka interface, so plain HTTP POSTs here are
the documented seeding route, not a workaround for a QuixStreams primitive. Every request
carries a bearer token: the DCM answers 403 to an unauthenticated call, in-cluster
included, so the token is resolved and asserted before the delay rather than after it.
"""

import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

REGIONS = ("eu-west", "us-east", "ap-south")
HEARTBEAT_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 30


def _env(name: str, default: str) -> str:
    """Read an env var, treating a blank Portal value as absent."""
    raw = os.getenv(name, "").strip()
    return raw if raw else default


DCM_API_URL = _env("DCM_API_URL", "http://config-api-svc").rstrip("/")
SEED_DELAY_SECONDS = int(_env("SEED_DELAY_SECONDS", "120"))
DEVICE_COUNT = int(_env("DEVICE_COUNT", "100"))
SEEDED_DEVICE_COUNT = int(_env("SEEDED_DEVICE_COUNT", "50"))
CONFIG_TYPE = _env("CONFIG_TYPE", "device")
VALID_FROM_BACKDATE_SECONDS = int(_env("VALID_FROM_BACKDATE_SECONDS", "86400"))
RUN_ID = _env("RUN_ID", "r1")


def config_id(target_key: str) -> str:
    """The DCM's own id derivation: sha1 of "<type>-<target_key>"."""
    return hashlib.sha1(f"{CONFIG_TYPE}-{target_key}".encode()).hexdigest()


def resolve_token() -> tuple[str, str]:
    """Return (token, source-variable-name), or ("", "") when neither source has one.

    The DCM authenticates every request, in-cluster DNS included: without a bearer header
    it answers 403. `Quix__Sdk__Token` is auto-injected into every Quix deployment and is
    the same credential QuixConfigurationService presents when it fetches per-version
    content, so the normal path needs no configuration at all. `DCM_API_TOKEN` stays ahead
    of it as the escape hatch for a DCM that wants a different token for the REST API.
    """
    for name in ("DCM_API_TOKEN", "Quix__Sdk__Token"):
        token = os.getenv(name, "").strip()
        if token:
            return token, name
    return "", ""


def sleep_with_heartbeat(seconds: int) -> None:
    """Sleep, logging every HEARTBEAT_SECONDS so the run can be timed against it."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(HEARTBEAT_SECONDS, remaining))
        remaining = max(0.0, deadline - time.monotonic())
        logger.info("waiting to seed: %.0f s remaining", remaining)


def post_config(
    session: requests.Session, index: int, valid_from: str
) -> requests.Response:
    """Create or version one device configuration.

    `replace: true` creates OR adds a version, which is what makes re-running this Job
    idempotent. PUT /configurations/{id} only updates and 404s on an unknown id. The
    session carries the Authorization header, so nothing is passed per request.
    """
    target_key = f"device-{index:03d}"
    body = {
        "metadata": {
            "type": CONFIG_TYPE,
            "target_key": target_key,
            "valid_from": valid_from,
            "category": f"pr1110-rig-{RUN_ID}",
        },
        "content": {
            # 10.0 + index, so the value itself identifies the device and a lookup that
            # resolved every key to one cached document is visible at a glance.
            "threshold": 10.0 + index,
            "region": REGIONS[index % len(REGIONS)],
            "device": {"name": f"sensor-{index:03d}"},
        },
        "replace": True,
    }
    response = session.post(
        f"{DCM_API_URL}/api/v1/configurations",
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response


def read_back(session: requests.Session, index: int) -> tuple[str, int, str | None]:
    """Fetch one configuration's stored metadata: (id, version, valid_from)."""
    target_key = f"device-{index:03d}"
    identifier = config_id(target_key)
    response = session.get(
        f"{DCM_API_URL}/api/v1/configurations/{identifier}",
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    metadata = response.json()["data"]["metadata"]
    return identifier, metadata.get("version", 0), metadata.get("valid_from")


def same_instant(stored: str | None, sent: datetime) -> bool:
    """Compare a stored ISO8601 timestamp with what was sent, as instants.

    String equality would be wrong: the DCM is free to normalise the offset ("Z" vs
    "+00:00"), re-serialise the microseconds, or store UTC after a conversion. What the
    assertion is actually about is whether the backdate survived at all, which is an
    instant comparison.
    """
    if not stored:
        return False
    parsed = datetime.fromisoformat(stored)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed == sent


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    token, token_source = resolve_token()

    logger.info("Starting Config Seeder")
    logger.info("  DCM API URL:  %s", DCM_API_URL)
    logger.info(
        "  Auth:         %s",
        f"bearer token from {token_source}" if token else "NO TOKEN FOUND",
    )
    logger.info("  Seed delay:   %d s", SEED_DELAY_SECONDS)
    logger.info(
        "  Devices:      seeding %d of %d (device-000 .. device-%03d); the rest stay "
        "permanently unresolvable and produce the timeout outcome",
        SEEDED_DEVICE_COUNT,
        DEVICE_COUNT,
        SEEDED_DEVICE_COUNT - 1,
    )
    logger.info("  Config type:  %s", CONFIG_TYPE)
    logger.info("  Backdate:     %d s", VALID_FROM_BACKDATE_SECONDS)
    logger.info("  Run id:       %s", RUN_ID)

    # Before the delay, not after it: the DCM 403s every unauthenticated request, so a
    # missing token would otherwise burn the whole SEED_DELAY_SECONDS experiment window
    # and only surface at the first POST, with the generator already producing.
    if not token:
        logger.error(
            "No DCM bearer token available. The DCM rejects unauthenticated requests "
            "with 403, in-cluster service DNS included. Set DCM_API_TOKEN, or run this "
            "Job in a Quix deployment where Quix__Sdk__Token is injected. Exiting now "
            "rather than after the %d s seed delay.",
            SEED_DELAY_SECONDS,
        )
        return 2

    # One Session, one Authorization header, every request: POSTs and readback GETs alike.
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    started = time.monotonic()
    sleep_with_heartbeat(SEED_DELAY_SECONDS)
    seeding_started = time.monotonic()

    # Whole seconds: sub-second precision is the one part of the timestamp a store is
    # free to round, and the readback assertion below compares instants exactly.
    valid_from_dt = (
        datetime.now(timezone.utc) - timedelta(seconds=VALID_FROM_BACKDATE_SECONDS)
    ).replace(microsecond=0)
    valid_from = valid_from_dt.isoformat()
    logger.info("seeding now, valid_from=%s", valid_from)

    for index in range(SEEDED_DEVICE_COUNT):
        post_config(session, index, valid_from)
    seeding_elapsed = time.monotonic() - seeding_started

    first, last = 0, SEEDED_DEVICE_COUNT - 1
    checks = []
    for index in (first, last):
        identifier, version, stored = read_back(session, index)
        matched = same_instant(stored, valid_from_dt)
        checks.append(matched)
        logger.info(
            "readback device-%03d: id=%s  version=%s  valid_from=%s  %s",
            index,
            identifier,
            version,
            stored,
            "MATCH" if matched else "MISMATCH",
        )

    logger.info(
        "seeded:          %d / %d devices (device-%03d .. device-%03d)",
        SEEDED_DEVICE_COUNT,
        DEVICE_COUNT,
        first,
        last,
    )
    logger.info("config type:     %s", CONFIG_TYPE)
    logger.info(
        "valid_from sent: %s   (backdate %d s)",
        valid_from,
        VALID_FROM_BACKDATE_SECONDS,
    )
    logger.info(
        "elapsed:         %.1f s  (delay %d s + %.1f s seeding)",
        time.monotonic() - started,
        SEED_DELAY_SECONDS,
        seeding_elapsed,
    )

    if not all(checks):
        logger.error(
            "valid_from was NOT stored as sent. This DCM does not honour "
            "metadata.valid_from, so find_valid_version() will reject every pre-seed "
            "record and the rig would run green while proving nothing."
        )
        logger.error(
            "Escape hatch: set TIMESTAMP_SKEW_MS on data-generator above "
            "SEED_DELAY_SECONDS*1000 (e.g. 300000), switch lake-sink's "
            "TIMESTAMP_COLUMN to produced_ms, bump RUN_ID and all consumer groups, "
            "and re-run."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
