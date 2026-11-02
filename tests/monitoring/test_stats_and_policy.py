from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest

from recsys.monitoring.drift import compute_drift, distributions_from_served
from recsys.monitoring.policy import Action, DriftSignals, DriftThresholds, decide
from recsys.monitoring.stats import (
    gini,
    histogram_shares,
    psi,
    psi_binned,
    shares,
    top_share,
)


def test_psi_identical_is_zero_and_shifted_mass_is_flagged() -> None:
    base = {"US": 0.5, "JP": 0.3, "other": 0.2}
    assert psi(base, base) == pytest.approx(0.0, abs=1e-9)
    shifted = {"US": 0.3, "JP": 0.3, "other": 0.4}  # 20% of the mass moved
    assert psi(base, shifted) > 0.2


def test_psi_handles_empty_buckets_and_new_categories() -> None:
    assert math.isfinite(psi({"a": 1.0}, {"b": 1.0}))
    assert psi_binned([0.5, 0.5], [0.5, 0.5]) == pytest.approx(0.0, abs=1e-9)


def test_gini_and_top_share() -> None:
    assert gini([1, 1, 1, 1]) == pytest.approx(0.0)
    assert gini([0, 0, 0, 10]) == pytest.approx(0.75)
    assert gini([]) == 0.0
    assert top_share([90] + [1] * 99, 0.01) == pytest.approx(90 / 189)


def test_shares_and_histograms() -> None:
    assert shares(["a", "a", "b"]) == {
        "a": pytest.approx(2 / 3),
        "b": pytest.approx(1 / 3),
    }
    assert shares([]) == {}
    assert histogram_shares([0.1, 0.3, 5.0, -2.0], [0, 0.2, 0.4, 1.0]) == [
        0.5,
        0.25,
        0.25,
    ]
    assert histogram_shares([], [0, 1, 2]) == [0.0, 0.0]


# ------------------------------------------------------------------ drift
REFERENCE = {
    "inputs": {
        "country_group": {"US": 0.5, "JP": 0.3, "other": 0.2},
        "lang": {"en": 0.6, "ja": 0.4},
        "tier": {"cold": 0.5, "lukewarm": 0.3, "warm": 0.2},
        "n_recent": {
            "bins": [0, 1, 2, 5, 10, 20, 31],
            "p": [0.5, 0.2, 0.15, 0.1, 0.04, 0.01],
        },
    },
    "outputs": {"src_share": {"rec_group": 0.5, "rec_trend": 0.5}},
}


def served(
    country: str, tier: str, n_recent: int, src: str = "rec_group"
) -> dict[str, object]:
    return {
        "country": country,
        "lang": "en",
        "tier": tier,
        "n_recent": n_recent,
        "items": [{"id": "x", "src": src, "score": 0.5}],
    }


def test_no_drift_when_serving_matches_reference() -> None:
    rows = (
        [served("US", "cold", 0)] * 25
        + [served("JP", "cold", 0)] * 15
        + [served("DE", "lukewarm", 1)] * 10
        + [served("US", "lukewarm", 3)] * 20
        + [served("JP", "warm", 8)] * 10
        + [served("DE", "warm", 6)] * 20
    )
    result = compute_drift(REFERENCE, distributions_from_served(rows))
    assert result.n_current == 100
    assert result.max_score("inputs") is not None
    assert "country_group" in {c.column for c in result.columns}


def test_drift_is_detected_in_the_shifted_column_only() -> None:
    rows = [served("DE", "cold", 0)] * 90 + [served("US", "cold", 0)] * 10
    result = compute_drift(REFERENCE, distributions_from_served(rows))
    drifted = {c.column for c in result.drifted}
    assert "country_group" in drifted
    assert result.max_score("outputs") is not None


def test_requests_that_never_reached_the_engine_are_ignored() -> None:
    rows: list[dict[str, Any]] = [
        {"tier": None, "n_recent": 0, "items": []},
        served("US", "cold", 0),
    ]
    assert distributions_from_served(rows)["n"] == 1
    assert compute_drift({}, distributions_from_served([])).columns == ()


# ------------------------------------------------------------------ policy
OK = DriftSignals(
    max_input_psi=0.35,
    output_psi=None,
    proxy_metric_drop=0.08,
    new_rows_since_train=200_000,
    hours_since_last_train=30,
    retrains_today=0,
    dq_blocking_ok=True,
    rollout_in_progress=False,
    frozen=False,
)


def with_(**kw: object) -> DriftSignals:
    return replace(OK, **kw)  # type: ignore[arg-type]


def test_nothing_happens_when_all_is_calm() -> None:
    d = decide(with_(max_input_psi=0.02, proxy_metric_drop=0.0))
    assert d.action is Action.NONE and d.reasons == ()


def test_drift_without_a_metric_drop_only_alerts() -> None:
    assert decide(with_(proxy_metric_drop=0.0)).action is Action.ALERT
    assert decide(with_(proxy_metric_drop=None)).action is Action.ALERT


def test_metric_drop_without_drift_only_alerts() -> None:
    assert decide(with_(max_input_psi=0.02)).action is Action.ALERT


def test_drift_plus_drop_plus_guards_retrains() -> None:
    d = decide(OK)
    assert d.action is Action.RETRAIN
    assert any(r.startswith("psi_in") for r in d.reasons)


def test_output_drift_counts_like_input_drift() -> None:
    quiet_inputs = {"max_input_psi": 0.02, "output_psi": 0.4}
    assert decide(with_(**quiet_inputs)).action is Action.RETRAIN
    assert decide(with_(**quiet_inputs, proxy_metric_drop=0.0)).action is Action.ALERT


@pytest.mark.parametrize(
    ("override", "guard"),
    [
        ({"hours_since_last_train": 2}, "cooldown"),
        ({"new_rows_since_train": 10}, "not_enough_rows"),
        ({"dq_blocking_ok": False}, "dq_failed"),
        ({"rollout_in_progress": True}, "rollout_in_progress"),
        ({"frozen": True}, "frozen"),
        ({"retrains_today": 1}, "daily_cap"),
    ],
)
def test_every_guard_blocks_the_retrain_and_says_why(
    override: dict[str, object], guard: str
) -> None:
    d = decide(with_(**override))
    assert d.action is Action.ALERT
    assert f"guard:{guard}" in d.reasons


def test_thresholds_are_injectable() -> None:
    lenient = DriftThresholds(cooldown_hours=0, min_new_rows=0, max_retrains_per_day=5)
    assert (
        decide(
            with_(hours_since_last_train=0, new_rows_since_train=0, retrains_today=3),
            lenient,
        ).action
        is Action.RETRAIN
    )
