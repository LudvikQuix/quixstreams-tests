"""Runtime configuration for the dashboard service.

One frozen dataclass, built once in `main.py` and handed to every component, so
no other module reads `os.environ` and the whole runtime surface is visible in
one place. Topic names use `os.environ[...]` deliberately: a hardcoded fallback
boots fine but draws no edge in the Quix pipeline graph, so the binding silently
stops being managed (quix-service-create skill, section 2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw or default


def _bool(name: str, default: bool) -> bool:
    """A Portal FreeText flag. Blank means absent, so the default wins."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    telemetry_topic: str
    control_topic: str
    plant_key: str
    config_api_url: str
    sdk_token: str
    signals_type: str
    parameters_type: str
    lexicon_target_key: str
    lexicon_refresh_s: float
    lexicon_boot_timeout_s: float
    lexicon_seed_enabled: bool
    signals_seed_path: str
    parameters_seed_path: str
    history_seconds: float
    history_max_samples: int
    ws_flush_hz: float
    ws_queue_max: int
    ws_stall_timeout_s: float
    write_coalesce_ms: int
    applied_timeout_ms: int
    http_port: int
    static_dir: str
    consumer_group: str
    log_level: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            telemetry_topic=os.environ["telemetry_in"],
            control_topic=os.environ["control_out"],
            plant_key=os.environ["PLANT_KEY"],
            config_api_url=os.environ["CONFIG_API_URL"].rstrip("/"),
            sdk_token=os.environ.get("Quix__Sdk__Token", ""),
            signals_type=_str("SIGNALS_TYPE", "sil-signals"),
            parameters_type=_str("PARAMETERS_TYPE", "sil-parameters"),
            lexicon_target_key=os.environ["LEXICON_TARGET_KEY"],
            lexicon_refresh_s=_float("LEXICON_REFRESH_S", 900.0),
            lexicon_boot_timeout_s=_float("LEXICON_BOOT_TIMEOUT_S", 60.0),
            lexicon_seed_enabled=_bool("LEXICON_SEED_ENABLED", True),
            signals_seed_path=_str("SIGNALS_SEED_PATH", "seed/signals.json"),
            parameters_seed_path=_str("PARAMETERS_SEED_PATH", "seed/parameters.json"),
            history_seconds=_float("HISTORY_SECONDS", 60.0),
            history_max_samples=_int("HISTORY_MAX_SAMPLES", 6000),
            ws_flush_hz=_float("WS_FLUSH_HZ", 10.0),
            ws_queue_max=_int("WS_QUEUE_MAX", 20),
            ws_stall_timeout_s=_float("WS_STALL_TIMEOUT_S", 30.0),
            write_coalesce_ms=_int("WRITE_COALESCE_MS", 50),
            applied_timeout_ms=_int("APPLIED_TIMEOUT_MS", 7000),
            http_port=_int("HTTP_PORT", 8080),
            static_dir=_str("STATIC_DIR", "static"),
            consumer_group=_str("DASHBOARD_CONSUMER_GROUP", "sil-dashboard"),
            log_level=_str("LOG_LEVEL", "INFO"),
        )

    @property
    def client_config(self) -> dict[str, object]:
        """The subset the browser needs. No secret, no topic name, no plant key."""
        return {
            "history_seconds": self.history_seconds,
            "applied_timeout_ms": self.applied_timeout_ms,
            "ws_flush_hz": self.ws_flush_hz,
        }
