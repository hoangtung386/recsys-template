from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from recsys.engine import Arm, Bundle, Reason
from recsys.loop.collect import (
    collect_canary_stats,
    collect_shadow_stats,
    request_ctr,
)
from recsys.monitoring.served_log import clicks_by_request
from recsys.rollout.policy import (
    Outcome,
    RolloutThresholds,
    canary_verdict,
    shadow_verdict,
)
from tests.loop.rows import click, served_row

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def rows(bundle: Bundle, n: int, **kw: object) -> list[dict[str, object]]:
    return [
        served_row(bundle, n=i, when=T0 + timedelta(minutes=i), **kw)  # type: ignore[arg-type]
        for i in range(n)
    ]


def test_shadow_stats_reflect_the_logs(random_bundle: Bundle) -> None:
    shadow = rows(random_bundle, 60, arm=Arm.SHADOW, latency_ms=12.0)
    shadow += rows(random_bundle, 5, arm=Arm.SHADOW, empty=True)
    shadow.append(
        served_row(random_bundle, n=900, when=T0, arm=Arm.SHADOW, reason=Reason.ERROR)
    )
    stats = collect_shadow_stats(
        shadow,
        version=random_bundle.version,
        active_version="other",
        active_bundle=random_bundle,
    )
    assert stats.n_requests == 66
    assert stats.hours == pytest.approx(59 / 60, abs=0.02)
    assert stats.p95_ms == pytest.approx(12.0, abs=0.5)
    assert stats.error_rate == pytest.approx(1 / 66)
    assert stats.empty_rate == pytest.approx(5 / 66)
    # the same model replayed without exploration overlaps almost completely
    assert stats.jaccard_top10_vs_active > 0.8


def test_shadow_with_nothing_logged_is_all_zero(random_bundle: Bundle) -> None:
    stats = collect_shadow_stats(
        [], version="v", active_version="a", active_bundle=random_bundle
    )
    assert (stats.n_requests, stats.hours, stats.p95_ms) == (0, 0.0, 0.0)
    assert shadow_verdict(stats).outcome is Outcome.WAIT


def test_ctr_counts_only_non_empty_responses(random_bundle: Bundle) -> None:
    data = rows(random_bundle, 10) + rows(random_bundle, 5, empty=True)
    clicked = clicks_by_request([click(r, T0) for r in data[:4]])
    ctr, shown = request_ctr(data, clicked)
    assert (ctr, shown) == (0.4, 10)


def _canary_inputs(
    bundle: Bundle, active_ctr: float, canary_ctr: float
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    served: list[dict[str, object]] = []
    clicks: list[dict[str, object]] = []
    for version, rate in (("active", active_ctr), ("canary", canary_ctr)):
        for i in range(100):
            row = served_row(bundle, n=i, when=T0 + timedelta(minutes=i))
            row["model_version"] = version
            row["request_id"] = f"{version}-{i}"
            row["device_id"] = f"{version}-dev{i}"
            served.append(row)
            if i < rate * 100:
                clicks.append(click(row, T0 + timedelta(minutes=i)))
    return served, clicks


def test_healthy_canary_passes(random_bundle: Bundle) -> None:
    served, clicks = _canary_inputs(random_bundle, 0.3, 0.3)
    stats = collect_canary_stats(
        served,
        clicks,
        canary_version="canary",
        active_version="active",
        since=T0,
        now=T0 + timedelta(hours=5),
    )
    assert stats.ctr_ratio_6h == (1.0,)
    assert (stats.n_canary, stats.n_active) == (100, 100)
    assert canary_verdict(stats, _lenient()).outcome is Outcome.PASS


def test_a_canary_with_half_the_ctr_is_rolled_back(random_bundle: Bundle) -> None:
    served, clicks = _canary_inputs(random_bundle, 0.4, 0.2)
    stats = collect_canary_stats(
        served,
        clicks,
        canary_version="canary",
        active_version="active",
        since=T0,
        now=T0 + timedelta(hours=5),
    )
    assert stats.ctr_ratio_6h == (0.5,)
    assert canary_verdict(stats, _lenient()).outcome is Outcome.ROLLBACK


def test_windows_without_enough_traffic_are_skipped(random_bundle: Bundle) -> None:
    served, clicks = _canary_inputs(random_bundle, 0.3, 0.3)
    stats = collect_canary_stats(
        served[:10] + served[100:110],
        clicks,
        canary_version="canary",
        active_version="active",
        since=T0,
        now=T0 + timedelta(hours=5),
    )
    assert stats.ctr_ratio_6h == ()


def _lenient() -> RolloutThresholds:
    return RolloutThresholds(canary_min_hours=0, canary_min_per_arm=10)
