"""Shared fixtures. Heavy objects are built once per session."""

from __future__ import annotations

from pathlib import Path

import pytest

from recsys.engine import Bundle, EngineConfig, load_bundle, write_artifact
from recsys.registry import SqliteRegistry
from recsys.training.job import TrainOutcome, TrainParams, run_training
from recsys.training.synthetic import SyntheticSource, random_payload

APP = "demo"


@pytest.fixture(scope="session")
def random_bundle(tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    """A random, 600-item bundle: enough to exercise the engine, no training."""
    root = tmp_path_factory.mktemp("random-bundle")
    path = write_artifact(
        root, "v1", random_payload(600, k=16, i2i_k=20, n_categories=8)
    )
    return load_bundle(path)


@pytest.fixture
def registry(tmp_path: Path) -> SqliteRegistry:
    """An empty registry in a temp directory."""
    return SqliteRegistry(tmp_path / "registry.sqlite3", tmp_path / "artifacts")


@pytest.fixture(scope="session")
def trained(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, TrainOutcome]:
    """A really trained artifact root (synthetic data), shared by read-only tests."""
    root = tmp_path_factory.mktemp("trained")
    reg = SqliteRegistry(root / "reg.sqlite3", root / "artifacts")
    outcome = run_training(
        SyntheticSource(), reg, TrainParams(app=APP, artifacts_root=root / "artifacts")
    )
    return root, outcome


@pytest.fixture
def default_config() -> EngineConfig:
    """The default engine configuration."""
    return EngineConfig()
