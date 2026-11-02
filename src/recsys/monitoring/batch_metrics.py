"""Metrics of the batch jobs (training, drift, rollout), pushed to a Pushgateway.

Batch processes are too short-lived to be scraped, so they push once at the end.
Pushing is best effort: a failure is logged and never fails the job.
"""

from __future__ import annotations

import logging
import time

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
    push_to_gateway,
)

from recsys.monitoring.drift import DriftResult

log = logging.getLogger(__name__)


class BatchMetrics:
    """All batch-side metrics, in a private registry."""

    def __init__(self) -> None:
        """Create the metrics."""
        self.registry = CollectorRegistry()
        r = self.registry
        self.train_last_success = Gauge(
            "rec_train_last_success_timestamp",
            "Unix time of the last good training run.",
            ["app"],
            registry=r,
        )
        self.train_gate_passed = Gauge(
            "rec_train_gate_passed",
            "1 if the last run passed the gate.",
            ["app"],
            registry=r,
        )
        self.train_ndcg = Gauge(
            "rec_train_ndcg", "Backtest NDCG of the last run.", ["app"], registry=r
        )
        self.drift_psi = Gauge(
            "rec_drift_psi", "PSI per column.", ["app", "layer", "feature"], registry=r
        )
        self.retrain_total = Counter(
            "rec_loop_retrain_total",
            "Drift-check outcomes.",
            ["app", "result"],
            registry=r,
        )
        self.rollout_total = Counter(
            "rec_loop_rollout_total", "Release outcomes.", ["app", "result"], registry=r
        )
        self.frozen = Gauge(
            "rec_loop_frozen", "1 while the app is frozen.", ["app"], registry=r
        )
        self.srm_pvalue = Gauge(
            "rec_ab_srm_pvalue",
            "Sample-ratio-mismatch p-value of the traffic split.",
            ["app"],
            registry=r,
        )
        self.skew_psi = Gauge(
            "rec_skew_psi",
            "PSI of served traffic vs. the user table, per column.",
            ["app", "column"],
            registry=r,
        )
        self.rollback_seconds = Gauge(
            "rec_loop_rollback_seconds",
            "Seconds from a rollback verdict to it taking effect.",
            ["app"],
            registry=r,
        )
        self.mirror_drift = Gauge(
            "rec_mlflow_mirror_drift",
            "1 if the MLflow mirror disagrees with the registry.",
            ["app"],
            registry=r,
        )
        self.mirror_failures = Gauge(
            "rec_mlflow_mirror_failures_total",
            "Mirror operations that failed.",
            ["app", "op"],
            registry=r,
        )

    def record_training(self, app: str, *, passed: bool, ndcg: float) -> None:
        """Record a finished training run."""
        self.train_gate_passed.labels(app).set(int(passed))
        self.train_ndcg.labels(app).set(ndcg)
        if passed:
            self.train_last_success.labels(app).set(time.time())

    def record_drift(self, app: str, result: DriftResult) -> None:
        """Record the PSI of every compared column."""
        for column in result.columns:
            self.drift_psi.labels(app, column.layer, column.column).set(column.score)

    def render(self) -> bytes:
        """Return the text exposition (for tests and debugging)."""
        return generate_latest(self.registry)

    def push(self, url: str | None, app: str, job: str = "rec-batch") -> bool:
        """Push everything to the gateway; returns whether it worked."""
        if not url:
            return False
        try:
            push_to_gateway(
                url, job=job, registry=self.registry, grouping_key={"app": app}
            )
        except Exception:
            log.exception("batch_metrics.push_failed")
            return False
        return True
