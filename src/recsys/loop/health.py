"""Traffic-health check: is the A/B split honest, and is the population sane?

Two cheap guards that decide whether the other loop numbers can be trusted:

* **SRM** (sample-ratio mismatch): devices per arm must match the configured
  split. If not, the experiment is invalid and its CTRs must not be read.
* **Skew**: the population hitting serving must look like the user table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from recsys.monitoring.ab import (
    SRM_ALARM,
    devices_per_arm,
    expected_arm_shares,
    srm_pvalue,
)
from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.monitoring.served_log import load_since
from recsys.monitoring.skew import SkewResult, skew_check
from recsys.registry import SqliteRegistry

DEFAULT_WINDOW_HOURS = 24
MIN_DEVICES = 1000


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """Outcome of one health check.

    Attributes:
        arm_devices: Observed distinct devices per arm.
        expected_share: Configured share per arm.
        srm_pvalue: ``None`` when there were too few devices to test.
        skew: ``None`` when no user distributions were supplied.
        alarms: Human-readable reasons the check failed (empty when healthy).
    """

    arm_devices: dict[str, int]
    expected_share: dict[str, float]
    srm_pvalue: float | None
    skew: SkewResult | None
    alarms: tuple[str, ...] = field(default=())

    @property
    def healthy(self) -> bool:
        """Whether no alarm fired."""
        return not self.alarms


def run_health_check(
    registry: SqliteRegistry,
    app: str,
    *,
    served_path: Path,
    now: datetime,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    user_attr_path: Path | None = None,
    min_devices: int = MIN_DEVICES,
    metrics: BatchMetrics | None = None,
) -> HealthCheck:
    """Test the traffic split of ``app`` and, optionally, population skew.

    Args:
        registry: Source of the configured experiment.
        app: App slug.
        served_path: ``rec.served`` JSON-lines file.
        now: Evaluation time.
        window_hours: How much log to look at. Keep it inside one experiment
            configuration: changing the split mid-window triggers a false SRM.
        user_attr_path: JSON of same-day user-table distributions keyed
            ``country_group``, ``lang``, ``tier`` (each ``value -> share``).
        min_devices: Fewer observed devices than this skips the SRM test.
        metrics: Receives the p-value and the skew scores.
    """
    rows = load_since(served_path, now - timedelta(hours=window_hours))
    expected = expected_arm_shares(registry.get_experiment(app))
    observed = devices_per_arm(rows)
    alarms: list[str] = []

    p_value: float | None = None
    if sum(observed.values()) >= min_devices:
        p_value = srm_pvalue(observed, expected)
        if p_value < SRM_ALARM:
            alarms.append(f"srm: p={p_value:.2e} < {SRM_ALARM}; do not read this A/B")
    unknown = set(observed) - set(expected)
    if unknown:
        alarms.append(f"srm: devices in unconfigured arms {sorted(unknown)}")

    skew: SkewResult | None = None
    if user_attr_path is not None:
        skew = skew_check(rows, json.loads(user_attr_path.read_text()))
        alarms.extend(
            f"skew: {name} PSI {skew.scores[name]:.3f}" for name in skew.flagged
        )

    if metrics:
        if p_value is not None:
            metrics.srm_pvalue.labels(app).set(p_value)
        for name, score in (skew.scores if skew else {}).items():
            metrics.skew_psi.labels(app, name).set(score)
    return HealthCheck(observed, expected, p_value, skew, tuple(alarms))
