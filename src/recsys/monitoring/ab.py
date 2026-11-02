"""A/B health: is the traffic split what the experiment configured?

Requires the ``train`` extra (scipy). Pure functions of ``rec.served`` rows and
an :class:`~recsys.engine.bucketing.Experiment`.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from scipy.stats import chisquare

from recsys.engine.bucketing import Experiment

SRM_ALARM = 1e-3


def srm_pvalue(
    assigned: Mapping[str, int], expected_share: Mapping[str, float]
) -> float:
    """Sample-ratio-mismatch test: p-value of the observed arm counts.

    A p-value below :data:`SRM_ALARM` means the traffic split is not what was
    configured, so the experiment is invalid and must not be read.

    Args:
        assigned: Observed users per arm.
        expected_share: Configured share per arm; must sum to 1.

    Raises:
        ValueError: If the shares do not sum to one or an arm has no count entry.
    """
    if abs(sum(expected_share.values()) - 1.0) > 1e-9:
        raise ValueError("expected shares must sum to 1")
    arms = list(expected_share)
    total = sum(assigned.get(arm, 0) for arm in arms)
    if total <= 0:
        raise ValueError("no assignments to test")
    observed = [assigned.get(arm, 0) for arm in arms]
    expected = [expected_share[arm] * total for arm in arms]
    return float(chisquare(observed, f_exp=expected).pvalue)


def expected_arm_shares(experiment: Experiment) -> dict[str, float]:
    """Share of devices each arm should get under ``experiment``.

    Mirrors the precedence of ``assign_arm``: holdout first, then the shadow
    slice, then the control/treatment split. Canary traffic is a slice of
    ``treatment`` (it differs by model version, not by arm), so it is not split
    out. Arms with a zero share are omitted.
    """
    holdout = experiment.holdout_pct / 100
    shadow = (1 - holdout) * (
        experiment.shadow_pct / 100 if experiment.shadow_version else 0
    )
    rest = 1 - holdout - shadow
    treatment = experiment.treatment_pct / 100
    shares = {
        "holdout": holdout,
        "shadow": shadow,
        "treatment": rest * treatment,
        "control": rest * (1 - treatment),
    }
    return {arm: share for arm, share in shares.items() if share > 0}


def devices_per_arm(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count distinct devices per arm in ``rec.served`` rows."""
    seen: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if row.get("arm") and row.get("device_id"):
            seen[row["arm"]].add(row["device_id"])
    return {arm: len(devices) for arm, devices in seen.items()}
