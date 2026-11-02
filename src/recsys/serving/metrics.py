"""Prometheus metrics of the serving layer.

Each :class:`Metrics` owns its registry, so tests (and several apps in one
process) never collide on the global default registry.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from recsys.serving.health import ExposureTracker, HealthCollector

LATENCY_BUCKETS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.15)
CONTENT_TYPE = CONTENT_TYPE_LATEST


class Metrics:
    """All metrics exported on ``/metrics``."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        """Create the metrics in ``registry`` (a fresh one by default)."""
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.requests = Counter(
            "rec_requests_total",
            "Requests by outcome.",
            ["app", "surface", "arm", "tier", "reason"],
            registry=r,
        )
        self.latency = Histogram(
            "rec_latency_seconds",
            "Whole-handler latency.",
            ["surface"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.engine = Histogram(
            "rec_engine_seconds",
            "Engine time only.",
            ["surface"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.inflight = Gauge("rec_inflight", "Engine slots in use.", registry=r)
        self.degraded = Counter(
            "rec_degraded_total",
            "Responses of reduced quality (silent failures).",
            ["reason"],
            registry=r,
        )
        self.empty = Counter(
            "rec_empty_total", "Responses without items.", ["reason"], registry=r
        )
        self.invalid = Counter(
            "rec_request_invalid_total", "Invalid requests.", ["field"], registry=r
        )
        self.version_info = Gauge(
            "rec_registry_version_info",
            "Model version being served (value is always 1).",
            ["app", "model_version"],
            registry=r,
        )
        self.load_failures = Counter(
            "rec_registry_load_failures_total",
            "Failed artifact refreshes.",
            ["app"],
            registry=r,
        )
        self.registry_age = Gauge(
            "rec_registry_age_seconds",
            "Seconds since the last successful refresh.",
            ["app"],
            registry=r,
        )
        self.unknown_ratio = Gauge(
            "rec_unknown_item_ratio",
            "Share of recent ids unknown to the catalog, last request.",
            ["app"],
            registry=r,
        )
        self._lock = threading.Lock()
        self._trackers: dict[str, ExposureTracker] = {}
        self._catalog_size: Callable[[str], int] = lambda _app: 0
        r.register(HealthCollector(self._snapshot_trackers, self._size_of))
        self.served_dropped = Counter(
            "rec_served_dropped_total",
            "rec.served records dropped because the queue was full.",
            registry=r,
        )

    def _snapshot_trackers(self) -> dict[str, ExposureTracker]:
        with self._lock:
            return dict(self._trackers)

    def _size_of(self, app: str) -> int:
        return self._catalog_size(app)  # late-bound: attach_catalog_size replaces it

    def exposure(self, app: str) -> ExposureTracker:
        """Return the exposure tracker of ``app`` (created on first use)."""
        with self._lock:
            return self._trackers.setdefault(app, ExposureTracker())

    def attach_catalog_size(self, lookup: Callable[[str], int]) -> None:
        """Say how to find an app's catalog size (needed for coverage and Gini)."""
        self._catalog_size = lookup

    def render(self) -> bytes:
        """Return the Prometheus text exposition."""
        return generate_latest(self.registry)
