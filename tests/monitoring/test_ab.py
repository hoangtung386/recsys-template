from __future__ import annotations

import pytest

from recsys.engine import Experiment
from recsys.monitoring.ab import (
    SRM_ALARM,
    devices_per_arm,
    expected_arm_shares,
    srm_pvalue,
)


def test_srm_balanced_split_passes_and_skewed_split_alarms() -> None:
    assert srm_pvalue({"a": 5000, "b": 5000}, {"a": 0.5, "b": 0.5}) > 0.5
    assert srm_pvalue({"a": 52_000, "b": 48_000}, {"a": 0.5, "b": 0.5}) < SRM_ALARM


def test_srm_validates_input() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        srm_pvalue({"a": 1}, {"a": 0.7})
    with pytest.raises(ValueError, match="no assignments"):
        srm_pvalue({}, {"a": 1.0})


def test_expected_shares_follow_assignment_precedence() -> None:
    shares = expected_arm_shares(
        Experiment(treatment_pct=50, holdout_pct=10, shadow_version="v", shadow_pct=20)
    )
    assert sum(shares.values()) == pytest.approx(1.0)
    assert shares["holdout"] == pytest.approx(0.10)
    assert shares["shadow"] == pytest.approx(0.18)
    assert shares["treatment"] == pytest.approx(0.36)
    assert shares["control"] == pytest.approx(0.36)


def test_expected_shares_omit_empty_arms() -> None:
    assert set(expected_arm_shares(Experiment(holdout_pct=0))) == {
        "treatment",
        "control",
    }


def test_devices_per_arm_counts_distinct_devices() -> None:
    rows: list[dict[str, str | None]] = [
        {"arm": "control", "device_id": "a"},
        {"arm": "control", "device_id": "a"},
        {"arm": "treatment", "device_id": "b"},
        {"arm": None, "device_id": "c"},
    ]
    assert devices_per_arm(rows) == {"control": 1, "treatment": 1}
