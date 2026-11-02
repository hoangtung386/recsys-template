"""Turning logs and the registry into the inputs of the decision functions.

Everything here is a pure function of ``rec.served`` rows, interaction rows and
registry records, so the whole loop can run (and be tested) without a warehouse.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from recsys.engine.bundle import Bundle
from recsys.engine.context import context_from_served
from recsys.engine.recommend import recommend
from recsys.monitoring.drift import (
    DriftResult,
    compute_drift,
    distributions_from_served,
)
from recsys.monitoring.policy import DriftSignals
from recsys.monitoring.served_log import clicks_by_request, parse_ts
from recsys.monitoring.stats import psi, shares
from recsys.registry import SqliteRegistry
from recsys.rollout.policy import CanaryStats, ShadowStats

Row = Mapping[str, Any]
MIN_PROXY_REQUESTS = 500
MIN_WINDOW_REQUESTS = 30
WINDOW_HOURS = 6
REPLAY_SAMPLE = 2000
TOP_K = 10


def _hours(rows: Sequence[Row]) -> float:
    if not rows:
        return 0.0
    stamps = [parse_ts(r["ts"]) for r in rows]
    return (max(stamps) - min(stamps)).total_seconds() / 3600


def _rate(rows: Sequence[Row], predicate: Any) -> float:
    return sum(1 for r in rows if predicate(r)) / len(rows) if rows else 0.0


def _served(rows: Sequence[Row], version: str) -> list[Row]:
    """Treatment-arm rows of ``version`` that were actually returned."""
    return [
        r
        for r in rows
        if r.get("arm") == "treatment" and r.get("model_version") == version
    ]


def request_ctr(
    rows: Sequence[Row], clicked: Mapping[str, set[str]]
) -> tuple[float, int]:
    """Share of non-empty responses with a click, and how many there were."""
    shown = [r for r in rows if r.get("items")]
    hits = sum(1 for r in shown if r["request_id"] in clicked)
    return (hits / len(shown) if shown else 0.0), len(shown)


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    union = set(a) | set(b)
    return len(set(a) & set(b)) / len(union) if union else 1.0


def collect_shadow_stats(
    rows: Sequence[Row],
    *,
    version: str,
    active_version: str,
    active_bundle: Bundle,
    sample: int = REPLAY_SAMPLE,
) -> ShadowStats:
    """Summarise the shadow stage of ``version``.

    Latency, error and empty rates come straight from ``rec.served``. Overlap
    with the active model is measured by replaying a sample of the shadow
    requests on the active bundle without exploration.
    """
    shadow = [
        r
        for r in rows
        if r.get("arm") == "shadow" and r.get("model_version") == version
    ]
    baseline = _served(rows, active_version)
    latency = [r["latency_ms"] for r in shadow if r.get("latency_ms") is not None]
    replayable = [r for r in shadow if r.get("items") and "recent" in r][:sample]
    overlaps = []
    for r in replayable:
        replay = recommend(
            active_bundle, context_from_served(active_bundle, r), eps=0.0
        )
        mine = [i["id"] for i in r["items"] if i["src"] != "rec_explore"][:TOP_K]
        overlaps.append(_jaccard(mine, [i.id for i in replay.items][:TOP_K]))

    def label_psi(key: str) -> float:
        def dist(group: Sequence[Row]) -> dict[str, float]:
            if key == "tier":
                return shares(str(r["tier"]) for r in group if r.get("tier"))
            return shares(i["src"] for r in group for i in r.get("items", ()))

        base, cand = dist(baseline), dist(shadow)
        return psi(base, cand) if base and cand else 0.0

    return ShadowStats(
        hours=_hours(shadow),
        n_requests=len(shadow),
        p95_ms=float(np.percentile(latency, 95)) if latency else 0.0,
        error_rate=_rate(shadow, lambda r: r.get("reason") == "error"),
        empty_rate=_rate(shadow, lambda r: not r.get("items")),
        jaccard_top10_vs_active=float(np.mean(overlaps)) if overlaps else 1.0,
        tier_psi=label_psi("tier"),
        src_psi=label_psi("src"),
    )


def collect_canary_stats(
    rows: Sequence[Row],
    interactions: Sequence[Row],
    *,
    canary_version: str,
    active_version: str,
    since: datetime,
    now: datetime,
) -> CanaryStats:
    """Summarise the canary stage of ``canary_version`` since ``since``.

    ``ctr_ratio_6h`` has one entry per 6-hour window in which both versions
    had enough traffic and the active CTR was non-zero.
    """
    clicked = clicks_by_request(interactions)
    window = [r for r in rows if parse_ts(r["ts"]) >= since]
    canary = [
        r
        for r in window
        if r.get("arm") == "treatment" and r.get("model_version") == canary_version
    ]
    active = [
        r
        for r in window
        if r.get("arm") == "treatment" and r.get("model_version") == active_version
    ]
    ratios: list[float] = []
    n_windows = max(math.ceil((now - since).total_seconds() / 3600 / WINDOW_HOURS), 0)
    for w in range(n_windows):
        lo = since + timedelta(hours=WINDOW_HOURS * w)
        hi = lo + timedelta(hours=WINDOW_HOURS)
        c = [r for r in canary if lo <= parse_ts(r["ts"]) < hi]
        a = [r for r in active if lo <= parse_ts(r["ts"]) < hi]
        ctr_c, n_c = request_ctr(c, clicked)
        ctr_a, n_a = request_ctr(a, clicked)
        if min(n_c, n_a) >= MIN_WINDOW_REQUESTS and ctr_a > 0:
            ratios.append(ctr_c / ctr_a)
    is_error = lambda r: r.get("reason") == "error"  # noqa: E731
    return CanaryStats(
        hours=(now - since).total_seconds() / 3600,
        n_canary=len({r["device_id"] for r in canary}),
        n_active=len({r["device_id"] for r in active}),
        ctr_ratio_6h=tuple(ratios),
        empty_rate=_rate(canary, lambda r: not r.get("items")),
        error_rate_delta=_rate(canary, is_error) - _rate(active, is_error),
    )


def collect_drift_signals(
    registry: SqliteRegistry,
    app: str,
    reference: Mapping[str, Any],
    served: Sequence[Row],
    interactions: Sequence[Row],
    *,
    now: datetime,
    dq_blocking_ok: bool = True,
) -> tuple[DriftSignals, DriftResult]:
    """Build the inputs of :func:`recsys.monitoring.policy.decide`.

    Args:
        registry: Source of versions, rollouts and the freeze flag.
        app: App slug.
        reference: ``reference.json`` of the active version.
        served: Recent ``rec.served`` rows.
        interactions: Recent interaction rows.
        now: Evaluation time.
        dq_blocking_ok: Result of the (external) blocking data-quality checks.
    """
    drift = compute_drift(reference, distributions_from_served(served))
    versions = registry.list_versions(app)
    active = registry.get_active(app)
    since_train = parse_ts(active.created_at) if active else now
    latest_train = max((parse_ts(v.created_at) for v in versions), default=now)
    today = now.date()
    retrains_today = sum(
        1
        for v in versions
        if "-r" in v.model_version and parse_ts(v.created_at).date() == today
    )

    drop: float | None = None
    previous = registry.previous_version(app)
    if active and previous:
        clicked = clicks_by_request(interactions)
        ctr_now, n_now = request_ctr(_served(served, active.model_version), clicked)
        ctr_before, n_before = request_ctr(_served(served, previous), clicked)
        if min(n_now, n_before) >= MIN_PROXY_REQUESTS and ctr_before > 0:
            drop = (ctr_before - ctr_now) / ctr_before

    signals = DriftSignals(
        max_input_psi=drift.max_score("inputs") or 0.0,
        output_psi=drift.max_score("outputs"),
        proxy_metric_drop=drop,
        new_rows_since_train=sum(
            1 for i in interactions if parse_ts(i["ts"]) > since_train
        ),
        hours_since_last_train=(now - latest_train).total_seconds() / 3600,
        retrains_today=retrains_today,
        dq_blocking_ok=dq_blocking_ok,
        rollout_in_progress=registry.active_rollout(app) is not None,
        frozen=registry.loop_state(app).frozen,
    )
    return signals, drift
