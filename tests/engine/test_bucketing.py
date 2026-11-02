from __future__ import annotations

from collections import Counter

import pytest

from recsys.engine import Arm, Experiment, assign_arm


def share(counter: Counter[Arm], arm: Arm, n: int) -> float:
    return counter[arm] / n


def test_assignment_is_deterministic() -> None:
    exp = Experiment(salt="s1")
    assert assign_arm("demo", "dev-1", exp) == assign_arm("demo", "dev-1", exp)


def test_split_matches_configuration() -> None:
    exp = Experiment(treatment_pct=50, holdout_pct=5)
    n = 20_000
    counts = Counter(assign_arm("demo", f"d{i}", exp).arm for i in range(n))
    assert share(counts, Arm.HOLDOUT, n) == pytest.approx(0.05, abs=0.01)
    assert share(counts, Arm.TREATMENT, n) == pytest.approx(0.475, abs=0.02)
    assert share(counts, Arm.CONTROL, n) == pytest.approx(0.475, abs=0.02)
    assert counts[Arm.SHADOW] == 0


def test_holdout_is_permanent_across_experiments() -> None:
    first, second = (
        Experiment(experiment_id="a"),
        Experiment(experiment_id="b", salt="x"),
    )
    held = {
        f"d{i}"
        for i in range(5000)
        if assign_arm("demo", f"d{i}", first).arm is Arm.HOLDOUT
    }
    held_later = {
        f"d{i}"
        for i in range(5000)
        if assign_arm("demo", f"d{i}", second).arm is Arm.HOLDOUT
    }
    assert held == held_later
    assert held


def test_shadow_and_canary_slices() -> None:
    exp = Experiment(
        holdout_pct=0,
        treatment_pct=100,
        shadow_version="v2",
        shadow_pct=20,
    )
    n = 10_000
    counts = Counter(assign_arm("demo", f"d{i}", exp).arm for i in range(n))
    assert share(counts, Arm.SHADOW, n) == pytest.approx(0.2, abs=0.02)

    canary = Experiment(
        holdout_pct=0, treatment_pct=100, canary_version="v3", canary_pct=10
    )
    versions = Counter(assign_arm("demo", f"d{i}", canary).version for i in range(n))
    assert versions["v3"] / n == pytest.approx(0.10, abs=0.015)
    assert versions[None] + versions["v3"] == n


def test_validation() -> None:
    with pytest.raises(ValueError):
        Experiment(treatment_pct=101)
    with pytest.raises(ValueError, match="canary_version"):
        Experiment(canary_pct=5)
    with pytest.raises(ValueError, match="shadow_version"):
        Experiment(shadow_pct=5)
    with pytest.raises(ValueError, match="unknown"):
        Experiment.from_dict({"bogus": 1})
