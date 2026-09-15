"""FastAPI surface: JSON API, the WebSocket, and the exported Next.js bundle.

One process serves the page, the API and the socket from one origin. That is
what removes the CORS surface, the second public URL and the
WebSocket-through-a-proxy problem in one move (spec 6.1, shape C).

Route order matters: the static catch-all is mounted LAST, because a Mount at
"/" shadows every route registered after it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .hub import Hub
from .lexicon import LexiconCache, LexiconError
from .settings import Settings
from .window import RollingWindow
from .writer import ControlWriter

logger = logging.getLogger(__name__)


class ControlBody(BaseModel):
    """The same body as the WebSocket `write` frame - one code path, two entry
    points (the UI, and scripting or e2e)."""

    signals: dict[str, Any] = Field(default_factory=dict)
    parameters: dict[str, Any] = Field(default_factory=dict)


def create_app(
    settings: Settings,
    lexicon: LexiconCache,
    window: RollingWindow,
    hub: Hub,
    writer: ControlWriter,
) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(hub.run())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="SIL Dashboard", lifespan=lifespan)

    # Two paths, one handler: `/healthz` is the spec's route table, `/api/healthz`
    # is where a caller who knows the rest of this API looks. The alias is out of
    # the schema so it cannot collide with the canonical route's operation id.
    @app.get("/api/healthz", include_in_schema=False)
    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness, plus an honest account of both lexicon configurations.

        200 even with no lexicon, deliberately: the process is up, it is serving
        the page, and it can be seeded where it stands. A 503 here cannot be
        told apart from the ingress's own 503 for "no pod is running", which is
        precisely the confusion the old boot-time SystemExit produced.

        `status` is three-valued since D9, because two independently versioned
        configurations have three outcomes and not two: `ok` (both in hand),
        `partial` (one of them, so the dashboard runs with that half only), and
        `degraded` (neither, or a pair that cannot be merged). A probe that only
        understands up/down still gets `lexicon_loaded`; one that needs to know
        which half is missing reads `signals` and `parameters`.
        """
        snapshot = lexicon.snapshot()
        if snapshot is None:
            status = "degraded"
        elif snapshot.signals_loaded and snapshot.parameters_loaded:
            status = "ok"
        else:
            status = "partial"
        body = {
            "status": status,
            "window_rows": window.rows(),
            "dropped_frames": hub.total_dropped,
            **lexicon.state(),
            **hub.status(),
        }
        return JSONResponse(body, status_code=200)

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        """Runtime knobs the browser needs to render honestly (history depth,
        how long to wait for an `applied` before showing `unconfirmed`)."""
        return dict(settings.client_config)

    @app.get("/api/lexicon")
    def get_lexicon() -> JSONResponse:
        snapshot = lexicon.snapshot()
        if snapshot is None:
            # 503 with the reason attached, not a bare status. This body is what
            # the empty-state page renders, so it has to carry enough for a user
            # to know whether to wait, to seed, or to fix CONFIG_API_URL.
            state = lexicon.state()
            detail = state["lexicon_error"] or "lexicon not loaded yet"
            return JSONResponse({"detail": detail, **state}, status_code=503)
        # The two per-configuration states ride along on the success answer too,
        # not only on the 503: a document with `parameters: []` is a perfectly
        # valid lexicon, and only `parameters.loaded` tells the page whether the
        # controls are missing or the plant simply has none.
        state = lexicon.state()
        return JSONResponse(
            {
                "rev": snapshot.rev,
                "sha256": snapshot.sha256,
                "fetched_at": snapshot.fetched_at,
                "document": snapshot.document,
                "signals": state["signals"],
                "parameters": state["parameters"],
            }
        )

    @app.post("/api/lexicon/refresh")
    def refresh_lexicon() -> dict[str, Any]:
        """Also the "retry" button on the empty state.

        `ensure_loaded` rather than `fetch`, so this retries the seed as well as
        the read: a user who has just fixed the DCM, or who wants the bundled
        copy written now, should not have to wait out the poll or redeploy.
        """
        try:
            snapshot = lexicon.ensure_loaded()
        except LexiconError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"rev": snapshot.rev, "sha256": snapshot.sha256}

    @app.get("/api/snapshot")
    def get_snapshot(signals: str = "", seconds: float | None = None) -> dict[str, Any]:
        names = [name for name in signals.split(",") if name]
        payload = window.snapshot(names, seconds or settings.history_seconds)
        applied, applied_ts = window.applied()
        payload["applied"] = applied
        payload["applied_ts"] = applied_ts
        return payload

    @app.post("/api/control")
    def post_control(body: ControlBody) -> JSONResponse:
        try:
            errors = writer.submit(body.signals, body.parameters)
        except LexiconError as exc:
            # Same 503-plus-detail shape as GET /api/lexicon: with no lexicon
            # there is nothing to validate a write against. Reachable now that
            # boot no longer blocks the HTTP thread on the lexicon.
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if errors:
            return JSONResponse({"errors": errors}, status_code=422)
        return JSONResponse({"accepted": True}, status_code=202)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await hub.serve(websocket)

    @app.websocket("/ws/echo")
    async def websocket_echo(websocket: WebSocket) -> None:
        """M1 task 0: proves the upgrade survives the Quix public ingress.

        Kept until the first deployed build has been checked end to end; M2
        removes it. It touches no plant state.
        """
        await websocket.accept()
        try:
            while True:
                received = await websocket.receive_text()
                await websocket.send_text(f"echo:{received}")
        except (WebSocketDisconnect, RuntimeError):
            return

    _mount_static(app, settings.static_dir)
    return app


def _mount_static(app: FastAPI, static_dir: str) -> None:
    """Serve the exported Next.js bundle, with an SPA fallback to index.html.

    The directory is absent when the backend runs from a source checkout without
    a frontend build; that is a normal dev path, so it warns instead of failing.
    """
    if not os.path.isdir(static_dir):
        logger.warning("[HTTP] static dir %s not found - API only", static_dir)
        return

    index = os.path.join(static_dir, "index.html")

    @app.exception_handler(404)
    async def spa_fallback(request: Any, _: Any) -> Any:
        path = request.url.path
        if path.startswith(("/api", "/ws", "/healthz")) or not os.path.isfile(index):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(index)

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
