"""Game day: the loop, driven by real traffic through the real HTTP app.

Scenarios follow section 16.4 of the design document. Timings are asserted where
the document sets a target.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from recsys.engine import Experiment
from recsys.integrations.mlflow_mirror import MlflowMirror
from recsys.integrations.wiring import attach_mirror, reconcile
from recsys.loop.runner import run_drift_check, run_rollout_step
from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.monitoring.policy import Action
from recsys.registry import ModelStatus, RolloutStage, SqliteRegistry
from recsys.rollout.policy import RolloutThresholds
from recsys.serving.app import create_app
from recsys.settings import Settings
from recsys.training.job import TrainOutcome, TrainParams, run_training
from recsys.training.synthetic import SyntheticSource
from tests.integrations.test_integrations import AliasClient

pytestmark = pytest.mark.slow
APP = "demo"
SMALL = RolloutThresholds(
    shadow_min_hours=0,
    shadow_min_requests=50,
    canary_min_hours=0,
    canary_min_per_arm=20,
)


class World:
    """Registry, artifacts, log files and a mirror, in one temp directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.served = root / "served.jsonl"
        self.interactions = root / "interactions.jsonl"
        self.client = AliasClient()
        self.registry = SqliteRegistry(root / "reg.db", root / "art")
        self.mirror = MlflowMirror(self.client)
        attach_mirror(self.registry, self.mirror)
        self.source = SyntheticSource(n_items=200, n_users=800)
        self.params = TrainParams(app=APP, artifacts_root=root / "art")

    def train(self, revision: int | None = None) -> str:
        out = run_training(
            self.source,
            self.registry,
            TrainParams(app=APP, artifacts_root=self.root / "art", revision=revision),
            mirror=self.mirror,
        )
        assert out.status == "validated"
        return out.version

    def settings(self) -> Settings:
        return Settings(
            artifact_dir=self.root / "art",
            apps=[APP],
            refresh_interval_s=3600,
            refresh_jitter_s=0,
            served_log_path=self.served,
        )

    def traffic(
        self, n: int, *, country: str = "US", click: Any = lambda _row: False
    ) -> None:
        """Send ``n`` requests through HTTP; log clicks chosen by ``click(row)``."""
        with TestClient(create_app(self.settings())) as client:
            for i in range(n):
                client.post(
                    "/v1/rec",
                    json={
                        "app": APP,
                        "device_id": f"{country}-{i}",
                        "recent": [f"i{i % 80:05d}", f"i{(i * 7) % 80:05d}"],
                        "lang": "en",
                        "request_id": f"{country}-{self.mark}-{i}",
                    },
                    headers={"x-country": country},
                )
        rows = [json.loads(line) for line in self.served.read_text().splitlines()]
        stamp = datetime.now(UTC).isoformat()
        with self.interactions.open("a") as handle:
            for row in rows[-n:]:
                if row["items"] and click(row):
                    handle.write(
                        json.dumps(
                            {
                                "ts": stamp,
                                "device_id": row["device_id"],
                                "request_id": row["request_id"],
                                "item_id": row["items"][0]["id"],
                                "event": "click",
                            }
                        )
                        + "\n"
                    )

    mark = 0

    def step(self, **kwargs: Any) -> Any:
        self.mark += 1
        return run_rollout_step(
            self.registry,
            APP,
            served_path=self.served,
            interactions_path=self.interactions,
            now=datetime.now(UTC),
            thresholds=SMALL,
            canary_pct=50,
            **kwargs,
        )


@pytest.fixture
def world(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> World:
    caplog.set_level(logging.CRITICAL, logger="recsys")
    w = World(tmp_path)
    v1 = w.train()
    w.registry.set_active(APP, v1, actor="alice", reason="launch")
    w.registry.set_experiment(
        APP, Experiment(treatment_pct=100, holdout_pct=0), actor="alice"
    )
    w.registry.set_autonomy(APP, "L2", actor="alice")
    return w


def test_good_model_is_released_end_to_end(world: World) -> None:
    v1 = world.registry.active_version(APP)
    # scenario 1+2: shifted traffic is detected; the policy decides, a retrain makes v2
    world.traffic(120, country="DE")
    check = run_drift_check(
        world.registry,
        APP,
        served_path=world.served,
        interactions_path=world.interactions,
        now=datetime.now(UTC),
        dq_blocking_ok=True,
    )
    assert "inputs.country_group" in check.drifted
    assert check.decision.action in {Action.ALERT, Action.RETRAIN}
    v2 = world.train(revision=1)
    assert v2 == f"{v1}-r1"

    # scenario 3: shadow, then canary, driven by logs from the real app
    rollout = world.registry.start_rollout(APP, v2, actor="dag", shadow_pct=100)
    candidate = world.registry.get(APP, v2)
    assert candidate is not None
    assert world.client.aliases["challenger"] == candidate.data["mlflow_model_version"]
    world.traffic(100)
    step = world.step()
    assert step.stage is RolloutStage.CANARY, step

    # canary: half of the traffic is on v2 and clicks at the same rate as v1
    world.traffic(160, click=lambda row: True)
    step = world.step()
    assert step.stage is RolloutStage.AWAITING_APPROVAL, step
    assert world.registry.active_version(APP) == v1  # L2: a human still decides

    # scenario 4: approval moves the registry, active.json and the MLflow alias together
    world.registry.set_active(APP, v2, actor="alice", reason="approved")
    assert world.client.aliases["production"] == str(
        candidate.data["mlflow_model_version"]
    )
    assert reconcile(world.registry, world.mirror, APP).in_sync
    state = json.loads((world.root / "art" / APP / "active.json").read_text())
    assert state["version"] == v2
    assert world.registry.get_rollout(rollout.rollout_id).stage is RolloutStage.PROMOTED  # type: ignore[union-attr]


def test_bad_model_is_rolled_back_fast_and_freezes_the_loop(world: World) -> None:
    v1 = world.registry.active_version(APP)
    v2 = world.train(revision=1)
    world.registry.start_rollout(APP, v2, actor="dag", shadow_pct=100)
    world.traffic(100)
    assert world.step().stage is RolloutStage.CANARY

    # scenario 5: v2 never gets clicked while v1 does
    world.traffic(200, click=lambda row: row["model_version"] == v1)
    metrics = BatchMetrics()
    step = world.step(metrics=metrics)
    assert step.stage is RolloutStage.ROLLED_BACK, step
    assert world.registry.active_version(APP) == v1
    assert world.registry.get(APP, v2).status is ModelStatus.ROLLED_BACK  # type: ignore[union-attr]
    text = metrics.render().decode()
    assert 'rec_loop_rollout_total{app="demo",result="rolled_back"} 1.0' in text
    rollback_seconds = float(
        next(
            line
            for line in text.splitlines()
            if line.startswith("rec_loop_rollback_seconds{")
        ).split()[-1]
    )
    assert rollback_seconds < 60  # document target: effective within 60 seconds

    # scenario 6: while frozen a new drift signal cannot trigger a retrain
    world.traffic(120, country="DE")
    retrained: list[int] = []

    def retrain(revision: int) -> TrainOutcome:
        retrained.append(revision)
        raise AssertionError("must not retrain while frozen")

    check = run_drift_check(
        world.registry,
        APP,
        served_path=world.served,
        interactions_path=world.interactions,
        now=datetime.now(UTC),
        retrain=retrain,
    )
    assert check.signals.frozen and not retrained
    assert check.decision.action is not Action.RETRAIN


def test_mlflow_outage_does_not_affect_training_or_promotion(world: World) -> None:
    # scenario 9
    world.client.fail = True
    v2 = world.train(revision=1)
    world.registry.set_active(APP, v2, actor="alice")
    assert world.registry.active_version(APP) == v2
    assert world.mirror.failures["register"] >= 1
    # nothing was mirrored (registration failed), so there is no alias to move
    assert world.registry.get(APP, v2).data["mlflow_model_version"] is None  # type: ignore[union-attr]
