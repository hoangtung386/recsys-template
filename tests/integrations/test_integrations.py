from __future__ import annotations

from typing import Any

import pytest

import recsys.integrations.tracking as tracking
from recsys.integrations.mlflow_mirror import MlflowMirror
from recsys.integrations.tracking import Tracker, flatten_metrics, start_run
from recsys.integrations.wiring import attach_mirror, build_mirror, reconcile
from recsys.registry import ModelStatus, ModelVersion, SqliteRegistry


def test_tracking_is_a_noop_without_a_server() -> None:
    with start_run("exp", "run", {"a": "b"}, tracking_uri=None) as tracker:
        assert tracker.run_id is None
        tracker.params({"k": 1})
        tracker.metrics({"ndcg": 0.1})
        tracker.artifact("/nonexistent")


def test_tracking_failure_never_breaks_training(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> Any:
        raise ImportError("mlflow not installed")

    monkeypatch.setattr(tracking, "_mlflow", broken)
    with start_run("exp", "run", tracking_uri="http://nowhere") as tracker:
        assert tracker.run_id is None


def test_tracker_swallows_logging_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def log_params(self, _: object) -> None:
            raise OSError("server went away")

    monkeypatch.setattr(tracking, "_mlflow", Boom)
    Tracker("run-1").params({"a": 1})  # must not raise


def test_flatten_metrics_keeps_numbers_only() -> None:
    assert flatten_metrics("", {"a": {"b": 1, "c": "x"}, "d": 2.5, "e": True}) == {
        "a.b": 1.0,
        "d": 2.5,
    }


class FakeClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def create_registered_model(self, name: str) -> None:
        self.calls.append(("create_model", name))

    def create_model_version(self, **kwargs: Any) -> Any:
        if self.fail:
            raise ConnectionError("down")
        self.calls.append(("create_version", kwargs["name"]))
        return type("V", (), {"version": 7})()

    def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.calls.append(("alias", alias, version))

    def delete_registered_model_alias(self, name: str, alias: str) -> None:
        self.calls.append(("unalias", alias))


def test_mirror_registers_and_moves_aliases() -> None:
    client = FakeClient()
    mirror = MlflowMirror(client)
    assert mirror.register("demo", "v1", "run-1", "/tmp/p.json") == "7"
    mirror.on_promote("demo", "7", "6")
    assert ("alias", "production", "7") in client.calls
    assert ("alias", "previous", "6") in client.calls
    assert not mirror.failures


def test_mirror_failures_are_counted_not_raised() -> None:
    mirror = MlflowMirror(FakeClient(fail=True))
    assert mirror.register("demo", "v1", None, "/p") is None
    mirror.on_promote("demo", "7", None)
    assert mirror.failures == {"register": 1, "promote": 1}
    mirror.on_promote("demo", None, None)  # nothing to mirror: no-op


# ------------------------------------------------------------------ wiring
class AliasClient(FakeClient):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__(fail=fail)
        self.aliases: dict[str, str] = {}
        self.counter = 0

    def create_model_version(self, **kwargs: Any) -> Any:
        if self.fail:
            raise ConnectionError("down")
        self.counter += 1
        return type("V", (), {"version": self.counter})()

    def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
        if self.fail:
            raise ConnectionError("down")
        self.aliases[alias] = str(version)

    def delete_registered_model_alias(self, name: str, alias: str) -> None:
        self.aliases.pop(alias, None)

    def get_model_version_by_alias(self, name: str, alias: str) -> Any:
        if alias not in self.aliases:
            raise KeyError(alias)
        return type("V", (), {"version": self.aliases[alias]})()


def _registry_with_mirror(
    tmp_path: Any, client: AliasClient
) -> tuple[Any, MlflowMirror]:
    registry = SqliteRegistry(tmp_path / "r.db", tmp_path / "a")
    mirror = MlflowMirror(client)
    attach_mirror(registry, mirror)
    for version in ("v1", "v2"):
        number = mirror.register("demo", version, None, "/p")
        registry.record(
            ModelVersion(
                "demo",
                version,
                ModelStatus.VALIDATED,
                "h",
                "/x",
                data={"mlflow_model_version": number},
            )
        )
    return registry, mirror


def test_promotion_and_release_move_the_mlflow_aliases(tmp_path: Any) -> None:
    client = AliasClient()
    registry, mirror = _registry_with_mirror(tmp_path, client)
    registry.set_active("demo", "v1", actor="a")
    assert client.aliases == {"production": "1"}
    rollout = registry.start_rollout("demo", "v2", actor="a")
    assert client.aliases["challenger"] == "2"
    registry.advance_to_canary(rollout.rollout_id, pct=10, actor="a")
    registry.advance_to_approval(rollout.rollout_id, actor="a")
    registry.set_active("demo", "v2", actor="a")
    assert client.aliases == {"production": "2", "previous": "1"}
    assert reconcile(registry, mirror, "demo").in_sync


def test_reconcile_detects_a_hand_edited_alias(tmp_path: Any) -> None:
    client = AliasClient()
    registry, mirror = _registry_with_mirror(tmp_path, client)
    registry.set_active("demo", "v1", actor="a")
    client.aliases["production"] = "2"  # someone edited it in the UI
    found = reconcile(registry, mirror, "demo")
    assert not found.in_sync and (found.expected, found.actual) == ("1", "2")


def test_mlflow_outage_does_not_stop_promotion(tmp_path: Any) -> None:
    client = AliasClient()
    registry, mirror = _registry_with_mirror(tmp_path, client)
    client.fail = True
    registry.set_active("demo", "v1", actor="a")
    assert registry.active_version("demo") == "v1"
    assert mirror.failures["promote"] == 1


def test_build_mirror_without_a_server_is_none() -> None:
    assert build_mirror(None) is None
