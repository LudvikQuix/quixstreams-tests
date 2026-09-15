"""Lexicon read path: DCM REST -> validate -> immutable in-memory snapshot.

D3 makes REST the read path (not join_lookup, which resolves configuration per
record by message key and is an enrichment idiom). The document is ~30
descriptors, so the cache is a plain object behind a lock rather than State.

Boot policy is fail-loud: no fallback copy of the lexicon ships in the image,
because a lexicon that disagrees with the running plant makes every control lie,
and does it silently.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import Settings

logger = logging.getLogger(__name__)

NUMERIC_TYPES = ("uint", "int", "float")
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
            _problem(problems, entry, "load rule 1: enum datatype needs a non-empty enum")
        if entry["min"] is not None or entry["max"] is not None:
            _problem(problems, entry, "load rule 1: enum datatype must have null min/max")
        return
    if datatype in NUMERIC_TYPES:
        low, high = entry["min"], entry["max"]
        numeric = isinstance(low, (int, float)) and isinstance(high, (int, float))
        if not numeric:
            _problem(problems, entry, "load rule 2: numeric datatype needs numeric min/max")
        elif low > high:
            _problem(problems, entry, "load rule 2: min > max")
        elif datatype == "uint" and low < 0:
            _problem(problems, entry, "load rule 5: uint min must be >= 0")
        if entry["enum"] is not None:
            _problem(problems, entry, "load rule 2: numeric datatype must have null enum")
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
            _problem(problems, entry, "load rule 4: default is not an enum member value")
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

    def snapshot(self) -> LexiconSnapshot | None:
        with self._lock:
            return self._snapshot

    def require(self) -> LexiconSnapshot:
        snapshot = self.snapshot()
        if snapshot is None:
            raise LexiconError("lexicon not loaded")
        return snapshot

    def fetch(self) -> LexiconSnapshot:
        """One GET against the deterministic configuration id.

        `/{id}/content` returns the content object UNWRAPPED, unlike every other
        endpoint on this API; reaching for ["data"] here is a silent KeyError.
        """
        response = self._client.get(f"/api/v1/configurations/{self.config_id}/content")
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

    def load_at_boot(self) -> LexiconSnapshot:
        """Retry with exponential backoff, then die so the platform restarts us."""
        deadline = time.monotonic() + self._settings.lexicon_boot_timeout_s
        delay = 1.0
        last: Exception | None = None
        while True:
            try:
                return self.fetch()
            except (LexiconError, httpx.HTTPError) as exc:
                last = exc
                if time.monotonic() + delay > deadline:
                    raise LexiconError(f"lexicon unavailable at boot: {exc}") from exc
                logger.warning(
                    "[LEXICON] boot fetch failed (%s); retrying in %.0fs", exc, delay
                )
                time.sleep(delay)
                delay = min(delay * 2, 10.0)

    def refresh_loop(self, stop: threading.Event) -> None:
        """TTL poll. M2 replaces the trigger with the DCM config-topic event."""
        while not stop.wait(self._settings.lexicon_refresh_s):
            try:
                self.fetch()
            except (LexiconError, httpx.HTTPError) as exc:
                # Deliberate catch: a failed refresh must neither drop the good
                # snapshot already being served nor kill the worker thread.
                logger.warning("[LEXICON] refresh failed, keeping current rev: %s", exc)
