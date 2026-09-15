"""SIL dashboard service - one deployment, one image, one process.

Threads:
  main      consumer_app.run(sdf)  - dashboard-out -> rolling window -> WS hub
  http      uvicorn                - static bundle, JSON API, /ws
  writer    the single producer    - coalesced writes -> dashboard-in
  lexicon   TTL re-fetch from DCM

app.run() installs the SIGINT/SIGTERM handlers, so it must own the main thread;
everything else is a supervised worker. The supervisor is the guard Phase 1
earned: a daemon thread dying silently left the deployment green while it served
a frozen page. On any worker exception we log CRITICAL and stop the Application
so __main__ can exit non-zero and the platform restarts the pod.

A missing lexicon is NOT one of those exceptions. The lexicon is two independent
DCM configurations (D9), and either can be absent: the service boots without
them, seeds each empty one from its bundled copy, keeps retrying in the
background, and reports both revisions and both failures separately on /healthz.
Exiting instead is what made an un-seeded DCM an undeployable dashboard.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import uvicorn
from quixstreams import Application

from backend.api import create_app
from backend.hub import Hub
from backend.lexicon import LexiconCache
from backend.settings import Settings
from backend.window import RollingWindow
from backend.writer import ControlWriter

logger = logging.getLogger("dashboard")

# uvicorn rejects a level name it does not know, and LOG_LEVEL is shared with the
# stdlib logger, which accepts more spellings than uvicorn does.
UVICORN_LEVELS = frozenset(("critical", "error", "warning", "info", "debug", "trace"))

settings = Settings.from_env()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)

window = RollingWindow(settings.history_seconds, settings.history_max_samples)
lexicon = LexiconCache(settings)
writer = ControlWriter(settings, lexicon)
hub = Hub(settings, window, lexicon, writer.submit)
lexicon.on_change.append(hub.publish_lexicon)

_failed = threading.Event()
_stop = threading.Event()
_unknown_keys = 0


def is_telemetry(value: Any) -> bool:
    """Shape check as a filter, never a try/except inside the step - an exception
    in sdf.update takes the whole application down."""
    return isinstance(value, dict)


def ingest(value: dict[str, Any], _key: Any, timestamp: int, _headers: Any) -> None:
    """Fold one dashboard-out message into the window and fan it out.

    The sample time is the Kafka message timestamp, not a payload field: the
    field name would be plant-specific, the broker timestamp is not. When the
    broker supplies none, the wall clock is the only model-agnostic fallback.
    """
    global _unknown_keys

    snapshot = lexicon.snapshot()
    if snapshot is None:
        return
    ts_ms = int(timestamp) if timestamp else int(time.time() * 1000)

    applied = value.get("applied")
    if isinstance(applied, dict):
        window.set_applied(applied, ts_ms)
        hub.publish_applied(applied, ts_ms)

    row: dict[str, float] = {}
    for name, raw in value.items():
        if name == "applied":
            continue
        if name not in snapshot.output_names:
            _unknown_keys += 1
            if _unknown_keys % 1000 == 1:
                # Once per 1000, not per message: at 10 Hz per-message logging
                # makes the deployment log useless.
                logger.warning(
                    "[INGEST] %s payload keys not described as output signals "
                    "(most recent: %s)",
                    _unknown_keys,
                    name,
                )
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        row[name] = float(raw)

    window.append(ts_ms, row)
    hub.publish_sample(ts_ms, row)


def supervise(name: str, target: Any, consumer_app: Application) -> None:
    try:
        target()
    except BaseException:
        logger.critical("[%s] worker died - stopping the service.", name, exc_info=True)
        _failed.set()
        consumer_app.stop(fail=True)


def writer_body(producer_app: Application) -> None:
    """The only thread that ever produces, so producer thread-safety is moot."""
    control_topic = producer_app.topic(settings.control_topic, value_serializer="json")
    with producer_app.get_producer() as producer:
        writer.run(producer, control_topic, _stop)


def http_body() -> None:
    level = settings.log_level.lower()
    config = uvicorn.Config(
        create_app(settings, lexicon, window, hub, writer),
        host="0.0.0.0",
        port=settings.http_port,
        log_level=level if level in UVICORN_LEVELS else "info",
        ws="websockets",
        access_log=False,
    )
    uvicorn.Server(config).run()


def log_startup() -> None:
    snapshot = lexicon.snapshot()
    logger.info(
        "[STARTUP] topics: telemetry_in=%s control_out=%s consumer_group=%s",
        settings.telemetry_topic,
        settings.control_topic,
        settings.consumer_group,
    )
    logger.info(
        "[STARTUP] lexicon: target=%s rev=%s model=%s",
        settings.lexicon_target_key,
        snapshot.rev if snapshot else 0,
        snapshot.model_name if snapshot else "<none>",
    )
    # One line per DCM configuration (D9). They load and fail independently, so
    # a single "lexicon ok" line would hide the case this split makes possible:
    # signals in hand, parameters absent, controls quietly unavailable.
    for config in lexicon.configs:
        state = config.state()
        logger.info(
            "[STARTUP]   %s id=%s rev=%s entries=%s%s",
            config.label,
            config.config_id,
            state["rev"],
            state["count"],
            "" if state["loaded"] else f" MISSING: {state['error']}",
        )
    if snapshot is None or not (snapshot.signals_loaded and snapshot.parameters_loaded):
        # Not a fatal line, but the first one anyone will look for: the page
        # renders an empty or read-only state until this clears.
        logger.warning(
            "[STARTUP] DEGRADED - seed_enabled=%s dcm=%s token=%s. "
            "Retrying in the background.",
            settings.lexicon_seed_enabled,
            settings.config_api_url,
            "present" if settings.sdk_token else "MISSING",
        )
    logger.info(
        "[STARTUP] window=%.0fs/%d samples  ws_flush=%.1fHz  plant_key=%s  port=%d",
        settings.history_seconds,
        settings.history_max_samples,
        settings.ws_flush_hz,
        settings.plant_key,
        settings.http_port,
    )


if __name__ == "__main__":
    # Returns None instead of raising when the DCM has nothing and cannot be
    # seeded. Serving an empty state beats a pod that never binds a port: the
    # lexicon worker keeps retrying, and one seed or one DCM write fixes it
    # without a redeploy.
    lexicon.load_at_boot()

    producer_app = Application(consumer_group=f"{settings.consumer_group}-prod")
    consumer_app = Application(
        consumer_group=settings.consumer_group,
        auto_offset_reset="latest",
    )
    telemetry_topic = consumer_app.topic(
        settings.telemetry_topic, value_deserializer="json"
    )

    log_startup()

    workers = (
        ("http", http_body),
        ("writer", lambda: writer_body(producer_app)),
        ("lexicon", lambda: lexicon.refresh_loop(_stop)),
    )
    for name, body in workers:
        threading.Thread(
            target=supervise,
            args=(name, body, consumer_app),
            name=name,
            daemon=True,
        ).start()

    sdf = consumer_app.dataframe(telemetry_topic)
    sdf = sdf.filter(is_telemetry).update(ingest, metadata=True)
    consumer_app.run(sdf)

    _stop.set()
    if _failed.is_set():
        raise SystemExit(1)
