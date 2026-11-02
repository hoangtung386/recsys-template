"""The request handler, free of any HTTP concern.

Contract: every outcome is an ordinary response (HTTP 200 at the edge). Failures
become ``items: []`` plus a ``reason`` so clients keep a single code path, and
*every* request, successful or not, produces one ``rec.served`` record.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from recsys.engine.bucketing import assign_arm
from recsys.engine.bundle import Bundle
from recsys.engine.context import context_from_request
from recsys.engine.errors import RequestError
from recsys.engine.recommend import recommend
from recsys.engine.request import RecRequest, parse_request, served_message
from recsys.engine.result import RecItem
from recsys.engine.schema import Arm, Degraded, Reason, Tier
from recsys.serving.metrics import Metrics
from recsys.serving.registry import RecRegistry
from recsys.serving.sinks import ServedSink

log = logging.getLogger(__name__)


class Ticket:
    """Decides which of two racing parties answers (and logs) a request.

    The handler thread and the HTTP timeout both want to finish a request; only
    the first to call :meth:`claim` does, so ``rec.served`` is never duplicated.
    """

    def __init__(self) -> None:
        """Start unclaimed."""
        self._lock = threading.Lock()
        self._claimed = False

    def claim(self) -> bool:
        """Return ``True`` for exactly one caller."""
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            return True


def _placeholder_request(body: bytes, country: str | None) -> RecRequest:
    """Best-effort identity of an invalid request, so it can still be logged."""
    app, device, request_id = "unknown", "", uuid.uuid4().hex
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, dict):
        app = str(data.get("app") or app)[:64]
        device = str(data.get("device_id") or "")[:128]
        request_id = str(data.get("request_id") or request_id)[:64]
    return RecRequest(
        app=app,
        device_id=device,
        request_id=request_id,
        country=country.upper() if country and len(country) == 2 else None,
    )


class RecService:
    """Turns a request body into a response and a ``rec.served`` record."""

    def __init__(
        self,
        registries: Mapping[str, RecRegistry],
        sink: ServedSink,
        metrics: Metrics,
        *,
        deadline_s: float = 0.03,
        max_inflight: int = 2,
    ) -> None:
        """Wire the service.

        Args:
            registries: One :class:`RecRegistry` per served app.
            sink: Where ``rec.served`` records go.
            metrics: Metrics sink.
            deadline_s: Engine soft deadline.
            max_inflight: Concurrent engine runs. The engine holds the GIL, so
                raising this lowers throughput; scale with processes and pods.
        """
        self._registries = registries
        self._sink = sink
        self._metrics = metrics
        self._deadline_s = deadline_s
        self._slots = threading.BoundedSemaphore(max_inflight)

    # ------------------------------------------------------------- slot control
    def try_acquire(self) -> bool:
        """Take an engine slot without waiting; ``False`` means overloaded."""
        if not self._slots.acquire(blocking=False):
            return False
        self._metrics.inflight.inc()
        return True

    def release(self) -> None:
        """Give a slot back."""
        self._metrics.inflight.dec()
        self._slots.release()

    # ----------------------------------------------------------------- handlers
    def handle(
        self,
        body: bytes,
        country: str | None,
        trace_id: str | None,
        ticket: Ticket,
    ) -> dict[str, Any] | None:
        """Serve one request. Runs on a worker thread and releases its slot.

        Returns ``None`` when the HTTP timeout already answered this request.
        """
        started = time.perf_counter()
        try:
            return self._handle(body, country, trace_id, ticket, started)
        except Exception:
            log.exception("service.unhandled")
            req = _placeholder_request(body, country)
            return self._finish(req, ticket, started, trace_id, reason=Reason.ERROR)
        finally:
            self.release()

    def overload_response(
        self, body: bytes, country: str | None, trace_id: str | None
    ) -> dict[str, Any]:
        """Answer immediately because no engine slot was free."""
        response = self._answer_unserved(
            body, country, trace_id, Reason.OVERLOAD, time.perf_counter()
        )
        assert response is not None  # no ticket, so nobody can have claimed it
        return response

    def timeout_response(
        self,
        body: bytes,
        country: str | None,
        trace_id: str | None,
        ticket: Ticket,
        started: float,
    ) -> dict[str, Any] | None:
        """Answer because the hard timeout passed.

        Returns ``None`` if the handler thread finished first; the caller should
        then use the handler's own response.
        """
        return self._answer_unserved(
            body, country, trace_id, Reason.TIMEOUT, started, ticket
        )

    def _answer_unserved(
        self,
        body: bytes,
        country: str | None,
        trace_id: str | None,
        reason: Reason,
        started: float,
        ticket: Ticket | None = None,
    ) -> dict[str, Any] | None:
        req = self._parse(body, country) or _placeholder_request(body, country)
        return self._finish(req, ticket, started, trace_id, reason=reason)

    # ----------------------------------------------------------------- internals
    def _parse(self, body: bytes, country: str | None) -> RecRequest | None:
        try:
            return parse_request(body, country=country)
        except RequestError:
            return None

    def _handle(
        self,
        body: bytes,
        country: str | None,
        trace_id: str | None,
        ticket: Ticket,
        started: float,
    ) -> dict[str, Any] | None:
        try:
            req = parse_request(body, country=country)
        except RequestError as exc:
            self._metrics.invalid.labels(exc.field).inc()
            return self._finish(
                _placeholder_request(body, country),
                ticket,
                started,
                trace_id,
                reason=Reason.BAD_REQUEST,
            )

        registry = self._registries.get(req.app)
        snapshot = registry.snapshot if registry else None
        if snapshot is None or snapshot.active is None:
            return self._finish(
                req, ticket, started, trace_id, reason=Reason.NO_ARTIFACT
            )
        state = snapshot.state
        experiment = state.experiment
        if state.kill:
            return self._finish(
                req,
                ticket,
                started,
                trace_id,
                reason=Reason.KILL_SWITCH,
                experiment_id=experiment.experiment_id,
            )

        assignment = assign_arm(req.app, req.device_id, experiment)
        arm = assignment.arm
        if assignment.arm in {Arm.HOLDOUT, Arm.CONTROL}:
            return self._finish(
                req,
                ticket,
                started,
                trace_id,
                reason=Reason(arm.value),
                arm=arm,
                experiment_id=experiment.experiment_id,
                model_version=state.version,
            )

        bundle: Bundle = snapshot.bundles.get(assignment.version or "", snapshot.active)
        ctx = context_from_request(bundle, req, state.blocked)
        self._metrics.unknown_ratio.labels(req.app).set(
            ctx.n_unknown / max(len(req.recent), 1)
        )
        try:
            result = recommend(bundle, ctx, deadline_s=self._deadline_s)
        except Exception:
            # Keep the arm and version so a bad canary shows up in its own error rate.
            log.exception("service.engine_failed", extra={"version": bundle.version})
            return self._finish(
                req,
                ticket,
                started,
                trace_id,
                reason=Reason.ERROR,
                arm=arm,
                experiment_id=experiment.experiment_id,
                model_version=bundle.version,
            )
        self._metrics.engine.labels(req.surface).observe(result.engine_seconds)
        if result.degraded:
            self._metrics.degraded.labels(result.degraded.value).inc()

        shadow = arm is Arm.SHADOW
        return self._finish(
            req,
            ticket,
            started,
            trace_id,
            reason=Reason.SHADOW if shadow else None,
            items=result.items,
            tier=result.tier,
            degraded=result.degraded,
            model_version=bundle.version,
            config_hash=bundle.config_hash,
            serve_items=not shadow,
            arm=arm,
            experiment_id=experiment.experiment_id,
        )

    def _finish(
        self,
        req: RecRequest,
        ticket: Ticket | None,
        started: float,
        trace_id: str | None,
        *,
        reason: Reason | None,
        arm: Arm | None = None,
        experiment_id: str | None = None,
        model_version: str | None = None,
        config_hash: str | None = None,
        items: Sequence[RecItem] = (),
        tier: Tier | None = None,
        degraded: Degraded | None = None,
        serve_items: bool = True,
    ) -> dict[str, Any] | None:
        """Build the response and, if this caller wins the ticket, log and count."""
        if ticket is not None and not ticket.claim():
            return None
        elapsed = time.perf_counter() - started
        message = served_message(
            req,
            arm=arm,
            model_version=model_version,
            config_hash=config_hash,
            experiment_id=experiment_id,
            tier=tier,
            reason=reason,
            items=items,
            degraded=degraded,
            latency_ms=elapsed * 1000,
            trace_id=trace_id,
        )
        if not self._sink.send(message):
            self._metrics.served_dropped.inc()

        returned = items if serve_items else ()
        if returned and req.app in self._registries:
            self._metrics.exposure(req.app).add(returned, tier)
        label = reason.value if reason else ""
        self._metrics.requests.labels(
            req.app,
            req.surface,
            arm.value if arm else "",
            tier.value if tier else "",
            label,
        ).inc()
        self._metrics.latency.labels(req.surface).observe(elapsed)
        if not returned:
            self._metrics.empty.labels(label or "empty").inc()

        payload: dict[str, Any] = {
            "request_id": req.request_id,
            "items": [
                {"id": i.id, "score": round(i.score, 5), "src": i.src.value}
                for i in returned
            ],
        }
        optional = {
            "model_version": model_version,
            "arm": arm.value if arm else None,
            "tier": tier.value if tier else None,
            "reason": label or None,
            "degraded": degraded.value if degraded else None,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        return payload
