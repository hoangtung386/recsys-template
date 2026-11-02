from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from recsys.registry import (
    ModelStatus,
    ModelVersion,
    RegistryError,
    RolloutStage,
    SqliteRegistry,
)

APP = "demo"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def add(registry: SqliteRegistry, *versions: str) -> None:
    for v in versions:
        registry.record(ModelVersion(APP, v, ModelStatus.VALIDATED, "h", "/x"))


def test_challenger_hook_fires_at_release_start_and_end(tmp_path: Path) -> None:
    calls: list[str | None] = []
    registry = SqliteRegistry(
        tmp_path / "r.db", tmp_path / "a", on_challenger=lambda _app, v: calls.append(v)
    )
    add(registry, "v1", "v2")
    registry.set_active(APP, "v1", actor="a")
    rollout = registry.start_rollout(APP, "v2", actor="a")
    registry.end_rollout(rollout.rollout_id, RolloutStage.ABORTED, actor="a")
    assert calls == ["v2", None]


def test_hook_failures_never_break_a_release(tmp_path: Path) -> None:
    def boom(_app: str, _version: str | None) -> None:
        raise RuntimeError("mlflow down")

    registry = SqliteRegistry(tmp_path / "r.db", tmp_path / "a", on_challenger=boom)
    add(registry, "v1", "v2")
    registry.set_active(APP, "v1", actor="a")
    assert registry.start_rollout(APP, "v2", actor="a").stage is RolloutStage.SHADOW


def test_hooks_can_be_attached_later(registry: SqliteRegistry) -> None:
    seen: list[tuple[str, str | None]] = []
    registry.set_hooks(on_promote=lambda _a, v, p: seen.append((v, p)))
    add(registry, "v1", "v2")
    registry.set_active(APP, "v1", actor="a")
    registry.set_active(APP, "v2", actor="a")
    assert seen == [("v1", None), ("v2", "v1")]


def test_new_rollouts_take_the_apps_autonomy_level(registry: SqliteRegistry) -> None:
    add(registry, "v1", "v2")
    registry.set_active(APP, "v1", actor="a")
    assert registry.get_autonomy(APP) == "L1"
    registry.set_autonomy(APP, "L2", actor="a")
    assert registry.start_rollout(APP, "v2", actor="a").autonomy == "L2"


def test_l3_requires_two_weeks_at_l2(tmp_path: Path) -> None:
    clock = Clock()
    registry = SqliteRegistry(tmp_path / "r.db", tmp_path / "a", clock=clock)
    with pytest.raises(RegistryError, match="L3 needs"):
        registry.set_autonomy(APP, "L3", actor="a")  # still L1
    registry.set_autonomy(APP, "L2", actor="a")
    clock.now += timedelta(days=13)
    with pytest.raises(RegistryError, match="L3 needs"):
        registry.set_autonomy(APP, "L3", actor="a")
    clock.now += timedelta(days=1)
    assert registry.set_autonomy(APP, "L3", actor="a") == "L3"


def test_force_is_audited_and_levels_are_validated(registry: SqliteRegistry) -> None:
    registry.set_autonomy(APP, "L3", actor="alice", reason="incident drill", force=True)
    assert any(e.action == "autonomy:L3:forced" for e in registry.audit_log(APP))
    with pytest.raises(RegistryError, match="unknown"):
        registry.set_autonomy(APP, "L9", actor="a")


def test_previous_version_is_exposed(registry: SqliteRegistry) -> None:
    add(registry, "v1", "v2")
    registry.set_active(APP, "v1", actor="a")
    assert registry.previous_version(APP) is None
    registry.set_active(APP, "v2", actor="a")
    assert registry.previous_version(APP) == "v1"
