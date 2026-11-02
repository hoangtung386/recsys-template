from __future__ import annotations

import json
from pathlib import Path

import pytest

from recsys.engine import Experiment, ServingState
from recsys.registry import (
    ModelStatus,
    ModelVersion,
    RegistryError,
    RolloutStage,
    SqliteRegistry,
)

APP = "demo"


def add(
    registry: SqliteRegistry, version: str, status: ModelStatus = ModelStatus.VALIDATED
) -> None:
    registry.record(
        ModelVersion(
            APP,
            version,
            status,
            "hash",
            f"/tmp/{version}",
            data={"backtest": {"ndcg@10": 0.1}},
        )
    )


def state_file(registry: SqliteRegistry) -> ServingState:
    return ServingState.from_json(
        (registry.artifacts_root / APP / "active.json").read_text()
    )


def test_promote_publishes_active_json(registry: SqliteRegistry) -> None:
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="alice", reason="first")
    assert registry.active_version(APP) == "v1"
    assert state_file(registry).version == "v1"
    assert registry.get(APP, "v1").status is ModelStatus.ACTIVE  # type: ignore[union-attr]


def test_cannot_promote_rejected_unknown_or_active(registry: SqliteRegistry) -> None:
    add(registry, "bad", ModelStatus.REJECTED)
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="a")
    for version in ("bad", "missing", "v1"):
        with pytest.raises(RegistryError):
            registry.set_active(APP, version, actor="a")


def test_promotion_retires_previous_and_rollback_restores_it(
    registry: SqliteRegistry,
) -> None:
    add(registry, "v1")
    add(registry, "v2")
    registry.set_active(APP, "v1", actor="a")
    registry.set_active(APP, "v2", actor="a")
    assert registry.get(APP, "v1").status is ModelStatus.RETIRED  # type: ignore[union-attr]
    assert registry.rollback(APP, actor="a", reason="oops") == "v1"
    assert registry.active_version(APP) == "v1"
    assert registry.get(APP, "v2").status is ModelStatus.ROLLED_BACK  # type: ignore[union-attr]
    assert state_file(registry).version == "v1"
    with pytest.raises(RegistryError, match="no previous"):
        registry.rollback(APP, actor="a")


def test_rolled_back_version_can_never_return(registry: SqliteRegistry) -> None:
    add(registry, "v1")
    add(registry, "v2")
    registry.set_active(APP, "v1", actor="a")
    registry.set_active(APP, "v2", actor="a")
    registry.rollback(APP, actor="a")
    with pytest.raises(RegistryError):
        registry.set_active(APP, "v2", actor="a")


def test_record_cannot_overwrite_a_version_that_served(
    registry: SqliteRegistry,
) -> None:
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="a")
    with pytest.raises(RegistryError, match="already"):
        add(registry, "v1")


def test_kill_switch_and_blocklist_reach_the_state_file(
    registry: SqliteRegistry,
) -> None:
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="a")
    registry.set_kill(APP, True, actor="a", reason="incident")
    assert state_file(registry).kill is True
    registry.set_kill(APP, False, actor="a")
    assert registry.block_items(APP, ["x", "y"], actor="a") == {"x", "y"}
    assert registry.block_items(APP, ["x"], actor="a", unblock=True) == {"y"}
    assert state_file(registry).blocked == {"y"}


def test_audit_trail_records_who_did_what(registry: SqliteRegistry) -> None:
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="alice", reason="launch")
    actions = {(e.actor, e.action) for e in registry.audit_log(APP)}
    assert ("alice", "promote") in actions


def test_promote_hook_failure_never_fails_the_promotion(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str | None]] = []

    def hook(app: str, version: str, previous: str | None) -> None:
        calls.append((app, version, previous))
        raise RuntimeError("mlflow is down")

    registry = SqliteRegistry(tmp_path / "r.db", tmp_path / "a", on_promote=hook)
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="a")
    assert calls == [(APP, "v1", None)]
    assert registry.active_version(APP) == "v1"


def test_set_experiment_is_validated_and_published(registry: SqliteRegistry) -> None:
    registry.set_experiment(APP, Experiment(treatment_pct=20), actor="a")
    assert state_file(registry).experiment.treatment_pct == 20
    assert (
        json.loads((registry.artifacts_root / APP / "active.json").read_text())["app"]
        == APP
    )


# ---------------------------------------------------------------- rollouts
def full_release(registry: SqliteRegistry, autonomy: str = "L1") -> str:
    add(registry, "v1")
    registry.set_active(APP, "v1", actor="a")
    add(registry, "v2")
    return registry.start_rollout(APP, "v2", actor="a", autonomy=autonomy).rollout_id


def test_shadow_then_canary_then_approval_then_promote(
    registry: SqliteRegistry,
) -> None:
    rid = full_release(registry)
    assert state_file(registry).experiment.shadow_version == "v2"
    registry.advance_to_canary(rid, pct=10, actor="a")
    exp = state_file(registry).experiment
    assert (exp.shadow_version, exp.canary_version, exp.canary_pct) == (None, "v2", 10)
    registry.advance_to_approval(rid, actor="a")
    assert registry.active_rollout(APP).stage is RolloutStage.AWAITING_APPROVAL  # type: ignore[union-attr]
    registry.set_active(APP, "v2", actor="alice", reason="approved")
    assert registry.get_rollout(rid).stage is RolloutStage.PROMOTED  # type: ignore[union-attr]
    exp = state_file(registry).experiment
    assert (exp.canary_version, exp.shadow_version) == (None, None)
    assert registry.active_rollout(APP) is None


def test_only_one_release_at_a_time(registry: SqliteRegistry) -> None:
    full_release(registry)
    add(registry, "v3")
    with pytest.raises(RegistryError, match="in progress"):
        registry.start_rollout(APP, "v3", actor="a")


def test_stages_cannot_be_skipped(registry: SqliteRegistry) -> None:
    rid = full_release(registry)
    with pytest.raises(RegistryError, match="expected"):
        registry.advance_to_approval(rid, actor="a")


def test_rollback_keeps_active_model_and_freezes_the_app(
    registry: SqliteRegistry,
) -> None:
    rid = full_release(registry)
    registry.advance_to_canary(rid, pct=10, actor="a")
    registry.end_rollout(rid, RolloutStage.ROLLED_BACK, actor="rollout", reason="ctr")
    assert registry.active_version(APP) == "v1"
    assert registry.get(APP, "v2").status is ModelStatus.ROLLED_BACK  # type: ignore[union-attr]
    assert registry.loop_state(APP).frozen
    add(registry, "v3")
    with pytest.raises(RegistryError, match="frozen"):
        registry.start_rollout(APP, "v3", actor="a")
    with pytest.raises(RegistryError, match="reason"):
        registry.unfreeze(APP, actor="a", reason=" ")
    registry.unfreeze(APP, actor="alice", reason="investigated, bad candidate")
    assert registry.start_rollout(APP, "v3", actor="a").stage is RolloutStage.SHADOW


def test_finished_rollout_cannot_end_twice(registry: SqliteRegistry) -> None:
    rid = full_release(registry)
    registry.end_rollout(rid, RolloutStage.ABORTED, actor="a")
    with pytest.raises(RegistryError, match="finished"):
        registry.end_rollout(rid, RolloutStage.ABORTED, actor="a")
    with pytest.raises(RegistryError, match="final"):
        registry.end_rollout(rid, RolloutStage.CANARY, actor="a")
