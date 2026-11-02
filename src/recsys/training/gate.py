"""Offline eval gate: decides whether a trained model may become ``validated``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """Gate limits.

    Attributes:
        min_lift_vs_baseline: Required NDCG ratio against the popularity baseline.
        max_drop_vs_active: Largest tolerated relative NDCG drop vs. the active model.
        min_coverage_ratio: Required coverage relative to the active model.
        max_gini_delta: Largest tolerated absolute change of the exposure Gini.
        min_items: Smallest acceptable catalog.
    """

    min_lift_vs_baseline: float = 1.0
    max_drop_vs_active: float = 0.05
    min_coverage_ratio: float = 0.90
    max_gini_delta: float = 0.05
    min_items: int = 50


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One gate condition and how the model scored on it."""

    name: str
    passed: bool
    value: float
    threshold: float


@dataclass(frozen=True, slots=True)
class GateResult:
    """Outcome of :func:`evaluate_gate`."""

    checks: tuple[GateCheck, ...]

    @property
    def passed(self) -> bool:
        """``True`` when every check passed."""
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        """Serialise for the manifest and the registry."""
        return {
            "passed": self.passed,
            "checks": {
                c.name: {"passed": c.passed, "value": c.value, "threshold": c.threshold}
                for c in self.checks
            },
        }


def evaluate_gate(
    candidate: Mapping[str, float],
    baseline: Mapping[str, float],
    active: Mapping[str, float] | None,
    *,
    n_items: int,
    k: int = 10,
    thresholds: GateThresholds = GateThresholds(),  # noqa: B008 - frozen, shared
) -> GateResult:
    """Compare candidate metrics with the baseline and the active model.

    Args:
        candidate: ``BacktestReport.metrics()`` of the new model.
        baseline: Metrics of the popularity baseline.
        active: Metrics of the model currently serving, ``None`` on first run.
        n_items: Catalog size.
        k: Cut-off the NDCG metric is keyed with.
        thresholds: Gate limits.
    """
    key = f"ndcg@{k}"
    checks = [
        GateCheck(
            "catalog_size",
            n_items >= thresholds.min_items,
            float(n_items),
            float(thresholds.min_items),
        ),
    ]
    lift = candidate[key] / baseline[key] if baseline[key] > 0 else float("inf")
    checks.append(
        GateCheck(
            "lift_vs_baseline",
            lift >= thresholds.min_lift_vs_baseline,
            lift,
            thresholds.min_lift_vs_baseline,
        )
    )
    if active is not None:
        floor = 1.0 - thresholds.max_drop_vs_active
        ratio = candidate[key] / active[key] if active[key] > 0 else float("inf")
        checks.append(GateCheck("ndcg_vs_active", ratio >= floor, ratio, floor))
        cov = (
            candidate["coverage"] / active["coverage"]
            if active["coverage"] > 0
            else float("inf")
        )
        checks.append(
            GateCheck(
                "coverage_vs_active",
                cov >= thresholds.min_coverage_ratio,
                cov,
                thresholds.min_coverage_ratio,
            )
        )
        delta = abs(candidate["gini"] - active["gini"])
        checks.append(
            GateCheck(
                "gini_delta",
                delta <= thresholds.max_gini_delta,
                delta,
                thresholds.max_gini_delta,
            )
        )
    return GateResult(tuple(checks))
