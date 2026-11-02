"""The whole loop in one process.

Train, serve, observe, drift, retrain, release, roll back.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recsys.engine import Experiment
from recsys.monitoring.drift import compute_drift, distributions_from_served
from recsys.monitoring.policy import Action, DriftSignals, decide
from recsys.registry import ModelStatus, RolloutStage, SqliteRegistry
from recsys.rollout.controller import apply_canary, apply_shadow
from recsys.rollout.policy import CanaryStats, Outcome, ShadowStats
from recsys.serving.app import create_app
from recsys.serving.sinks import MemorySink
from recsys.settings import Settings
from recsys.training.job import TrainParams, run_training
from recsys.training.synthetic import SyntheticSource

pytestmark = pytest.mark.slow
APP = "demo"
GOOD_SHADOW = ShadowStats(30, 9000, 12.0, 0.001, 0.05, 0.5, 0.05, 0.05)
GOOD_CANARY = CanaryStats(30, 3000, 3000, (1.0, 0.99, 1.01), 0.05, 0.0)


def traffic(client: TestClient, n: int, country: str) -> None:
    for i in range(n):
        client.post(
            "/v1/rec",
            json={
                "app": APP,
                "device_id": f"dev-{country}-{i}",
                "recent": [f"i{i % 50:05d}"],
                "lang": "en",
            },
            headers={"x-country": country},
        )


def test_closed_loop(tmp_path: Path) -> None:
    registry = SqliteRegistry(tmp_path / "reg.sqlite3", tmp_path / "art")
    params = TrainParams(app=APP, artifacts_root=tmp_path / "art")
    source = SyntheticSource(n_items=200, n_users=800)

    # 1. train + human promotion (autonomy L0/L1)
    v1 = run_training(source, registry, params)
    assert v1.status == "validated"
    registry.set_active(APP, v1.version, actor="alice", reason="launch")
    registry.set_experiment(
        APP, Experiment(treatment_pct=100, holdout_pct=0), actor="alice"
    )

    # 2. serve and observe: every request leaves a rec.served record
    sink = MemorySink()
    settings = Settings(
        artifact_dir=tmp_path / "art",
        apps=[APP],
        refresh_interval_s=3600,
        refresh_jitter_s=0,
    )
    with TestClient(create_app(settings, sink=sink)) as client:
        traffic(client, 60, "DE")
        reference = __import__("json").loads(
            (v1.artifact_path / "reference.json").read_text()
        )

        # 3. drift: all traffic comes from DE while training saw a US/JP/VN/DE mix
        drift = compute_drift(reference, distributions_from_served(sink.messages))
        assert "country_group" in {c.column for c in drift.drifted}

        signals = DriftSignals(
            max_input_psi=drift.max_score("inputs") or 0.0,
            output_psi=drift.max_score("outputs"),
            proxy_metric_drop=0.1,
            new_rows_since_train=100_000,
            hours_since_last_train=26,
            retrains_today=0,
            dq_blocking_ok=True,
            rollout_in_progress=registry.active_rollout(APP) is not None,
            frozen=registry.loop_state(APP).frozen,
        )
        decision = decide(signals)
        assert decision.action is Action.RETRAIN

        # 4. off-schedule retrain (a revision, because the data day is unchanged)
        v2 = run_training(source, registry, replace(params, revision=1))
        assert v2.status == "validated" and v2.version == f"{v1.version}-r1"
        assert registry.active_version(APP) == v1.version  # nothing auto-promoted

        # 5. release: shadow -> canary -> human approval -> promote
        rollout = registry.start_rollout(APP, v2.version, actor="dag", autonomy="L2")
        assert (
            apply_shadow(registry, rollout.rollout_id, GOOD_SHADOW, actor="dag").outcome
            is Outcome.PASS
        )
        assert (
            apply_canary(registry, rollout.rollout_id, GOOD_CANARY, actor="dag").outcome
            is Outcome.PASS
        )
        current = registry.get_rollout(rollout.rollout_id)
        assert current is not None
        assert current.stage is RolloutStage.AWAITING_APPROVAL
        registry.set_active(APP, v2.version, actor="alice", reason="approved")
        client.app.state.registries[APP].refresh_once()  # type: ignore[attr-defined]
        assert (
            client.post("/v1/rec", json={"app": APP, "device_id": "x"}).json()[
                "model_version"
            ]
            == v2.version
        )

        # 6. a bad third model is rolled back, and the loop freezes
        v3 = run_training(source, registry, replace(params, revision=2))
        bad = registry.start_rollout(APP, v3.version, actor="dag", autonomy="L3")
        apply_shadow(registry, bad.rollout_id, GOOD_SHADOW, actor="dag")
        verdict = apply_canary(
            registry,
            bad.rollout_id,
            replace(GOOD_CANARY, ctr_ratio_6h=(0.6, 0.5)),
            actor="dag",
        )
        assert verdict.outcome is Outcome.ROLLBACK
        assert registry.active_version(APP) == v2.version  # active model untouched
        assert registry.get(APP, v3.version).status is ModelStatus.ROLLED_BACK  # type: ignore[union-attr]

        # 7. frozen: a new drift signal may alert but never retrains
        frozen = replace(
            signals, frozen=registry.loop_state(APP).frozen, hours_since_last_train=99
        )
        after = decide(frozen)
        assert after.action is Action.ALERT and "guard:frozen" in after.reasons
        with pytest.raises(Exception, match="frozen"):
            registry.start_rollout(APP, v1.version, actor="dag")

    assert all(m["app_id"] == APP for m in sink.messages)
    assert {m["model_version"] for m in sink.messages} >= {v1.version, v2.version}
