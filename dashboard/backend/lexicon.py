"""Lexicon read path: two DCM configurations -> validate -> one merged snapshot.

D3 makes REST the read path (not join_lookup, which resolves configuration per
record by message key and is an enrichment idiom). D9 makes it *two* reads: the
lexicon is published as `sil-signals` and `sil-parameters`, two configurations
that version independently because parameters are tuned constantly and signals
change only when the plant's interface does.

Each configuration is owned by its own `ConfigCache` (`lexicon_config.py`): its
own deterministic id, its own revision, its own bundled seed file, its own
reason for being absent. This module owns the *pair* - it merges whatever
loaded into the single combined document the rest of the service reads, and is
the only place that knows either half can be missing.

Boot policy is degraded-but-serving, and D9 makes it degraded *per
configuration*. The process never exits over a lexicon: each config that 404s is
seeded from the copy bundled in the image and read straight back, independently
of the other, and whatever loaded is served. Signals without parameters is a
read-only dashboard - charts and readouts draw, controls find nothing to bind.
Parameters without signals is the mirror image and is equally survivable.

The DCM stays the source of truth. A bundle seeds an EMPTY configuration and is
never an overwrite and never a local fallback that is served instead - a lexicon
that disagrees with the running plant makes every control lie, and does it
silently.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .lexicon_config import ConfigCache, config_id, sha256_of
from .lexicon_rules import (
    LexiconError,
    LexiconMissing,
    pair_problems,
    validate_parameters,
    validate_signals,
)
from .settings import Settings

logger = logging.getLogger(__name__)

# Retry cadence while either configuration is missing. LEXICON_REFRESH_S (15 min)
# is a TTL for documents already in hand, not the interval on which a dashboard
# with nothing to draw should notice that the DCM came back.
DEGRADED_RETRY_S = 30.0

# Re-exported so callers keep importing the lexicon vocabulary from one module.
__all__ = [
    "DEGRADED_RETRY_S",
    "ConfigCache",
    "LexiconCache",
    "LexiconError",
    "LexiconMissing",
    "LexiconSnapshot",
    "config_id",
]


@dataclass(frozen=True)
class LexiconSnapshot:
    """An immutable view of the merged pair. Replaced wholesale, never mutated.

    `signals_loaded` / `parameters_loaded` are not derivable from the arrays:
    an empty collection because its configuration is missing and an empty
    collection because the plant genuinely has none are the same document and
    very different news, and only the first is something to tell a user about.
    """

    document: dict[str, Any]
    sha256: str
    rev: int
    fetched_at: float
    signals: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    output_names: frozenset[str] = frozenset()
    signals_loaded: bool = False
    parameters_loaded: bool = False

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


def _index(
    document: dict[str, Any],
    sha: str,
    rev: int,
    *,
    signals_loaded: bool,
    parameters_loaded: bool,
) -> LexiconSnapshot:
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
        signals_loaded=signals_loaded,
        parameters_loaded=parameters_loaded,
    )


class LexiconCache:
    """The pair, merged into the one snapshot everything downstream reads.

    Readers - the consumer thread, the uvicorn loop, the writer thread - take
    the snapshot object once and then read it lock-free: it is frozen and
    replaced whole, so a reader never observes a half-updated index.
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
        self.signals = ConfigCache(
            self._client,
            collection="signals",
            config_type=settings.signals_type,
            target_key=settings.lexicon_target_key,
            seed_path=settings.signals_seed_path,
            seed_enabled=settings.lexicon_seed_enabled,
            validate=validate_signals,
        )
        self.parameters = ConfigCache(
            self._client,
            collection="parameters",
            config_type=settings.parameters_type,
            target_key=settings.lexicon_target_key,
            seed_path=settings.parameters_seed_path,
            seed_enabled=settings.lexicon_seed_enabled,
            validate=validate_parameters,
        )
        self.on_change: list[Callable[[int], None]] = []
        # Why the pair is unusable even though both halves loaded. Kept apart
        # from either configuration's own error because it belongs to neither.
        self._pair_error: str | None = None

    @property
    def configs(self) -> tuple[ConfigCache, ConfigCache]:
        return (self.signals, self.parameters)

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

        The two configurations report separately and are never collapsed into
        one boolean: signals at rev 4 with parameters absent is a working
        read-only dashboard, and it must not read the same as a dashboard with
        nothing at all.
        """
        snapshot = self.snapshot()
        with self._lock:
            pair_error = self._pair_error
        signals = self.signals.state()
        parameters = self.parameters.state()
        return {
            "lexicon_loaded": snapshot is not None,
            "lexicon_rev": snapshot.rev if snapshot else 0,
            "lexicon_target_key": self._settings.lexicon_target_key,
            "lexicon_seed_enabled": self._settings.lexicon_seed_enabled,
            "lexicon_pair_error": pair_error,
            "lexicon_error": _summary(signals, parameters, pair_error),
            "dcm_token_present": bool(self._settings.sdk_token),
            "signals": signals,
            "parameters": parameters,
        }

    def ensure_loaded(self) -> LexiconSnapshot:
        """Load both configurations, independently, then merge what came back.

        The single entry point for boot, the TTL loop and POST
        /api/lexicon/refresh, so all three seed, all three record why they
        failed, and none of them can drift apart. Raises `LexiconError` only
        when the merge yields nothing usable - one half loading is a success
        with a degraded dashboard, not a failure.
        """
        for config in self.configs:
            config.ensure_loaded()
        return self._merge()

    def _merge(self) -> LexiconSnapshot:
        """Build the combined document from whichever halves are in hand.

        A missing half becomes an empty collection, which is what makes the
        degraded states fall out for free: no parameters means the picker offers
        none and every parameter binding resolves to nothing, which the grid
        already renders as unbound. `signals_loaded` / `parameters_loaded` carry
        the distinction between "absent" and "empty" for anything that has to
        say so out loud.
        """
        signals_doc = self.signals.document()
        parameters_doc = self.parameters.document()

        if signals_doc is None and parameters_doc is None:
            self._set_pair_error(None)
            summary = _summary(self.signals.state(), self.parameters.state(), None)
            raise LexiconError(summary or "no lexicon configuration has been read yet")

        if signals_doc is not None and parameters_doc is not None:
            self._check_pair(signals_doc, parameters_doc)
        self._set_pair_error(None)

        # Cannot be None: the both-absent case raised above. The signal document
        # supplies the shared header when both are present, which `_check_pair`
        # has just proved agrees on `model`. `lexicon_version` may differ in its
        # minor - both are major 1 or neither validated - and that is benign: it
        # describes the descriptor shape, and the shape is the same.
        head = signals_doc if signals_doc is not None else parameters_doc
        document = {
            "lexicon_version": head["lexicon_version"],
            "model": head["model"],
            "signals": signals_doc["signals"] if signals_doc else [],
            "parameters": parameters_doc["parameters"] if parameters_doc else [],
        }
        sha = sha256_of(document)

        with self._lock:
            current = self._snapshot
            if current is not None and current.sha256 == sha:
                return current
            rev = 1 if current is None else current.rev + 1
            snapshot = _index(
                document,
                sha,
                rev,
                signals_loaded=signals_doc is not None,
                parameters_loaded=parameters_doc is not None,
            )
            self._snapshot = snapshot

        logger.info(
            "[LEXICON] rev=%s model=%s signals=%s(%s) parameters=%s(%s) sha=%s",
            snapshot.rev,
            snapshot.model_name,
            len(snapshot.signals),
            "loaded" if snapshot.signals_loaded else "MISSING",
            len(snapshot.parameters),
            "loaded" if snapshot.parameters_loaded else "MISSING",
            sha[:12],
        )
        for callback in self.on_change:
            callback(snapshot.rev)
        return snapshot

    def _check_pair(
        self, signals_doc: dict[str, Any], parameters_doc: dict[str, Any]
    ) -> None:
        """Refuse to merge two configurations that do not describe one plant.

        A mismatched pair is a misconfiguration, not a document to merge: one
        plant's signals next to another's parameters would render controls that
        write fields the running plant has never heard of, and the page would
        look perfectly healthy while doing it. The whole lexicon is dropped
        until one of the two configurations is corrected, and the previous
        snapshot goes with it - continuing to serve it would mean the dashboard
        silently ignored a DCM change it had already read.
        """
        problems = pair_problems(signals_doc, parameters_doc)
        if not problems:
            return
        message = "; ".join(problems)
        logger.error(
            "[LEXICON] %s and %s cannot be paired: %s. Serving no lexicon until "
            "one of them is corrected.",
            self.signals.label,
            self.parameters.label,
            message,
        )
        self._set_pair_error(message)
        with self._lock:
            self._snapshot = None
        raise LexiconError(message)

    def _set_pair_error(self, message: str | None) -> None:
        with self._lock:
            self._pair_error = message

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
        """TTL poll, and the recovery path for a boot that found nothing.

        The period is DEGRADED_RETRY_S while either configuration is missing and
        LEXICON_REFRESH_S once both are in hand: a 15-minute TTL is right for
        re-reading documents already held and far too slow for noticing that an
        empty or unreachable DCM became usable. A dashboard running without its
        parameters is degraded even though it has a snapshot, so it polls on the
        fast cadence too. M2 replaces the TTL trigger with the DCM config-topic
        event.
        """
        while True:
            complete = all(config.loaded() for config in self.configs)
            period = self._settings.lexicon_refresh_s if complete else DEGRADED_RETRY_S
            if stop.wait(period):
                return
            try:
                self.ensure_loaded()
            except LexiconError as exc:
                # Deliberate catch: a failed refresh must neither drop the good
                # snapshot already being served nor kill the worker thread.
                logger.warning("[LEXICON] refresh incomplete: %s", exc)


def _summary(
    signals: dict[str, Any], parameters: dict[str, Any], pair_error: str | None
) -> str | None:
    """One line naming everything that is wrong, or None when nothing is.

    A caller that only wants to know whether to page someone reads this; one
    that wants to know *which* configuration to fix reads the two state objects
    next to it in `LexiconCache.state()`. Both are in the body so neither has to
    guess.
    """
    if pair_error:
        return pair_error
    problems = [
        f"{state['type']}: {state['error']}"
        for state in (signals, parameters)
        if not state["loaded"] and state["error"]
    ]
    return "; ".join(problems) if problems else None
