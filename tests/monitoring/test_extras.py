from __future__ import annotations

import pytest

from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.monitoring.drift import ColumnDrift, DriftResult
from recsys.monitoring.skew import skew_check
from recsys.training.ips import ips_estimate


def served(request_id: str, items: list[tuple[str, float]]) -> dict[str, object]:
    return {
        "request_id": request_id,
        "items": [{"id": i, "propensity": p} for i, p in items],
    }


def test_ips_on_a_deterministic_log_is_the_click_rate() -> None:
    rows = [served(f"r{n}", [("a", 1.0), ("b", 1.0)]) for n in range(10)]
    target = {f"r{n}": ["a", "b"] for n in range(10)}
    clicks = {f"r{n}": {"a"} for n in range(5)}  # half the requests click "a"
    result = ips_estimate(rows, target, clicks)
    assert result.ips == pytest.approx(0.25)  # 0.5 clicks per request / 2 slots
    assert result.snips == pytest.approx(0.25)
    assert (result.n_requests, result.n_matched) == (10, 20)


def test_ips_upweights_rarely_chosen_slots() -> None:
    rows = [served("r1", [("a", 0.1)]), served("r2", [("a", 0.9)])]
    clicks = {"r1": {"a"}, "r2": set()}
    result = ips_estimate(rows, {"r1": ["a"], "r2": ["a"]}, clicks)
    assert result.ips == pytest.approx(5.0)  # (1/0.1 + 0) / 2 requests
    assert result.snips == pytest.approx(10 / (10 + 1 / 0.9))


def test_ips_ignores_requests_the_target_did_not_serve() -> None:
    result = ips_estimate([served("r1", [("a", 1.0)])], {}, {})
    assert (result.ips, result.snips, result.n_requests) == (0.0, 0.0, 0)


def test_skew_flags_only_the_columns_that_differ() -> None:
    rows = [{"tier": "cold", "country": "US", "lang": "en"}] * 90 + [
        {"tier": "warm", "country": "JP", "lang": "ja"}
    ] * 10
    users = {
        "country_group": {"US": 0.9, "JP": 0.1},
        "lang": {"en": 0.9, "ja": 0.1},
        "tier": {"cold": 0.2, "warm": 0.8},
    }
    result = skew_check(rows, users)
    assert result.flagged == ("tier",)
    assert result.scores["country_group"] < 0.01


def test_skew_ignores_unknown_columns_and_empty_input() -> None:
    assert skew_check([], {"tier": {"cold": 1.0}}).scores == {}


def test_batch_metrics_render_and_push_failure_is_swallowed() -> None:
    metrics = BatchMetrics()
    metrics.record_training("demo", passed=True, ndcg=0.12)
    metrics.record_drift(
        "demo", DriftResult((ColumnDrift("inputs", "tier", 0.31, True),), n_current=10)
    )
    metrics.rollout_total.labels("demo", "rolled_back").inc()
    text = metrics.render().decode()
    assert 'rec_train_gate_passed{app="demo"} 1.0' in text
    assert 'rec_drift_psi{app="demo",feature="tier",layer="inputs"} 0.31' in text
    assert "rec_train_last_success_timestamp" in text
    assert metrics.push(None, "demo") is False
    assert metrics.push("http://127.0.0.1:1", "demo") is False  # nothing listens there


def test_a_failed_run_does_not_refresh_the_success_timestamp() -> None:
    metrics = BatchMetrics()
    metrics.record_training("demo", passed=False, ndcg=0.01)
    assert "rec_train_last_success_timestamp{" not in metrics.render().decode()
