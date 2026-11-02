"""FastAPI application: ``/v1/rec``, ``/healthz``, ``/readyz``, ``/metrics``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from recsys.logging_config import configure_logging
from recsys.serving.metrics import CONTENT_TYPE, Metrics
from recsys.serving.models import RecResponse
from recsys.serving.registry import RecRegistry
from recsys.serving.service import RecService, Ticket
from recsys.serving.sinks import JsonlSink, NullSink, ServedSink
from recsys.settings import Settings, get_settings


def _trace_id(request: Request) -> str | None:
    """Take the trace id from ``x-trace-id`` or a W3C ``traceparent`` header."""
    explicit = request.headers.get("x-trace-id")
    if explicit:
        return explicit[:64]
    parts = request.headers.get("traceparent", "").split("-")
    return parts[1] if len(parts) == 4 and len(parts[1]) == 32 else None


def _catalog_size(registries: dict[str, RecRegistry], app: str) -> int:
    """Catalog size of the active bundle of ``app`` (0 while nothing is loaded)."""
    registry = registries.get(app)
    snapshot = registry.snapshot if registry else None
    bundle = snapshot.active if snapshot else None
    return bundle.n_items if bundle else 0


def create_app(  # noqa: PLR0915 - routes are closures over the settings
    settings: Settings | None = None, *, sink: ServedSink | None = None
) -> FastAPI:
    """Build the application.

    Args:
        settings: Defaults to the process settings.
        sink: Override the ``rec.served`` destination (tests).
    """
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(cfg.log_level, json_output=cfg.log_json)
        metrics = Metrics()
        registries = {
            name: RecRegistry(
                cfg.artifact_dir,
                name,
                interval_s=cfg.refresh_interval_s,
                jitter_s=cfg.refresh_jitter_s,
                verify=cfg.verify_artifacts,
                metrics=metrics,
            )
            for name in cfg.apps
        }
        metrics.attach_catalog_size(lambda name: _catalog_size(registries, name))
        for registry in registries.values():
            registry.start()
        served: ServedSink = sink or (
            JsonlSink(
                cfg.served_log_path,
                max_queue=cfg.served_queue_size,
                on_drop=metrics.served_dropped.inc,
            )
            if cfg.served_log_path
            else NullSink()
        )
        pool = ThreadPoolExecutor(
            max_workers=cfg.max_inflight, thread_name_prefix="rec"
        )
        app.state.metrics = metrics
        app.state.registries = registries
        app.state.service = RecService(
            registries,
            served,
            metrics,
            deadline_s=cfg.engine_deadline_s,
            max_inflight=cfg.max_inflight,
        )
        app.state.pool = pool
        try:
            yield
        finally:
            for registry in registries.values():
                registry.stop()
            pool.shutdown(wait=True)
            served.close()

    app = FastAPI(title="rec-serve", version="1.0.0", lifespan=lifespan)

    @app.post(
        "/v1/rec",
        responses={200: {"model": RecResponse}},
        summary="Recommend items (always 200; failures set `reason`)",
    )
    async def rec(request: Request) -> JSONResponse:
        started = time.perf_counter()
        body = await request.body()
        country = request.headers.get("x-country")
        trace_id = _trace_id(request)
        service: RecService = request.app.state.service
        if not service.try_acquire():
            return JSONResponse(service.overload_response(body, country, trace_id))

        ticket = Ticket()
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(
                request.app.state.pool, service.handle, body, country, trace_id, ticket
            )
        except BaseException:
            service.release()
            raise
        payload: dict[str, Any] | None
        try:
            payload = await asyncio.wait_for(asyncio.shield(future), cfg.hard_timeout_s)
        except TimeoutError:
            # The client is answered now; the worker thread finishes in the
            # background and releases its slot, so work never piles up unseen.
            payload = service.timeout_response(body, country, trace_id, ticket, started)
            if payload is None:
                payload = await future
        return JSONResponse(payload)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        """Liveness: the process is up."""
        return {"ok": True}

    @app.get("/readyz")
    def readyz(request: Request, response: Response) -> dict[str, Any]:
        """Readiness: every configured app has an active bundle loaded."""
        registries: dict[str, RecRegistry] = request.app.state.registries
        versions = {name: reg.version for name, reg in registries.items()}
        ready = all(reg.ready for reg in registries.values())
        response.status_code = 200 if ready else 503
        return {"ready": ready, "versions": versions}

    if cfg.metrics_enabled:

        @app.get("/metrics")
        def prometheus(request: Request) -> Response:
            """Prometheus exposition."""
            return Response(request.app.state.metrics.render(), media_type=CONTENT_TYPE)

    return app
