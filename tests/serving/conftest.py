from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recsys.engine import Experiment, write_artifact
from recsys.registry import ModelStatus, ModelVersion, SqliteRegistry
from recsys.serving.app import create_app
from recsys.serving.sinks import MemorySink
from recsys.settings import Settings
from recsys.training.synthetic import random_payload

APP = "demo"


class Harness:
    """A running app plus the registry that drives it and the sink it logs to."""

    def __init__(self, root: Path, sink: MemorySink, registry: SqliteRegistry) -> None:
        self.root, self.sink, self.registry = root, sink, registry

    def publish(self, version: str, *, promote: bool = True) -> None:
        write_artifact(
            self.root / APP, version, random_payload(300, k=8, i2i_k=10, n_categories=6)
        )
        self.registry.record(
            ModelVersion(
                APP, version, ModelStatus.VALIDATED, "h", str(self.root / APP / version)
            )
        )
        if promote:
            self.registry.set_active(APP, version, actor="test")


def make_settings(root: Path, **overrides: object) -> Settings:
    return Settings(
        artifact_dir=root,
        registry_db=root / "reg.sqlite3",
        apps=[APP],
        refresh_interval_s=3600,
        refresh_jitter_s=0,
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(
        tmp_path, MemorySink(), SqliteRegistry(tmp_path / "reg.sqlite3", tmp_path)
    )


@pytest.fixture
def client(harness: Harness) -> Iterator[TestClient]:
    harness.publish("v1")
    # everyone is treated: no holdout, no control
    harness.registry.set_experiment(
        APP, Experiment(treatment_pct=100, holdout_pct=0), actor="test"
    )
    app = create_app(make_settings(harness.root), sink=harness.sink)
    with TestClient(app) as test_client:
        yield test_client


def rec_body(**overrides: object) -> dict[str, object]:
    return {
        "app": APP,
        "device_id": "dev-1",
        "recent": ["i00001", "i00002"],
        "limit": 5,
        **overrides,
    }
