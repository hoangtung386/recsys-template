"""Retrain decision: a pure function, so it is testable without Airflow.

Drift alone never retrains. A retrain needs drift *and* a drop of the proxy
metric *and* every safety guard, because input drift is often harmless (a new
app version changes what it sends, quality is unchanged).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    """What the watchdog does."""

    NONE = "none"
    ALERT = "alert"
    RETRAIN = "retrain"


@dataclass(frozen=True, slots=True)
class DriftThresholds:
    """Policy limits. Values marked "provisional" are tuned after two weeks of data."""

    psi_warn: float = 0.10
    psi_action: float = 0.20
    proxy_drop_action: float = 0.05  # provisional
    min_new_rows: int = 50_000  # provisional
    cooldown_hours: float = 12.0  # provisional
    max_retrains_per_day: int = 1


@dataclass(frozen=True, slots=True)
class DriftSignals:
    """Everything the decision depends on.

    Attributes:
        max_input_psi: Largest PSI among input distributions.
        output_psi: Largest PSI among output distributions, ``None`` before
            ``rec.served`` data exists.
        proxy_metric_drop: Relative drop of the proxy metric vs. the previous
            version, ``None`` when there is not enough data.
        new_rows_since_train: Interactions logged since the last training run.
        hours_since_last_train: Hours since the last training run.
        retrains_today: Off-schedule retrains already triggered today.
        dq_blocking_ok: Every blocking data-quality check passed.
        rollout_in_progress: A release is in shadow, canary or awaiting approval.
        frozen: The app is frozen after a failed release.
    """

    max_input_psi: float
    output_psi: float | None
    proxy_metric_drop: float | None
    new_rows_since_train: int
    hours_since_last_train: float
    retrains_today: int
    dq_blocking_ok: bool
    rollout_in_progress: bool
    frozen: bool


@dataclass(frozen=True, slots=True)
class Decision:
    """The chosen action and the reasons behind it."""

    action: Action
    reasons: tuple[str, ...] = ()


def _blocking_guards(s: DriftSignals, th: DriftThresholds) -> list[str]:
    checks = {
        "dq_failed": not s.dq_blocking_ok,
        "rollout_in_progress": s.rollout_in_progress,
        "frozen": s.frozen,
        "not_enough_rows": s.new_rows_since_train < th.min_new_rows,
        "cooldown": s.hours_since_last_train < th.cooldown_hours,
        "daily_cap": s.retrains_today >= th.max_retrains_per_day,
    }
    return [f"guard:{name}" for name, tripped in checks.items() if tripped]


DEFAULT_THRESHOLDS = DriftThresholds()


def decide(
    signals: DriftSignals, thresholds: DriftThresholds = DEFAULT_THRESHOLDS
) -> Decision:
    """Decide between doing nothing, alerting a human, and retraining."""
    th, s = thresholds, signals
    reasons: list[str] = []
    drifted = s.max_input_psi > th.psi_action or (s.output_psi or 0.0) > th.psi_action
    degraded = (
        s.proxy_metric_drop is not None and s.proxy_metric_drop > th.proxy_drop_action
    )
    if drifted or s.max_input_psi > th.psi_warn:
        reasons.append(f"psi_in={s.max_input_psi:.3f}")
    if s.output_psi is not None and s.output_psi > th.psi_warn:
        reasons.append(f"psi_out={s.output_psi:.3f}")
    if degraded:
        reasons.append(f"proxy_drop={s.proxy_metric_drop:.3f}")
    if not reasons:
        return Decision(Action.NONE)
    if drifted and degraded:
        guards = _blocking_guards(s, th)
        if not guards:
            return Decision(Action.RETRAIN, tuple(reasons))
        reasons.extend(guards)
    return Decision(Action.ALERT, tuple(reasons))
