from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import recsys.serving.service as service_module
from recsys.engine import Experiment, recommend
from recsys.serving.app import create_app
from recsys.serving.service import RecService
from tests.serving.conftest import APP, Harness, make_settings, rec_body


def post(client: TestClient, body: object, **headers: str):  # type: ignore[no-untyped-def]
    payload = (
        body if isinstance(body, bytes) else __import__("json").dumps(body).encode()
    )
    return client.post("/v1/rec", content=payload, headers=headers)


def test_happy_path(client: TestClient, harness: Harness) -> None:
    response = post(client, rec_body(), **{"x-country": "vn", "x-trace-id": "trace-1"})
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 5
    assert data["arm"] == "treatment" and data["model_version"] == "v1"
    assert "reason" not in data
    (logged,) = harness.sink.messages
    assert logged["trace_id"] == "trace-1" and logged["country"] == "VN"
    assert logged["items"][0]["id"] == data["items"][0]["id"]
    assert logged["request_id"] == data["request_id"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"not json", "bad_request"),
        (rec_body(limit=0), "bad_request"),
        (rec_body(app="other-app"), "no_artifact"),
    ],
)
def test_failures_are_200_with_a_reason_and_are_logged(
    client: TestClient, harness: Harness, body: object, reason: str
) -> None:
    response = post(client, body)
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["reason"] == reason
    assert harness.sink.messages[-1]["reason"] == reason


def test_kill_switch_takes_effect_after_refresh(
    client: TestClient, harness: Harness
) -> None:
    harness.registry.set_kill(APP, True, actor="test", reason="drill")
    client.app.state.registries[APP].refresh_once()  # type: ignore[attr-defined]
    assert post(client, rec_body()).json()["reason"] == "kill_switch"
    harness.registry.set_kill(APP, False, actor="test")
    client.app.state.registries[APP].refresh_once()  # type: ignore[attr-defined]
    assert "reason" not in post(client, rec_body()).json()


def test_blocklist_is_enforced(client: TestClient, harness: Harness) -> None:
    first = post(client, rec_body(recent=[], limit=3)).json()["items"][0]["id"]
    harness.registry.block_items(APP, [first], actor="test")
    client.app.state.registries[APP].refresh_once()  # type: ignore[attr-defined]
    ids = [i["id"] for i in post(client, rec_body(recent=[], limit=50)).json()["items"]]
    assert first not in ids


def test_holdout_and_control_return_nothing_but_are_logged(harness: Harness) -> None:
    harness.publish("v1")
    harness.registry.set_experiment(
        APP, Experiment(treatment_pct=0, holdout_pct=0), actor="t"
    )
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        data = post(c, rec_body()).json()
    assert (
        data["reason"] == "control" and data["items"] == [] and data["arm"] == "control"
    )
    assert harness.sink.messages[-1]["arm"] == "control"

    harness.registry.set_experiment(APP, Experiment(holdout_pct=100), actor="t")
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        assert post(c, rec_body()).json()["reason"] == "holdout"


def test_shadow_computes_and_logs_but_returns_nothing(harness: Harness) -> None:
    harness.publish("v1")
    harness.publish("v2", promote=False)
    harness.registry.set_experiment(
        APP,
        Experiment(
            treatment_pct=100, holdout_pct=0, shadow_version="v2", shadow_pct=100
        ),
        actor="t",
    )
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        data = post(c, rec_body()).json()
    assert data["items"] == [] and data["reason"] == "shadow"
    logged = harness.sink.messages[-1]
    assert logged["arm"] == "shadow" and logged["model_version"] == "v2"
    assert len(logged["items"]) == 5


def test_canary_serves_the_candidate_version(harness: Harness) -> None:
    harness.publish("v1")
    harness.publish("v2", promote=False)
    harness.registry.set_experiment(
        APP,
        Experiment(
            treatment_pct=100, holdout_pct=0, canary_version="v2", canary_pct=100
        ),
        actor="t",
    )
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        assert post(c, rec_body()).json()["model_version"] == "v2"


def test_unloadable_canary_falls_back_to_active(harness: Harness) -> None:
    harness.publish("v1")
    harness.registry.set_experiment(
        APP,
        Experiment(
            treatment_pct=100, holdout_pct=0, canary_version="ghost", canary_pct=100
        ),
        actor="t",
    )
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        # refresh fails on the missing canary, so the app is not ready ...
        assert c.get("/readyz").status_code == 503
        assert post(c, rec_body()).json()["reason"] == "no_artifact"


def test_overload_answers_immediately_without_queueing(
    client: TestClient, harness: Harness
) -> None:
    service: RecService = client.app.state.service  # type: ignore[attr-defined]
    taken = 0
    while service.try_acquire():
        taken += 1
    try:
        started = time.perf_counter()
        data = post(client, rec_body()).json()
        assert time.perf_counter() - started < 0.5
    finally:
        for _ in range(taken):
            service.release()
    assert data["reason"] == "overload" and data["items"] == []
    assert harness.sink.messages[-1]["reason"] == "overload"
    assert "reason" not in post(client, rec_body()).json()  # slots were returned


def test_timeout_answers_once_and_logs_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.publish("v1")
    harness.registry.set_experiment(
        APP, Experiment(treatment_pct=100, holdout_pct=0), actor="t"
    )
    original = recommend

    def slow(*args, **kwargs):  # type: ignore[no-untyped-def]
        time.sleep(0.25)
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "recommend", slow)
    settings = make_settings(harness.root, hard_timeout_s=0.05)
    with TestClient(create_app(settings, sink=harness.sink)) as c:
        data = post(c, rec_body()).json()
        assert data["reason"] == "timeout" and data["items"] == []
        time.sleep(0.4)  # let the worker finish in the background
        service: RecService = c.app.state.service  # type: ignore[attr-defined]
        assert service.try_acquire()  # the slot came back
        service.release()
    assert [m["reason"] for m in harness.sink.messages] == ["timeout"]


def test_unexpected_engine_error_becomes_reason_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.publish("v1")
    harness.registry.set_experiment(
        APP, Experiment(treatment_pct=100, holdout_pct=0), actor="t"
    )

    def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("bug")

    monkeypatch.setattr(service_module, "recommend", boom)
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        response = post(c, rec_body())
    assert response.status_code == 200 and response.json()["reason"] == "error"
    assert harness.sink.messages[-1]["reason"] == "error"


def test_readyz_follows_the_artifact(tmp_path: Path, harness: Harness) -> None:
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        assert c.get("/healthz").json() == {"ok": True}
        assert c.get("/readyz").status_code == 503
        harness.publish("v1")
        c.app.state.registries[APP].refresh_once()  # type: ignore[attr-defined]
        ready = c.get("/readyz")
        assert ready.status_code == 200 and ready.json()["versions"] == {APP: "v1"}


def test_promotion_is_picked_up_without_restart(
    client: TestClient, harness: Harness
) -> None:
    harness.publish("v2")
    assert post(client, rec_body()).json()["model_version"] == "v1"
    client.app.state.registries[APP].refresh_once()  # type: ignore[attr-defined]
    assert post(client, rec_body()).json()["model_version"] == "v2"


def test_failed_refresh_keeps_serving_the_old_version(
    client: TestClient, harness: Harness
) -> None:
    harness.publish("v2")
    (harness.root / APP / "v2" / "_SUCCESS").unlink()  # corrupt the new version
    registry = client.app.state.registries[APP]  # type: ignore[attr-defined]
    assert registry.refresh_once() is False
    assert post(client, rec_body()).json()["model_version"] == "v1"
    assert b"rec_registry_load_failures_total" in client.get("/metrics").content


def test_metrics_endpoint_reports_the_contract_metrics(client: TestClient) -> None:
    post(client, rec_body())
    post(client, b"nope")
    text = client.get("/metrics").text
    for name in (
        "rec_requests_total",
        "rec_latency_seconds_bucket",
        "rec_engine_seconds_bucket",
        "rec_inflight",
        "rec_request_invalid_total",
        "rec_registry_version_info",
        "rec_registry_age_seconds",
        "rec_empty_total",
        "rec_unknown_item_ratio",
    ):
        assert name in text, name
    assert 'model_version="v1"' in text


def test_metrics_can_be_disabled(harness: Harness) -> None:
    harness.publish("v1")
    app = create_app(
        make_settings(harness.root, metrics_enabled=False), sink=harness.sink
    )
    with TestClient(app) as c:
        assert c.get("/metrics").status_code == 404


def test_concurrent_requests_never_exceed_the_slot_limit(client: TestClient) -> None:
    results: list[str] = []
    lock = threading.Lock()

    def call() -> None:
        reason = post(client, rec_body()).json().get("reason", "ok")
        with lock:
            results.append(reason)

    threads = [threading.Thread(target=call) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) <= {"ok", "overload"}
    assert "ok" in results


def test_openapi_documents_the_response(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "RecResponse" in schema["components"]["schemas"]


@pytest.fixture(autouse=True)
def _quiet_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.CRITICAL, logger="recsys.serving")


def test_content_health_metrics_follow_the_traffic(client: TestClient) -> None:
    for i in range(30):
        post(client, rec_body(device_id=f"d{i}", recent=[f"i{i:05d}"]))
    text = client.get("/metrics").text
    for name in (
        "rec_served_item_gini",
        "rec_top1pct_exposure_share",
        "rec_catalog_coverage",
        'rec_src_share{app="demo",src=',
        'rec_tier_share{app="demo",tier="lukewarm"}',
    ):
        assert name in text, name
    coverage = float(
        next(
            line
            for line in text.splitlines()
            if line.startswith("rec_catalog_coverage{")
        ).split()[-1]
    )
    assert 0.0 < coverage <= 1.0


def test_engine_error_is_attributed_to_its_arm_and_version(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.publish("v1")
    harness.registry.set_experiment(
        APP, Experiment(treatment_pct=100, holdout_pct=0), actor="t"
    )

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(service_module, "recommend", boom)
    with TestClient(create_app(make_settings(harness.root), sink=harness.sink)) as c:
        post(c, rec_body())
    logged = harness.sink.messages[-1]
    assert (logged["reason"], logged["arm"], logged["model_version"]) == (
        "error",
        "treatment",
        "v1",
    )
