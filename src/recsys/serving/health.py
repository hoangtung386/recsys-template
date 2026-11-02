"""Content-health metrics: is what we serve getting narrower or emptier?

A recommender can keep answering quickly while quietly collapsing onto a few
items. These gauges are computed over a sliding window of recent slates and
evaluated lazily, when Prometheus scrapes.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from recsys.engine.result import RecItem
from recsys.engine.schema import Tier
from recsys.monitoring.stats import gini, top_share


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Exposure statistics of the window."""

    n_slates: int
    gini: float
    top1pct_share: float
    coverage: float
    src_share: dict[str, float]
    tier_share: dict[str, float]


class ExposureTracker:
    """Counts what the last ``window`` slates showed. Thread-safe."""

    def __init__(self, window: int = 2000) -> None:
        """Keep at most ``window`` slates."""
        self._slates: deque[tuple[tuple[str, ...], tuple[str, ...], str]] = deque(
            maxlen=window
        )
        self._lock = threading.Lock()

    def add(self, items: Sequence[RecItem], tier: Tier | None) -> None:
        """Record one returned slate (empty slates are not recorded)."""
        if not items:
            return
        entry = (
            tuple(i.id for i in items),
            tuple(i.src.value for i in items),
            tier.value if tier else "",
        )
        with self._lock:
            self._slates.append(entry)

    def snapshot(self, n_items: int) -> HealthSnapshot:
        """Compute statistics against a catalog of ``n_items``."""
        with self._lock:
            slates = list(self._slates)
        exposure: Counter[str] = Counter(i for ids, _, _ in slates for i in ids)
        sources: Counter[str] = Counter(s for _, srcs, _ in slates for s in srcs)
        tiers: Counter[str] = Counter(t for _, _, t in slates if t)
        counts = np.concatenate(
            [list(exposure.values()), np.zeros(max(n_items - len(exposure), 0))]
        )
        return HealthSnapshot(
            n_slates=len(slates),
            gini=gini(counts),
            top1pct_share=top_share(counts, 0.01),
            coverage=len(exposure) / n_items if n_items else 0.0,
            src_share=_normalise(sources),
            tier_share=_normalise(tiers),
        )


def _normalise(counter: Counter[str]) -> dict[str, float]:
    total = sum(counter.values())
    return {k: v / total for k, v in sorted(counter.items())} if total else {}


class HealthCollector(Collector):
    """Exposes :class:`ExposureTracker` statistics at scrape time."""

    def __init__(
        self,
        trackers: Callable[[], dict[str, ExposureTracker]],
        catalog_size: Callable[[str], int],
    ) -> None:
        """Wire the collector to the trackers and to the catalog size lookup."""
        self._trackers = trackers
        self._catalog_size = catalog_size

    def collect(self) -> Iterable[GaugeMetricFamily]:
        """Yield one gauge family per statistic."""
        gini_g = GaugeMetricFamily(
            "rec_served_item_gini", "Exposure Gini over items.", labels=["app"]
        )
        top_g = GaugeMetricFamily(
            "rec_top1pct_exposure_share",
            "Exposure held by the top 1% of items.",
            labels=["app"],
        )
        cov_g = GaugeMetricFamily(
            "rec_catalog_coverage",
            "Share of the catalog shown recently.",
            labels=["app"],
        )
        src_g = GaugeMetricFamily(
            "rec_src_share", "Share of slots per signal.", labels=["app", "src"]
        )
        tier_g = GaugeMetricFamily(
            "rec_tier_share", "Share of slates per tier.", labels=["app", "tier"]
        )
        for app, tracker in self._trackers().items():
            n_items = self._catalog_size(app)
            if n_items <= 0:
                continue
            snap = tracker.snapshot(n_items)
            if not snap.n_slates:
                continue
            gini_g.add_metric([app], snap.gini)
            top_g.add_metric([app], snap.top1pct_share)
            cov_g.add_metric([app], snap.coverage)
            for src, value in snap.src_share.items():
                src_g.add_metric([app, src], value)
            for tier, value in snap.tier_share.items():
                tier_g.add_metric([app, tier], value)
        yield from (gini_g, top_g, cov_g, src_g, tier_g)
