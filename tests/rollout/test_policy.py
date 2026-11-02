from __future__ import annotations

from dataclasses import replace

import pytest

from recsys.registry import ModelStatus, ModelVersion, RolloutStage, SqliteRegistry
from recsys.rollout.controller import apply_canary, apply_shadow
from recsys.rollout.policy import (
    CanaryStats,
    Outcome,
    ShadowStats,
    canary_verdict,
    shadow_verdict,
)

GOOD_SHADOW = ShadowStats(30, 9000, 12.0, 0.001, 0.05, 0.5, 0.05, 0.05)
GOOD_CANARY = CanaryStats(30, 3000, 3000, (1.0, 0.98, 1.02, 0.99), 0.05, 0.0)


def test_shadow_waits_for_time_and_traffic() -> None:
    assert shadow_verdict(replace(GOOD_SHADOW, hours=10)).outcome is Outcome.WAIT
    assert shadow_verdict(replace(GOOD_SHADOW, n_requests=10)).outcome is Outcome.WAIT
    assert shadow_verdict(GOOD_SHADOW).outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("p95_ms", 45.0, "p95"),
        ("error_rate", 0.02, "error"),
        ("empty_rate", 0.4, "empty"),
        ("jaccard_top10_vs_active", 0.05, "jaccard"),
        ("src_psi", 0.5, "psi"),
    ],
)
def test_shadow_fails_with_the_violated_limit(
    field: str, value: float, reason: str
) -> None:
    verdict = shadow_verdict(replace(GOOD_SHADOW, **{field: value}))  # type: ignore[arg-type]
    assert verdict.outcome is Outcome.FAIL
    assert any(reason in r for r in verdict.reasons)


def test_canary_passes_when_ctr_holds() -> None:
    assert canary_verdict(GOOD_CANARY).outcome is Outcome.PASS


def test_two_bad_windows_in_a_row_roll_back_immediately() -> None:
    early = replace(GOOD_CANARY, hours=3, ctr_ratio_6h=(1.0, 0.85, 0.80))
    assert canary_verdict(early).outcome is Outcome.ROLLBACK


def test_one_bad_window_followed_by_a_good_one_does_not_roll_back() -> None:
    stats = replace(GOOD_CANARY, ctr_ratio_6h=(1.0, 0.85, 1.0, 1.0))
    assert canary_verdict(stats).outcome is Outcome.PASS


def test_not_enough_samples_waits_then_rolls_back_after_48h() -> None:
    thin = replace(GOOD_CANARY, hours=30, n_canary=100)
    assert canary_verdict(thin).outcome is Outcome.WAIT
    stale = replace(thin, hours=48)
    verdict = canary_verdict(stale)
    assert verdict.outcome is Outcome.ROLLBACK
    assert "inconclusive_48h" in verdict.reasons


def test_empty_responses_or_errors_roll_back() -> None:
    assert (
        canary_verdict(replace(GOOD_CANARY, empty_rate=0.5)).outcome is Outcome.ROLLBACK
    )
    assert (
        canary_verdict(replace(GOOD_CANARY, error_rate_delta=0.05)).outcome
        is Outcome.ROLLBACK
    )


def test_low_average_ctr_rolls_back_even_without_two_consecutive_bad_windows() -> None:
    stats = replace(GOOD_CANARY, ctr_ratio_6h=(0.5, 1.0, 0.5, 1.0, 0.95, 0.5))
    assert canary_verdict(stats).outcome is Outcome.ROLLBACK


# --------------------------------------------------- end to end with the registry
@pytest.fixture
def release(registry: SqliteRegistry) -> tuple[SqliteRegistry, str]:
    for version in ("v1", "v2"):
        registry.record(ModelVersion("demo", version, ModelStatus.VALIDATED, "h", "/x"))
    registry.set_active("demo", "v1", actor="a")
    return registry, registry.start_rollout(
        "demo", "v2", actor="a", autonomy="L2"
    ).rollout_id


def test_good_model_goes_to_awaiting_approval_at_l2(
    release: tuple[SqliteRegistry, str],
) -> None:
    registry, rid = release
    assert apply_shadow(registry, rid, GOOD_SHADOW, actor="dag").outcome is Outcome.PASS
    assert registry.get_rollout(rid).stage is RolloutStage.CANARY  # type: ignore[union-attr]
    assert apply_canary(registry, rid, GOOD_CANARY, actor="dag").outcome is Outcome.PASS
    assert registry.get_rollout(rid).stage is RolloutStage.AWAITING_APPROVAL  # type: ignore[union-attr]
    assert registry.active_version("demo") == "v1"  # a human still has to promote


def test_good_model_is_promoted_automatically_at_l3(registry: SqliteRegistry) -> None:
    for version in ("v1", "v2"):
        registry.record(ModelVersion("demo", version, ModelStatus.VALIDATED, "h", "/x"))
    registry.set_active("demo", "v1", actor="a")
    rid = registry.start_rollout("demo", "v2", actor="a", autonomy="L3").rollout_id
    apply_shadow(registry, rid, GOOD_SHADOW, actor="dag")
    apply_canary(registry, rid, GOOD_CANARY, actor="dag")
    assert registry.active_version("demo") == "v2"


def test_bad_model_is_rolled_back_and_the_loop_freezes(
    release: tuple[SqliteRegistry, str],
) -> None:
    registry, rid = release
    apply_shadow(registry, rid, GOOD_SHADOW, actor="dag")
    bad = replace(GOOD_CANARY, ctr_ratio_6h=(0.7, 0.6))
    assert apply_canary(registry, rid, bad, actor="dag").outcome is Outcome.ROLLBACK
    assert registry.active_version("demo") == "v1"
    assert registry.loop_state("demo").frozen
    assert registry.get_rollout(rid).stage is RolloutStage.ROLLED_BACK  # type: ignore[union-attr]


def test_failed_shadow_rejects_the_version(release: tuple[SqliteRegistry, str]) -> None:
    registry, rid = release
    apply_shadow(registry, rid, replace(GOOD_SHADOW, p95_ms=99.0), actor="dag")
    assert registry.get("demo", "v2").status is ModelStatus.REJECTED  # type: ignore[union-attr]
    assert registry.loop_state("demo").frozen


def test_waiting_changes_nothing(release: tuple[SqliteRegistry, str]) -> None:
    registry, rid = release
    apply_shadow(registry, rid, replace(GOOD_SHADOW, hours=1), actor="dag")
    assert registry.get_rollout(rid).stage is RolloutStage.SHADOW  # type: ignore[union-attr]
