from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from recsys.engine import load_bundle
from recsys.engine.schema import REFERENCE
from recsys.registry import ModelStatus, SqliteRegistry
from recsys.training.backtest import engine_ranker, evaluate, popularity_ranker
from recsys.training.dataset import build_dataset
from recsys.training.gate import GateThresholds, evaluate_gate
from recsys.training.job import TrainOutcome, TrainParams, make_version, run_training
from recsys.training.synthetic import SyntheticSource

pytestmark = pytest.mark.slow


def test_dataset_split_has_no_leakage() -> None:
    dataset = build_dataset(SyntheticSource(n_users=300).load(), test_days=7)
    assert np.max(dataset.train_ts) < dataset.split_day
    assert dataset.train_user.size and dataset.test_user.size
    recent = dataset.recent_by_user(max_recent=5)
    assert all(len(items) <= 5 for items in recent.values())


def test_dataset_rejects_impossible_split() -> None:
    with pytest.raises(ValueError):
        build_dataset(SyntheticSource(n_users=50).load(), test_days=100)


def test_trained_model_beats_popularity(trained: tuple[Path, TrainOutcome]) -> None:
    _, outcome = trained
    assert outcome.status == "validated"
    assert outcome.gate is not None and outcome.gate.passed
    bundle = load_bundle(outcome.artifact_path)
    dataset = build_dataset(SyntheticSource().load())
    model = evaluate(dataset, engine_ranker(bundle))
    baseline = evaluate(dataset, popularity_ranker(bundle))
    assert model.ndcg > baseline.ndcg
    assert 0 < model.coverage <= 1
    assert 0 <= model.gini < 1
    assert sum(model.tier_share.values()) == pytest.approx(1.0)


def test_artifact_contains_verified_reference(
    trained: tuple[Path, TrainOutcome],
) -> None:
    _, outcome = trained
    bundle = load_bundle(outcome.artifact_path, verify=True)
    assert REFERENCE in bundle.manifest["files"]
    reference = (outcome.artifact_path / REFERENCE).read_text()
    assert '"inputs"' in reference and '"outputs"' in reference
    assert "gate" in bundle.manifest and "lineage" in bundle.manifest


def test_training_is_idempotent_and_revision_forces_a_new_version(
    tmp_path: Path,
) -> None:
    registry = SqliteRegistry(tmp_path / "r.db", tmp_path / "a")
    params = TrainParams(app="demo", artifacts_root=tmp_path / "a")
    source = SyntheticSource(n_items=120, n_users=400)
    first = run_training(source, registry, params)
    again = run_training(source, registry, params)
    assert again.status == "exists" and again.version == first.version
    revised = run_training(
        source,
        registry,
        TrainParams(app="demo", artifacts_root=tmp_path / "a", revision=2),
    )
    assert revised.version == f"{first.version}-r2"
    assert registry.get("demo", revised.version) is not None


def test_failing_the_gate_registers_a_rejected_version(tmp_path: Path) -> None:
    registry = SqliteRegistry(tmp_path / "r.db", tmp_path / "a")
    impossible = GateThresholds(min_lift_vs_baseline=1000.0)
    outcome = run_training(
        SyntheticSource(n_items=120, n_users=400),
        registry,
        TrainParams(app="demo", artifacts_root=tmp_path / "a", thresholds=impossible),
    )
    assert outcome.status == "rejected"
    stored = registry.get("demo", outcome.version)
    assert stored is not None and stored.status is ModelStatus.REJECTED


def test_make_version() -> None:
    assert make_version(date(2026, 11, 1), "abcdef123456") == "20261101-abcdef"
    assert make_version(date(2026, 11, 1), "abcdef123456", 3) == "20261101-abcdef-r3"


def test_gate_compares_with_the_active_model() -> None:
    base = {"ndcg@10": 0.10, "coverage": 0.5, "gini": 0.5}
    baseline = {"ndcg@10": 0.05, "coverage": 0.1, "gini": 0.9}
    ok = evaluate_gate(base, baseline, {**base}, n_items=100)
    assert ok.passed
    worse = evaluate_gate({**base, "ndcg@10": 0.08}, baseline, base, n_items=100)
    assert not worse.passed
    assert [c.name for c in worse.checks if not c.passed] == ["ndcg_vs_active"]
    skewed = evaluate_gate({**base, "gini": 0.7}, baseline, base, n_items=100)
    assert [c.name for c in skewed.checks if not c.passed] == ["gini_delta"]
    tiny = evaluate_gate(base, baseline, None, n_items=10)
    assert [c.name for c in tiny.checks if not c.passed] == ["catalog_size"]


def test_svd_factors_are_unit_or_zero(trained: tuple[Path, TrainOutcome]) -> None:
    _, outcome = trained
    factors = np.asarray(load_bundle(outcome.artifact_path).factors)
    norms = np.linalg.norm(factors, axis=1)
    assert np.all((np.abs(norms - 1) < 1e-4) | (norms < 1e-6))
