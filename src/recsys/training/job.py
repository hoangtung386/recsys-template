"""The training job: data -> fit -> backtest -> gate -> artifact -> registry."""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal

from recsys.engine.bundle import bundle_from_payload
from recsys.engine.config import EngineConfig
from recsys.engine.writer import write_artifact
from recsys.integrations.mlflow_mirror import MlflowMirror
from recsys.integrations.tracking import start_run
from recsys.registry import ModelStatus, ModelVersion, SqliteRegistry
from recsys.training.backtest import (
    BacktestReport,
    engine_ranker,
    evaluate,
    popularity_ranker,
)
from recsys.training.dataset import Dataset, build_dataset
from recsys.training.fit import fit_payload
from recsys.training.gate import GateResult, GateThresholds, evaluate_gate
from recsys.training.reference import build_reference
from recsys.training.synthetic import DataSource

log = logging.getLogger(__name__)

Status = Literal["validated", "rejected", "exists"]


@dataclass(frozen=True, slots=True)
class TrainParams:
    """Knobs of one training run.

    Attributes:
        app: App slug.
        artifacts_root: Directory under which ``<app>/<version>/`` is published.
        revision: Suffix that forces a new version for the same data day
            (off-schedule retrains); ``None`` for the scheduled run.
        test_days: Length of the held-out period.
        k: Latent dimension.
        i2i_k: Item-item neighbours kept.
        seed: Seed for the fit.
        thresholds: Gate limits.
        tracking_uri: MLflow server, or ``None``.
        lineage: Provenance written to the manifest (commit, image digest, run ids).
        actor: Name recorded in the audit trail.
    """

    app: str
    artifacts_root: Path
    revision: int | None = None
    test_days: int = 7
    k: int = 64
    i2i_k: int = 50
    seed: int = 0
    thresholds: GateThresholds = field(default_factory=GateThresholds)
    tracking_uri: str | None = None
    lineage: Mapping[str, str] = field(default_factory=dict)
    actor: str = "rec-train"


@dataclass(frozen=True, slots=True)
class TrainOutcome:
    """Result of :func:`run_training`."""

    status: Status
    version: str
    artifact_path: Path
    gate: GateResult | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)


def make_version(data_end: date, config_hash: str, revision: int | None = None) -> str:
    """Return ``YYYYMMDD-<hash6>[-rN]``: idempotent per data day and config."""
    base = f"{data_end:%Y%m%d}-{config_hash[:6]}"
    return f"{base}-r{revision}" if revision else base


def _data_summary(dataset: Dataset, source: DataSource) -> dict[str, Any]:
    return {
        "source": type(source).__name__,
        "window": {
            "start": dataset.window_start.isoformat(),
            "end": dataset.window_end.isoformat(),
        },
        "split_day": dataset.split_day,
        "n_items": dataset.n_items,
        "n_users": dataset.n_users,
        "n_train_events": int(dataset.train_user.size),
        "n_test_events": int(dataset.test_user.size),
    }


def _pointer(app: str, version: str, path: Path, config_hash: str) -> str:
    """Write the small artifact pointer that the MLflow mirror registers."""
    target = Path(tempfile.mkdtemp(prefix="rec-pointer-")) / "artifact_pointer.json"
    pointer = {
        "app": app,
        "version": version,
        "artifact_uri": str(path),
        "config_hash": config_hash,
    }
    target.write_text(json.dumps(pointer, sort_keys=True) + "\n")
    return str(target)


def run_training(
    source: DataSource,
    registry: SqliteRegistry,
    params: TrainParams,
    config: EngineConfig | None = None,
    mirror: MlflowMirror | None = None,
) -> TrainOutcome:
    """Train one version for ``params.app`` and register it.

    Publishing happens before registration, so a version in the registry always
    has a complete artifact. A version that fails the gate is still published
    (for inspection) but registered as ``rejected`` and can never be promoted.

    Returns:
        The outcome; ``status == "exists"`` when this data day and config were
        already trained (use ``params.revision`` to force a retrain).
    """
    config = config or EngineConfig()
    dataset = build_dataset(source.load(), test_days=params.test_days)
    version = make_version(dataset.window_end, config.config_hash(), params.revision)
    app_root = params.artifacts_root / params.app
    final = app_root / version
    if final.exists() or registry.get(params.app, version) is not None:
        log.info("train.exists", extra={"app": params.app, "version": version})
        return TrainOutcome("exists", version, final)

    tags = {"app": params.app, "config_hash": config.config_hash(), **params.lineage}
    with start_run(
        f"rec-{params.app}", version, tags, tracking_uri=params.tracking_uri
    ) as tracker:
        data = _data_summary(dataset, source)
        tracker.params(
            {**data, "k": params.k, "i2i_k": params.i2i_k, "seed": params.seed}
        )

        payload = fit_payload(
            dataset, config, k=params.k, i2i_k=params.i2i_k, seed=params.seed
        )
        bundle = bundle_from_payload(payload, version)
        report = evaluate(dataset, engine_ranker(bundle))
        baseline = evaluate(dataset, popularity_ranker(bundle))
        active = registry.get_active(params.app)
        active_metrics = (active.data.get("backtest") if active else None) or None
        gate = evaluate_gate(
            report.metrics(),
            baseline.metrics(),
            active_metrics,
            n_items=dataset.n_items,
            thresholds=params.thresholds,
        )
        tracker.metrics({"backtest": report.metrics(), "baseline": baseline.metrics()})
        tracker.params({"gate_passed": gate.passed})

        summary = _summarise(report, baseline, gate)
        payload = replace(
            payload,
            reference=build_reference(dataset, report, version),
            extra_manifest={
                "lineage": dict(params.lineage),
                "data": data,
                **summary,
            },
        )
        path = write_artifact(app_root, version, payload)
        for name in ("manifest.json", "reference.json"):
            tracker.artifact(str(path / name))
        status = ModelStatus.VALIDATED if gate.passed else ModelStatus.REJECTED

        mlflow_version = None
        if mirror and gate.passed:
            mlflow_version = mirror.register(
                params.app,
                version,
                tracker.run_id,
                _pointer(params.app, version, path, config.config_hash()),
            )
        registry.record(
            ModelVersion(
                app_id=params.app,
                model_version=version,
                status=status,
                config_hash=config.config_hash(),
                artifact_path=str(path),
                run_id=tracker.run_id,
                data={**summary, "data": data, "mlflow_model_version": mlflow_version},
            ),
            actor=params.actor,
        )
    log.info(
        "train.done",
        extra={"app": params.app, "version": version, "status": status.value},
    )
    return TrainOutcome(
        status.value,  # type: ignore[arg-type]
        version,
        path,
        gate,
        report.metrics(),
    )


def _summarise(
    report: BacktestReport, baseline: BacktestReport, gate: GateResult
) -> dict[str, Any]:
    return {
        "backtest": report.metrics(),
        "baseline": baseline.metrics(),
        "gate": gate.to_dict(),
    }
