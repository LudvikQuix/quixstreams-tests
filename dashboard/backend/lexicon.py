"""Lexicon read path: DCM REST -> validate -> immutable in-memory snapshot.

D3 makes REST the read path (not join_lookup, which resolves configuration per
record by message key and is an enrichment idiom). The document is ~30
descriptors, so the cache is a plain object behind a lock rather than State.

Boot policy is degraded-but-serving. The process never exits over a lexicon: if
the DCM holds none, the copy bundled in the image is POSTed once as a
create-if-absent and read straight back, so a fresh environment self-starts; if
the DCM cannot be reached, the service boots with no snapshot and keeps retrying
while `/healthz` and `/api/lexicon` report exactly that.

The DCM stays the source of truth. The bundle seeds an EMPTY store and is never
an overwrite and never a local fallback that is served instead - a lexicon that
disagrees with the running plant makes every control lie, and does it silently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .settings import Settings

logger = logging.getLogger(__name__)

NUMERIC_TYPES = ("uint", "int", "float")
# Marks a configuration this service created from its bundled copy, so an
# operator can tell a seeded document from one they wrote.
SEED_CATEGORY = "sil-dashboard-seed"
# The seeded version is backdated for the reason config-seeder backdates its
# own: a version whose valid_from is "now" is not valid for anything asking
# about a moment before the write, and this pod's clock is not the DCM's.
SEED_VALID_FROM_BACKDATE_S = 86400
# Retry cadence while nothing is loaded at all. LEXICON_REFRESH_S (15 min) is a
# TTL for a document already in hand, not the interval on which a dashboard with
# nothing to draw should notice that the DCM came back.
DEGRADED_RETRY_S = 30.0
DESCRIPTOR_KEYS = (
    "name",
    "label",
    "description",
    "datatype",
    "unit",
    "default",
    "min",
    "max",
    "enum",
    "direction",
    "tunable",
)


class LexiconError(Exception):
    """The document could not be fetched, parsed, or validated."""


class LexiconMissing(LexiconError):
    """The DCM answered 404: this configuration does not exist yet.

    The one failure the bundled copy is allowed to answer. Every other failure -
    403, 500, a connection error, a malformed document - means the store may
    well hold a lexicon, so writing the bundle over it is never right.
    """


@dataclass(frozen=True)
class LexiconSnapshot:
    """An immutable view. Replaced wholesale on refresh, never mutated."""

    document: dict[str, Any]
    sha256: str
    rev: int
    fetched_at: float
    signals: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_names: frozenset[str] = frozenset()

    @property
    def model_name(self) -> str:
        model = self.document.get("model", {})
        return str(model.get("name", "")) if isinstance(model, dict) else ""

    def descriptor(
        self, collection: str, name: str, direction: str | None
    ) -> dict[str, Any] | None:
        """Resolve a binding triple. Signals are unique on (name, direction)."""
        if collection == "signals":
            return self.signals.get((name, direction or ""))
        if collection == "parameters":
            return self.parameters.get(name)
        return None


def config_id(lexicon_type: str, target_key: str) -> str:
    """DCM's deterministic id, so content is one GET with no search round-trip."""
    digest = hashlib.sha1(f"{lexicon_type}-{target_key}".encode())  # noqa: S324
    return digest.hexdigest()


def _problem(problems: list[str], entry: dict[str, Any], message: str) -> None:
    problems.append(f"{entry.get('name', '<unnamed>')}: {message}")


def _validate_range(entry: dict[str, Any], problems: list[str]) -> None:
    """Load rules 1, 2, 3 and 5 - min/max/enum shape follows from the datatype."""
    datatype = entry["datatype"]
    if datatype == "enum":
        if not isinstance(entry["enum"], list) or not entry["enum"]:
            _problem(
                problems, entry, "load rule 1: enum datatype needs a non-empty enum"
            )
        if entry["min"] is not None or entry["max"] is not None:
            _problem(
                problems, entry, "load rule 1: enum datatype must have null min/max"
            )
        return
    if datatype in NUMERIC_TYPES:
        low, high = entry["min"], entry["max"]
        numeric = isinstance(low, (int, float)) and isinstance(high, (int, float))
        if not numeric:
            _problem(
                problems, entry, "load rule 2: numeric datatype needs numeric min/max"
            )
        elif low > high:
            _problem(problems, entry, "load rule 2: min > max")
        elif datatype == "uint" and low < 0:
            _problem(problems, entry, "load rule 5: uint min must be >= 0")
        if entry["enum"] is not None:
            _problem(
                problems, entry, "load rule 2: numeric datatype must have null enum"
            )
        return
    if any(entry[key] is not None for key in ("min", "max", "enum")):
        _problem(problems, entry, "load rule 3: bool must have null min/max/enum")


def _validate_default(entry: dict[str, Any], problems: list[str]) -> None:
    """Load rule 4 - the startup default must be a value the plant would accept."""
    datatype = entry["datatype"]
    default = entry["default"]
    if datatype == "enum" and isinstance(entry["enum"], list):
        members = [m.get("value") for m in entry["enum"] if isinstance(m, dict)]
        if default not in members:
            _problem(
                problems, entry, "load rule 4: default is not an enum member value"
            )
        return
    if datatype in NUMERIC_TYPES and isinstance(entry["min"], (int, float)):
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            _problem(problems, entry, "load rule 4: default is not numeric")
        elif not entry["min"] <= default <= entry["max"]:
            _problem(problems, entry, "load rule 4: default outside min/max")


def _validate_descriptor(entry: Any, collection: str, problems: list[str]) -> None:
    if not isinstance(entry, dict):
        problems.append(f"{collection}: entry is not an object")
        return
    missing = [key for key in DESCRIPTOR_KEYS if key not in entry]
    if missing:
        _problem(problems, entry, f"missing keys {missing}")
        return
    if entry["datatype"] not in ("bool", "uint", "int", "float", "enum"):
        _problem(problems, entry, f"unknown datatype {entry['datatype']!r}")
        return

    _validate_range(entry, problems)
    _validate_default(entry, problems)

    if collection == "signals":
        if entry["direction"] not in ("input", "output"):
            _problem(problems, entry, "signal needs direction input|output")
        if entry["tunable"] is not None:
            _problem(problems, entry, "signal tunable must be null")
        return
    if entry["direction"] is not None:
        _problem(problems, entry, "parameter direction must be null")
    if not isinstance(entry["tunable"], bool):
        _problem(problems, entry, "parameter tunable must be a boolean")


def _validate_uniqueness(
    signals: list[Any], parameters: list[Any], problems: list[str]
) -> None:
    """Load rule 6 - signals unique on (name, direction), parameters on name, no overlap."""
    signal_keys = [
        (s.get("name"), s.get("direction")) for s in signals if isinstance(s, dict)
    ]
    if len(set(signal_keys)) != len(signal_keys):
        problems.append("load rule 6: duplicate (name, direction) in signals")
    param_names = [p.get("name") for p in parameters if isinstance(p, dict)]
    if len(set(param_names)) != len(param_names):
        problems.append("load rule 6: duplicate name in parameters")
    overlap = {name for name, _ in signal_keys} & set(param_names)
    if overlap:
        problems.append(f"load rule 6: name in both collections: {sorted(overlap)}")


def validate_document(document: Any) -> None:
    """Re-run the six load-time rules of parameter-contract spec section 6.1.

    DCM does not validate content for us, and a malformed descriptor becomes a
    broken control rather than an error anyone sees.
    """
    if not isinstance(document, dict):
        raise LexiconError("lexicon content is not a JSON object")

    version = str(document.get("lexicon_version", ""))
    if version.split(".")[0] != "1":
        raise LexiconError(f"unsupported lexicon_version {version!r}; major must be 1")

    signals = document.get("signals")
    parameters = document.get("parameters")
    if not isinstance(signals, list) or not isinstance(parameters, list):
        raise LexiconError("lexicon needs `signals` and `parameters` arrays")

    problems: list[str] = []
    for entry in signals:
        _validate_descriptor(entry, "signals", problems)
    for entry in parameters:
        _validate_descriptor(entry, "parameters", problems)
    _validate_uniqueness(signals, parameters, problems)
    if problems:
        raise LexiconError("; ".join(problems))


def _index(document: dict[str, Any], sha: str, rev: int) -> LexiconSnapshot:
    signals = {(s["name"], s["direction"]): s for s in document["signals"]}
    outputs = frozenset(
        s["name"] for s in document["signals"] if s["direction"] == "output"
    )
    return LexiconSnapshot(
        document=document,
        sha256=sha,
        rev=rev,
        fetched_at=time.time(),
        signals=signals,
        parameters={p["name"]: p for p in document["parameters"]},
        output_names=outputs,
    )


class LexiconCache:
    """Thread-safe holder of the current snapshot.

    Readers - the consumer thread, the uvicorn loop, the writer thread - take the
    snapshot object once and then read it lock-free: it is frozen and replaced
    whole, so a reader never observes a half-updated index.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._snapshot: LexiconSnapshot | None = None
        self._client = httpx.Client(
            base_url=settings.config_api_url,
            headers={"Authorization": f"Bearer {settings.sdk_token}"},
            timeout=10.0,
        )
        self.config_id = config_id(settings.lexicon_type, settings.lexicon_target_key)
        self.on_change: list[Callable[[int], None]] = []
        # Why there is no snapshot, kept so /healthz and the empty-state page can
        # say it. A degraded process that cannot explain itself is what made the
        # previous build a 503 with nothing behind it.
        self._last_error: str | None = None
        self._seeded = False

    def snapshot(self) -> LexiconSnapshot | None:
        with self._lock:
            return self._snapshot

    def require(self) -> LexiconSnapshot:
        snapshot = self.snapshot()
        if snapshot is None:
            raise LexiconError("lexicon not loaded")
        return snapshot

    def state(self) -> dict[str, Any]:
        """The lexicon half of /healthz, and the body of a 503 /api/lexicon.

        Everything here is already public in `quix.yaml`; the token and the DCM
        URL are not, so neither appears - only whether a token was resolved,
        because "no token" and "DCM down" are the two failures that look alike
        from the outside and the DCM answers 403 to an unauthenticated call.
        """
        with self._lock:
            snapshot = self._snapshot
            error = self._last_error
            seeded = self._seeded
        return {
            "lexicon_loaded": snapshot is not None,
            "lexicon_rev": snapshot.rev if snapshot else 0,
            "lexicon_type": self._settings.lexicon_type,
            "lexicon_target_key": self._settings.lexicon_target_key,
            "lexicon_config_id": self.config_id,
            "lexicon_seed_enabled": self._settings.lexicon_seed_enabled,
            "lexicon_seeded_by_this_pod": seeded,
            "dcm_token_present": bool(self._settings.sdk_token),
            "lexicon_error": error,
        }

    def fetch(self) -> LexiconSnapshot:
        """One GET against the deterministic configuration id.

        `/{id}/content` returns the content object UNWRAPPED, unlike every other
        endpoint on this API; reaching for ["data"] here is a silent KeyError.
        """
        response = self._client.get(f"/api/v1/configurations/{self.config_id}/content")
        if response.status_code == 404:
            raise LexiconMissing(
                f"DCM holds no configuration {self.config_id} "
                f"({self._settings.lexicon_type}/"
                f"{self._settings.lexicon_target_key})"
            )
        if response.status_code != 200:
            raise LexiconError(
                f"DCM returned {response.status_code} for configuration "
                f"{self.config_id} ({self._settings.lexicon_type}/"
                f"{self._settings.lexicon_target_key})"
            )
        document = response.json()
        validate_document(document)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(payload.encode()).hexdigest()

        with self._lock:
            self._last_error = None
            current = self._snapshot
            if current is not None and current.sha256 == sha:
                return current
            rev = 1 if current is None else current.rev + 1
            self._snapshot = _index(document, sha, rev)
            snapshot = self._snapshot

        logger.info(
            "[LEXICON] rev=%s model=%s signals=%s parameters=%s sha=%s",
            snapshot.rev,
            snapshot.model_name,
            len(snapshot.signals),
            len(snapshot.parameters),
            sha[:12],
        )
        for callback in self.on_change:
            callback(snapshot.rev)
        return snapshot

    def ensure_loaded(self) -> LexiconSnapshot:
        """Fetch, and on a 404 seed the bundled copy once and read it back.

        The single entry point for boot, the TTL loop and POST
        /api/lexicon/refresh, so all three seed, all three record why they
        failed, and none of them can drift apart. Raises `LexiconError` only -
        transport failures are wrapped here so no caller has to know this module
        speaks httpx.
        """
        try:
            return self._load()
        except httpx.HTTPError as exc:
            error = LexiconError(
                f"DCM unreachable at {self._settings.config_api_url}: {exc!r}"
            )
            self._remember(str(error))
            raise error from exc
        except LexiconError as exc:
            self._remember(str(exc))
            raise

    def _load(self) -> LexiconSnapshot:
        try:
            return self.fetch()
        except LexiconMissing as exc:
            if not self._settings.lexicon_seed_enabled:
                raise
            logger.warning("[LEXICON] %s - seeding the copy bundled in the image", exc)
        self._seed()
        return self.fetch()

    def _seed(self) -> None:
        """Create-if-absent from the bundled copy. Never an overwrite.

        `replace` is deliberately left at its default of false, which makes this
        POST create the configuration or fail: with `replace: true` every
        restart would push the image's copy over whatever an operator had
        edited in the DCM, which is the opposite of DCM being the source of
        truth. Two replicas racing and a redeploy against an already-seeded DCM
        therefore land on the same harmless branch - the create is declined and
        the fetch that follows reads whatever is actually stored.
        """
        document = self._read_bundle()
        valid_from = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(
            seconds=SEED_VALID_FROM_BACKDATE_S
        )
        body = {
            "metadata": {
                "type": self._settings.lexicon_type,
                "target_key": self._settings.lexicon_target_key,
                "valid_from": valid_from.isoformat(),
                "category": SEED_CATEGORY,
            },
            "content": document,
        }
        response = self._client.post("/api/v1/configurations", json=body)
        if response.status_code in (200, 201):
            with self._lock:
                self._seeded = True
            logger.info(
                "[LEXICON] seeded %s/%s (id=%s) from %s, valid_from=%s",
                self._settings.lexicon_type,
                self._settings.lexicon_target_key,
                self.config_id,
                self._settings.lexicon_seed_path,
                valid_from.isoformat(),
            )
            return
        # Not raised: the likeliest answer here is "already exists", which is
        # exactly what a second replica or a redeploy is supposed to get. The
        # read that follows is what decides whether this boot has a lexicon, so
        # the status and body are logged rather than turned into a failure.
        logger.warning(
            "[LEXICON] seed declined - POST /api/v1/configurations returned %s: %s",
            response.status_code,
            response.text[:200].replace("\n", " "),
        )

    def _read_bundle(self) -> dict[str, Any]:
        """The image's copy, validated before it is ever written to the DCM.

        Same rules as a document read back from the DCM: seeding something this
        service would refuse to read leaves the store poisoned, and the failure
        then surfaces at every future boot instead of here. The explicit
        encoding is load-bearing - the lexicon carries degree signs.
        """
        path = Path(self._settings.lexicon_seed_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LexiconError(f"bundled lexicon {path} is unreadable: {exc}") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LexiconError(f"bundled lexicon {path} is not JSON: {exc}") from exc
        validate_document(document)
        return document

    def _remember(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def load_at_boot(self) -> LexiconSnapshot | None:
        """Retry for LEXICON_BOOT_TIMEOUT_S, then serve degraded rather than exit.

        Exiting here is what made a DCM with no lexicon in it undeployable: the
        process died before uvicorn ever bound, so the platform restarted a pod
        whose HTTP surface had never existed and the ingress answered 503 to
        every route, health check included. A dashboard with no lexicon has
        nothing to draw, but it can say so, it can be seeded while it runs, and
        it must not take the page down with it.
        """
        deadline = time.monotonic() + self._settings.lexicon_boot_timeout_s
        delay = 1.0
        while True:
            try:
                return self.ensure_loaded()
            except LexiconError as exc:
                if time.monotonic() + delay > deadline:
                    logger.error(
                        "[LEXICON] no lexicon after %.0fs: %s. Serving degraded; "
                        "retrying every %.0fs.",
                        self._settings.lexicon_boot_timeout_s,
                        exc,
                        DEGRADED_RETRY_S,
                    )
                    return None
                logger.warning(
                    "[LEXICON] boot fetch failed (%s); retrying in %.0fs", exc, delay
                )
                time.sleep(delay)
                delay = min(delay * 2, 10.0)

    def refresh_loop(self, stop: threading.Event) -> None:
        """TTL poll, and the recovery path for a boot that found no lexicon.

        The period is DEGRADED_RETRY_S while nothing is loaded and
        LEXICON_REFRESH_S once something is: a 15-minute TTL is right for
        re-reading a document already in hand and far too slow for noticing that
        an empty or unreachable DCM became usable. M2 replaces the TTL trigger
        with the DCM config-topic event.
        """
        while True:
            loaded = self.snapshot() is not None
            period = self._settings.lexicon_refresh_s if loaded else DEGRADED_RETRY_S
            if stop.wait(period):
                return
            try:
                self.ensure_loaded()
            except LexiconError as exc:
                # Deliberate catch: a failed refresh must neither drop the good
                # snapshot already being served nor kill the worker thread.
                logger.warning("[LEXICON] refresh failed, keeping current rev: %s", exc)
