from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from recsys.engine import (
    ArtifactError,
    ArtifactExistsError,
    EngineConfig,
    Experiment,
    ServingState,
    load_bundle,
    verify_artifact,
    write_artifact,
)
from recsys.engine.schema import MANIFEST, POPULARITY, SUCCESS_MARKER
from recsys.training.synthetic import random_payload


@pytest.fixture
def published(tmp_path: Path) -> Path:
    payload = random_payload(60, k=8, i2i_k=5, n_categories=4)
    return write_artifact(tmp_path, "v1", payload)


def test_roundtrip(published: Path) -> None:
    bundle = load_bundle(published)
    assert bundle.version == "v1"
    assert bundle.n_items == 60
    assert bundle.factors.shape == (60, 8)
    assert bundle.config == EngineConfig()
    assert not (published.parent / ".tmp-v1-1").exists()


def test_rejects_republish(tmp_path: Path, published: Path) -> None:
    with pytest.raises(ArtifactExistsError):
        write_artifact(published.parent, "v1", random_payload(60, k=8, i2i_k=5))


def test_missing_success_marker_means_no_artifact(published: Path) -> None:
    (published / SUCCESS_MARKER).unlink()
    with pytest.raises(ArtifactError, match=SUCCESS_MARKER):
        load_bundle(published)


def test_corruption_is_detected(published: Path) -> None:
    verify_artifact(published)
    path = published / POPULARITY
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(ArtifactError, match="does not match"):
        verify_artifact(published)
    assert load_bundle(published, verify=False).n_items == 60


def test_newer_engine_requirement_is_refused(published: Path) -> None:
    manifest = json.loads((published / MANIFEST).read_text())
    manifest["engine_min_version"] = "99.0.0"
    (published / MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ArtifactError, match="needs engine"):
        load_bundle(published)


def test_payload_shape_mismatch_is_refused(tmp_path: Path) -> None:
    payload = random_payload(30, k=4, i2i_k=3)
    broken = replace(payload, popularity=np.zeros(29, dtype=np.float32))
    with pytest.raises(ArtifactError, match="shape mismatch"):
        write_artifact(tmp_path, "v1", broken)
    assert not list(tmp_path.iterdir())  # nothing half-written


def test_serving_state_roundtrip() -> None:
    state = ServingState(
        app="demo",
        version="v1",
        kill=True,
        blocked=frozenset({"b", "a"}),
        experiment=Experiment(canary_version="v2", canary_pct=5),
    )
    assert ServingState.from_json(state.to_json()) == state
    assert state.required_versions() == {"v1", "v2"}
