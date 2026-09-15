"""The DCM client for ONE lexicon configuration.

D9 turned the lexicon into two configurations that version independently, so
everything that used to be "the lexicon client" is now per-configuration and
happens twice. This module is that half: it knows a type, a target key, a
deterministic id, a validator and a bundled seed file, and nothing about the
other configuration or about how the two are merged. `lexicon.py` owns the pair.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .lexicon_rules import LexiconError, LexiconMissing

logger = logging.getLogger(__name__)

# Marks a configuration this service created from its bundled copy, so an
# operator can tell a seeded document from one they wrote.
SEED_CATEGORY = "sil-dashboard-seed"
# The seeded version is backdated for the reason config-seeder backdates its
# own: a version whose valid_from is "now" is not valid for anything asking
# about a moment before the write, and this pod's clock is not the DCM's.
SEED_VALID_FROM_BACKDATE_S = 86400


def config_id(config_type: str, target_key: str) -> str:
    """DCM's deterministic id, so content is one GET with no search round-trip."""
    digest = hashlib.sha1(f"{config_type}-{target_key}".encode())  # noqa: S324
    return digest.hexdigest()


def sha256_of(document: dict[str, Any]) -> str:
    """Canonical hash of a document, so an unchanged re-read is a no-op."""
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class ConfigCache:
    """One DCM configuration: read it, seed it if it is absent, remember why not.

    Deliberately never raises out of `ensure_loaded`. The two configurations are
    independent by D9, and an exception escaping here would make one missing
    document stop the other from being read or seeded - which is exactly the
    coupling the split exists to remove. The failure is recorded instead, and
    `LexiconCache` decides what a half-loaded pair means.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        collection: str,
        config_type: str,
        target_key: str,
        seed_path: str,
        seed_enabled: bool,
        validate: Callable[[Any], None],
    ) -> None:
        self._client = client
        self.collection = collection
        self.config_type = config_type
        self.target_key = target_key
        self.config_id = config_id(config_type, target_key)
        self._seed_path = seed_path
        self._seed_enabled = seed_enabled
        self._validate = validate
        self._lock = threading.Lock()
        self._document: dict[str, Any] | None = None
        self._sha: str | None = None
        self._rev = 0
        self._error: str | None = None
        self._seeded = False

    @property
    def label(self) -> str:
        return f"{self.config_type}/{self.target_key}"

    def document(self) -> dict[str, Any] | None:
        with self._lock:
            return self._document

    def loaded(self) -> bool:
        return self.document() is not None

    def state(self) -> dict[str, Any]:
        """This configuration's half of /healthz. Never merged with the other's.

        Collapsing the two into one boolean is what would hide the case this
        whole split has to make visible: signals at rev 4 and parameters absent
        is a working dashboard with no controls, and it must not read the same
        as a dashboard with nothing at all.
        """
        with self._lock:
            document = self._document
            error = self._error
            rev = self._rev
            seeded = self._seeded
        entries = document.get(self.collection, []) if document else []
        return {
            "loaded": document is not None,
            "rev": rev,
            "type": self.config_type,
            "target_key": self.target_key,
            "config_id": self.config_id,
            "count": len(entries),
            "seeded_by_this_pod": seeded,
            "error": error,
        }

    def ensure_loaded(self) -> bool:
        """Fetch, and on a 404 seed the bundled copy once and read it back.

        Returns whether a document is in hand afterwards - including one read on
        an earlier attempt, because a refresh that fails must not drop a good
        document that is already being served.
        """
        try:
            self._load()
        except httpx.HTTPError as exc:
            self._remember(f"DCM unreachable at {self._client.base_url}: {exc!r}")
        except LexiconError as exc:
            self._remember(str(exc))
        return self.loaded()

    def _load(self) -> None:
        try:
            self._fetch()
            return
        except LexiconMissing as exc:
            if not self._seed_enabled:
                raise
            logger.warning("[LEXICON] %s - seeding the copy bundled in the image", exc)
        self._seed()
        self._fetch()

    def _fetch(self) -> None:
        """One GET against the deterministic configuration id.

        `/{id}/content` returns the content object UNWRAPPED, unlike every other
        endpoint on this API; reaching for ["data"] here is a silent KeyError.
        """
        response = self._client.get(f"/api/v1/configurations/{self.config_id}/content")
        if response.status_code == 404:
            raise LexiconMissing(
                f"DCM holds no configuration {self.config_id} ({self.label})"
            )
        if response.status_code != 200:
            raise LexiconError(
                f"DCM returned {response.status_code} for configuration "
                f"{self.config_id} ({self.label})"
            )
        document = response.json()
        self._validate(document)
        sha = sha256_of(document)

        with self._lock:
            self._error = None
            if self._sha == sha:
                return
            self._document = document
            self._sha = sha
            self._rev += 1
            rev = self._rev

        logger.info(
            "[LEXICON] %s rev=%s model=%s entries=%s sha=%s",
            self.label,
            rev,
            document["model"]["name"],
            len(document[self.collection]),
            sha[:12],
        )

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
                "type": self.config_type,
                "target_key": self.target_key,
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
                "[LEXICON] seeded %s (id=%s) from %s, valid_from=%s",
                self.label,
                self.config_id,
                self._seed_path,
                valid_from.isoformat(),
            )
            return
        # Not raised: the likeliest answer here is "already exists", which is
        # exactly what a second replica or a redeploy is supposed to get. The
        # read that follows is what decides whether this boot has a document, so
        # the status and body are logged rather than turned into a failure.
        logger.warning(
            "[LEXICON] seed of %s declined - POST /api/v1/configurations "
            "returned %s: %s",
            self.label,
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
        path = Path(self._seed_path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LexiconError(f"bundled {path} is unreadable: {exc}") from exc
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LexiconError(f"bundled {path} is not JSON: {exc}") from exc
        self._validate(document)
        return document

    def _remember(self, message: str) -> None:
        with self._lock:
            self._error = message
