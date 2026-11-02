"""The two periodic jobs of the loop, as plain functions.

``run_drift_check`` is what a 6-hourly scheduler runs; ``run_rollout_step`` is
what an hourly one runs. Both read logs, call the pure policies, and act on the
registry, so the whole loop works without Airflow.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from recsys.engine.bundle import load_bundle
from recsys.loop.collect import (
    collect_canary_stats,
    collect_drift_signals,
    collect_shadow_stats,
)
from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.monitoring.policy import Action, Decision, DriftSignals, decide
from recsys.monitoring.served_log import load_since
from recsys.registry import RegistryError, RolloutStage, SqliteRegistry
from recsys.rollout.controller import DEFAULT_CANARY_PCT, apply_canary, apply_shadow
from recsys.rollout.policy import Outcome, RolloutThresholds, Verdict
from recsys.training.job import TrainOutcome

DEFAULT_WINDOW_HOURS = 72
_REVISION = re.compile(r"-r(\d+)$")


@dataclass(frozen=True, slots=True)
class DriftCheck:
    """Result of one drift check.

    Attributes:
        decision: What the policy chose.
        signals: The inputs it chose from.
        drifted: ``layer.column`` of every column over the PSI threshold.
        retrain: Outcome of the retrain, when one was executed.
    """

    decision: Decision
    signals: DriftSignals
    drifted: tuple[str, ...]
    retrain: TrainOutcome | None = None


@dataclass(frozen=True, slots=True)
class RolloutStep:
    """Result of one rollout step."""

    rollout_id: str | None
    stage: RolloutStage | None
    verdict: Verdict | None = None


def next_revision(registry: SqliteRegistry, app: str) -> int:
    """Return the next free ``-rN`` suffix for off-schedule retrains."""
    found = [
        int(m.group(1))
        for v in registry.list_versions(app)
        if (m := _REVISION.search(v.model_version))
    ]
    return max(found, default=0) + 1


def run_drift_check(
    registry: SqliteRegistry,
    app: str,
    *,
    served_path: Path,
    interactions_path: Path,
    now: datetime,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    dq_blocking_ok: bool = True,
    retrain: Callable[[int], TrainOutcome] | None = None,
    metrics: BatchMetrics | None = None,
) -> DriftCheck:
    """Measure drift for ``app``, decide, and optionally retrain.

    Args:
        registry: Registry holding the active version and the freeze flag.
        app: App slug.
        served_path: ``rec.served`` JSON-lines file.
        interactions_path: Interactions JSON-lines file.
        now: Evaluation time.
        window_hours: How much log to look at.
        dq_blocking_ok: Outcome of the external blocking data-quality checks.
        retrain: Called with the revision number when the decision is ``retrain``.
        metrics: Receives drift and outcome metrics.

    Raises:
        RegistryError: If ``app`` has no active version to compare against.
    """
    active = registry.get_active(app)
    if active is None:
        raise RegistryError(f"{app} has no active version")
    reference: dict[str, Any] = json.loads(
        (Path(active.artifact_path) / "reference.json").read_text()
    )
    since = now - timedelta(hours=window_hours)
    served = load_since(served_path, since)
    interactions = load_since(interactions_path, since)
    signals, drift = collect_drift_signals(
        registry,
        app,
        reference,
        served,
        interactions,
        now=now,
        dq_blocking_ok=dq_blocking_ok,
    )
    decision = decide(signals)
    outcome = None
    if decision.action is Action.RETRAIN and retrain is not None:
        outcome = retrain(next_revision(registry, app))
    if metrics:
        metrics.record_drift(app, drift)
        metrics.retrain_total.labels(app, decision.action.value).inc()
        metrics.frozen.labels(app).set(int(signals.frozen))
    return DriftCheck(
        decision,
        signals,
        tuple(f"{c.layer}.{c.column}" for c in drift.drifted),
        outcome,
    )


def run_rollout_step(
    registry: SqliteRegistry,
    app: str,
    *,
    served_path: Path,
    interactions_path: Path,
    now: datetime,
    metrics: BatchMetrics | None = None,
    actor: str = "rec-loop",
    thresholds: RolloutThresholds | None = None,
    canary_pct: float = DEFAULT_CANARY_PCT,
) -> RolloutStep:
    """Judge the release in progress for ``app`` and move it forward.

    Does nothing when no release is running or when it waits for a human.
    """
    rollout = registry.active_rollout(app)
    active = registry.get_active(app)
    if rollout is None or active is None:
        return RolloutStep(None, None)
    started = time.perf_counter()
    verdict: Verdict | None = None
    if rollout.stage is RolloutStage.SHADOW:
        rows = load_since(served_path, datetime.fromisoformat(rollout.started_at))
        stats = collect_shadow_stats(
            rows,
            version=rollout.model_version,
            active_version=active.model_version,
            active_bundle=load_bundle(Path(active.artifact_path), verify=False),
        )
        verdict = apply_shadow(
            registry,
            rollout.rollout_id,
            stats,
            actor=actor,
            thresholds=thresholds or RolloutThresholds(),
            canary_pct=canary_pct,
        )
    elif rollout.stage is RolloutStage.CANARY:
        since = datetime.fromisoformat(rollout.stage_since)
        canary_stats = collect_canary_stats(
            load_since(served_path, since),
            load_since(interactions_path, since),
            canary_version=rollout.model_version,
            active_version=active.model_version,
            since=since,
            now=now,
        )
        verdict = apply_canary(
            registry,
            rollout.rollout_id,
            canary_stats,
            actor=actor,
            thresholds=thresholds or RolloutThresholds(),
        )
    after = registry.get_rollout(rollout.rollout_id)
    stage = after.stage if after else None
    if metrics:
        _record_rollout(metrics, app, stage, verdict, time.perf_counter() - started)
    return RolloutStep(rollout.rollout_id, stage, verdict)


def _record_rollout(
    metrics: BatchMetrics,
    app: str,
    stage: RolloutStage | None,
    verdict: Verdict | None,
    elapsed: float,
) -> None:
    if stage in {
        RolloutStage.PROMOTED,
        RolloutStage.ROLLED_BACK,
        RolloutStage.REJECTED,
    }:
        metrics.rollout_total.labels(app, stage.value).inc()
    if verdict and verdict.outcome is Outcome.ROLLBACK:
        metrics.rollback_seconds.labels(app).set(elapsed)
