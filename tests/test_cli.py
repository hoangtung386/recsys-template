from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from recsys.cli import admin, loop, train
from recsys.engine import load_bundle
from recsys.settings import get_settings
from recsys.training.gate import GateThresholds
from recsys.training.job import TrainParams
from tests.loop.rows import served_row

pytestmark = pytest.mark.slow
SMALL = ("--synthetic-items", "150", "--synthetic-users", "500")


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("RECSYS_ARTIFACT_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("RECSYS_REGISTRY_DB", str(tmp_path / "reg.sqlite3"))
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def run(module: Any, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    code = module.main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_train_then_operate(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, trained = run(
        train,
        capsys,
        "--app",
        "demo",
        "--synthetic-items",
        "150",
        "--synthetic-users",
        "500",
    )
    assert code == 0 and trained["status"] == "validated"
    version = trained["version"]

    code, again = run(
        train,
        capsys,
        "--app",
        "demo",
        "--synthetic-items",
        "150",
        "--synthetic-users",
        "500",
    )
    assert code == 0 and again["status"] == "exists"

    _, versions = run(admin, capsys, "versions", "--app", "demo")
    assert versions[0]["status"] == "validated"

    code, _ = run(
        admin,
        capsys,
        "--actor",
        "alice",
        "promote",
        "--app",
        "demo",
        "--version",
        version,
        "--reason",
        "go",
    )
    assert code == 0
    assert (env / "art" / "demo" / "active.json").is_file()

    _, status = run(admin, capsys, "status", "--app", "demo")
    assert status["active"] == version

    run(admin, capsys, "kill", "--app", "demo", "--reason", "drill")
    _, status = run(admin, capsys, "status", "--app", "demo")
    assert status["state"]["kill"] is True
    run(admin, capsys, "unkill", "--app", "demo")

    _, blocked = run(admin, capsys, "block", "--app", "demo", "i00001", "i00002")
    assert blocked["blocked"] == ["i00001", "i00002"]

    _, audit = run(admin, capsys, "audit", "--app", "demo")
    assert {entry["action"] for entry in audit} >= {"promote", "kill_on", "block"}


def test_refused_operations_exit_2(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = admin.main(["promote", "--app", "demo", "--version", "nope"])
    assert code == 2
    assert "refused" in capsys.readouterr().err
    assert admin.main(["rollback", "--app", "demo"]) == 2


def test_release_commands(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--synthetic-items", "150", "--synthetic-users", "500"]
    _, first = run(train, capsys, "--app", "demo", *args)
    run(admin, capsys, "promote", "--app", "demo", "--version", first["version"])
    _, second = run(train, capsys, "--app", "demo", "--revision", "1", *args)
    v2 = second["version"]
    assert v2.endswith("-r1")

    _, shadow = run(
        admin,
        capsys,
        "experiment",
        "shadow",
        "--app",
        "demo",
        "--version",
        v2,
        "--pct",
        "20",
    )
    assert shadow["stage"] == "shadow"
    _, canary = run(
        admin,
        capsys,
        "experiment",
        "canary",
        "--app",
        "demo",
        "--version",
        v2,
        "--pct",
        "10",
    )
    assert canary["stage"] == "canary"
    _, ended = run(
        admin,
        capsys,
        "experiment",
        "end",
        "--app",
        "demo",
        "--rollout",
        canary["rollout_id"],
        "--result",
        "rolled_back",
        "--reason",
        "bad",
    )
    assert ended["stage"] == "rolled_back"
    _, status = run(admin, capsys, "status", "--app", "demo")
    assert status["loop"]["frozen"] is True
    with pytest.raises(SystemExit):  # unfreeze demands a reason
        admin.main(["unfreeze", "--app", "demo"])
    capsys.readouterr()
    code, _ = run(
        admin, capsys, "unfreeze", "--app", "demo", "--reason", "looked into it"
    )
    assert code == 0


def test_rejected_model_exits_3(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def strict(**kwargs: Any) -> TrainParams:
        return TrainParams(
            **kwargs, thresholds=GateThresholds(min_lift_vs_baseline=1e6)
        )

    monkeypatch.setattr("recsys.cli.train.TrainParams", strict)
    code, out = run(
        train,
        capsys,
        "--app",
        "demo",
        "--synthetic-items",
        "150",
        "--synthetic-users",
        "500",
    )
    assert code == 3
    assert out["status"] == "rejected"


# ------------------------------------------------------- operator tooling
def _trained_with_log(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[str, Path]:
    _, trained = run(train, capsys, "--app", "demo", *SMALL)
    version = trained["version"]
    run(admin, capsys, "promote", "--app", "demo", "--version", version)
    bundle = load_bundle(env / "art" / "demo" / version)
    log = env / "served.jsonl"
    now = datetime.now(UTC)
    log.write_text(
        "".join(json.dumps(served_row(bundle, n=i, when=now)) + "\n" for i in range(30))
    )
    return version, log


def test_autonomy_command_enforces_the_l3_rule(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(admin, capsys, "autonomy", "--app", "demo")[1] == {"autonomy": "L1"}
    assert run(admin, capsys, "autonomy", "--app", "demo", "--level", "L2")[1] == {
        "autonomy": "L2"
    }
    assert admin.main(["autonomy", "--app", "demo", "--level", "L3"]) == 2
    assert "L3 needs" in capsys.readouterr().err
    code, out = run(
        admin,
        capsys,
        "autonomy",
        "--app",
        "demo",
        "--level",
        "L3",
        "--force",
        "--reason",
        "drill",
    )
    assert (code, out) == (0, {"autonomy": "L3"})


def test_explain_simulate_and_diff(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    version, log = _trained_with_log(env, capsys)
    request = json.dumps(
        {"app": "demo", "device_id": "d", "recent": ["i00001", "i00002"], "limit": 4}
    )
    _, explained = run(admin, capsys, "explain", "--app", "demo", "--request", request)
    assert explained["version"] == version and len(explained["items"]) == 4
    assert {"group", "content", "trend"} <= set(explained["items"][0])

    _, simulated = run(
        admin,
        capsys,
        "simulate",
        "--app",
        "demo",
        "--served",
        str(log),
        "--limit",
        "10",
    )
    assert (
        simulated["n"] == 10 and simulated["identical_share"] == 1.0
    )  # same model, same seed

    _, same = run(
        admin,
        capsys,
        "diff",
        "--app",
        "demo",
        "--served",
        str(log),
        "--a",
        version,
        "--b",
        version,
    )
    assert same["mean_jaccard@10"] == 1.0


def test_validate_config_and_schema(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = env / "good.json"
    good.write_text(json.dumps({"eps": 0.1}))
    code, out = run(admin, capsys, "validate-config", "--file", str(good))
    assert code == 0 and len(out["config_hash"]) == 12
    bad = env / "bad.json"
    bad.write_text(json.dumps({"epsilon": 0.1}))
    assert admin.main(["validate-config", "--file", str(bad)]) == 2
    assert "unknown config keys" in capsys.readouterr().err
    _, schema = run(admin, capsys, "schema")
    assert schema["title"] == "EngineConfig"


def test_reconcile_needs_a_configured_mlflow(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin.main(["reconcile-mlflow", "--app", "demo"]) == 2
    assert "MLFLOW_TRACKING_URI" in capsys.readouterr().err


def test_rec_loop_commands(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, log = _trained_with_log(env, capsys)
    interactions = env / "interactions.jsonl"
    interactions.write_text("")
    args = ["--app", "demo", "--served", str(log), "--interactions", str(interactions)]
    code, check = run(loop, capsys, "drift-check", *args)
    assert code == 0 and check["action"] in {"none", "alert"}
    assert "signals" in check and check["retrain"] is None
    code, step = run(loop, capsys, "rollout-step", *args)
    assert (code, step["rollout_id"], step["stage"]) == (0, None, None)


def test_rec_loop_health_check_skips_srm_on_thin_traffic(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = _trained_with_log(env, capsys)
    code, out = run(loop, capsys, "health-check", "--app", "demo", "--served", str(log))
    assert code == 0 and out["healthy"] is True
    assert out["srm_pvalue"] is None and out["skew"] is None
    assert set(out["expected_share"]) == {"holdout", "treatment", "control"}


def test_rec_loop_health_check_alarms_on_skewed_population(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = _trained_with_log(env, capsys)
    attr = env / "users.json"
    attr.write_text(json.dumps({"country_group": {"JP": 1.0}}))
    code, out = run(
        loop,
        capsys,
        *("health-check", "--app", "demo", "--served", str(log)),
        *("--user-attr", str(attr)),
    )
    assert code == 1 and out["healthy"] is False
    assert out["alarms"][0].startswith("skew: country_group")


def test_rec_admin_ips_replays_logged_requests(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, log = _trained_with_log(env, capsys)
    interactions = env / "interactions.jsonl"
    interactions.write_text("")
    code, out = run(
        admin,
        capsys,
        *("ips", "--app", "demo", "--served", str(log)),
        *("--interactions", str(interactions)),
    )
    assert code == 0
    assert out["n_requests"] > 0 and out["ips"] == 0.0


def test_rec_loop_refuses_an_app_without_a_model(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (env / "s.jsonl").write_text("")
    code = loop.main(
        [
            "drift-check",
            "--app",
            "ghost",
            "--served",
            str(env / "s.jsonl"),
            "--interactions",
            str(env / "s.jsonl"),
        ]
    )
    assert code == 2
    assert "no active version" in capsys.readouterr().err
