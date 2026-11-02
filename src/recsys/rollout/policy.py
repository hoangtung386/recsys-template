"""Release verdicts for the shadow and canary stages (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Outcome(StrEnum):
    """Verdict of a stage."""

    PASS = "pass"  # noqa: S105 - a verdict, not a password
    WAIT = "wait"
    FAIL = "fail"
    ROLLBACK = "rollback"


@dataclass(frozen=True, slots=True)
class RolloutThresholds:
    """Limits of both stages. "provisional" values are tuned after two weeks of data."""

    shadow_min_hours: float = 24.0
    shadow_min_requests: int = 5_000  # provisional
    p95_max_ms: float = 30.0
    error_rate_max: float = 0.005  # provisional
    empty_rate_max: float = 0.20
    jaccard_min: float = 0.30  # provisional
    psi_max: float = 0.20
    canary_min_hours: float = 24.0
    canary_max_hours: float = 48.0
    canary_min_per_arm: int = 2_000  # provisional
    ctr_ratio_min: float = 0.90
    rollback_windows: int = 2


@dataclass(frozen=True, slots=True)
class ShadowStats:
    """What the shadow stage observed about the candidate."""

    hours: float
    n_requests: int
    p95_ms: float
    error_rate: float
    empty_rate: float
    jaccard_top10_vs_active: float
    tier_psi: float
    src_psi: float


@dataclass(frozen=True, slots=True)
class CanaryStats:
    """What the canary stage observed.

    Attributes:
        hours: Time since the canary started.
        n_canary: Exposed users on the canary version.
        n_active: Exposed users on the active version.
        ctr_ratio_6h: Canary CTR over active CTR per 6-hour window, oldest first.
        empty_rate: Share of empty responses on the canary.
        error_rate_delta: Canary error rate minus active error rate.
    """

    hours: float
    n_canary: int
    n_active: int
    ctr_ratio_6h: tuple[float, ...]
    empty_rate: float
    error_rate_delta: float


@dataclass(frozen=True, slots=True)
class Verdict:
    """A stage outcome with the reasons for it."""

    outcome: Outcome
    reasons: tuple[str, ...] = ()


def shadow_verdict(
    stats: ShadowStats,
    th: RolloutThresholds = RolloutThresholds(),  # noqa: B008
) -> Verdict:
    """Judge a candidate that served shadow traffic.

    Returns ``WAIT`` until there is enough time and traffic, then ``PASS`` or
    ``FAIL`` with the violated limits.
    """
    if stats.hours < th.shadow_min_hours or stats.n_requests < th.shadow_min_requests:
        return Verdict(
            Outcome.WAIT, (f"hours={stats.hours:.1f}", f"n={stats.n_requests}")
        )
    why: list[str] = []
    if stats.p95_ms > th.p95_max_ms:
        why.append(f"p95={stats.p95_ms:.1f}ms")
    if stats.error_rate > th.error_rate_max:
        why.append(f"error={stats.error_rate:.4f}")
    if stats.empty_rate > th.empty_rate_max:
        why.append(f"empty={stats.empty_rate:.3f}")
    if stats.jaccard_top10_vs_active < th.jaccard_min:
        why.append(f"jaccard={stats.jaccard_top10_vs_active:.2f}")
    if max(stats.tier_psi, stats.src_psi) > th.psi_max:
        why.append("psi")
    return Verdict(Outcome.FAIL, tuple(why)) if why else Verdict(Outcome.PASS)


def canary_verdict(  # noqa: PLR0911 - one early return per rollback rule
    stats: CanaryStats,
    th: RolloutThresholds = RolloutThresholds(),  # noqa: B008
) -> Verdict:
    """Judge a canary.

    A rollback takes effect at once, without waiting for the minimum time.

    Never returns ``PASS`` on too little data: after ``canary_max_hours`` an
    inconclusive canary is rolled back.
    """
    recent = stats.ctr_ratio_6h[-th.rollback_windows :]
    if len(recent) == th.rollback_windows and all(r < th.ctr_ratio_min for r in recent):
        return Verdict(Outcome.ROLLBACK, (f"ctr_ratio={list(recent)}",))
    if stats.empty_rate > th.empty_rate_max:
        return Verdict(Outcome.ROLLBACK, (f"empty={stats.empty_rate:.3f}",))
    if stats.error_rate_delta > th.error_rate_max:
        return Verdict(Outcome.ROLLBACK, (f"error_delta={stats.error_rate_delta:.4f}",))
    enough = (
        stats.hours >= th.canary_min_hours
        and min(stats.n_canary, stats.n_active) >= th.canary_min_per_arm
    )
    if not enough:
        if stats.hours >= th.canary_max_hours:
            return Verdict(Outcome.ROLLBACK, ("inconclusive_48h",))
        return Verdict(Outcome.WAIT)
    average = sum(stats.ctr_ratio_6h) / max(len(stats.ctr_ratio_6h), 1)
    if average >= th.ctr_ratio_min:
        return Verdict(Outcome.PASS)
    return Verdict(Outcome.ROLLBACK, (f"ctr_ratio_avg={average:.3f}",))
