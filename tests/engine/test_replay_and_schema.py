from __future__ import annotations

import json
from pathlib import Path

import pytest

from recsys.engine import (
    Arm,
    Bundle,
    EngineConfig,
    build_context,
    context_from_request,
    parse_request,
    recommend,
    served_message,
)
from recsys.engine.context import context_from_served
from recsys.engine.explain import explain

SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "engine-config.schema.json"


def test_a_logged_request_replays_to_the_same_slate(random_bundle: Bundle) -> None:
    req = parse_request(
        json.dumps(
            {
                "app": "demo",
                "device_id": "d",
                "recent": ["i00001", "i00002"],
                "skip": ["i00010"],
                "onb": ["cat1"],
                "lang": "en",
                "limit": 12,
                "request_id": "r1",
            }
        )
    )
    served = recommend(random_bundle, context_from_request(random_bundle, req))
    row = served_message(
        req,
        arm=Arm.TREATMENT,
        model_version="v1",
        config_hash="h",
        experiment_id="e",
        tier=served.tier,
        reason=None,
        items=served.items,
        degraded=None,
        latency_ms=1.0,
    )
    assert row["recent"] == ["i00001", "i00002"]
    replay = recommend(random_bundle, context_from_served(random_bundle, row))
    assert replay.items == served.items


def test_explain_reports_each_signal(random_bundle: Bundle) -> None:
    ctx = build_context(random_bundle, recent=["i00001", "i00002"], limit=5)
    out = explain(random_bundle, ctx)
    assert out["tier"] == "lukewarm" and len(out["items"]) == 5
    for item in out["items"]:
        total = item["group"] + item["content"] + item["trend"]
        assert total == pytest.approx(item["score"], abs=0.01)


def test_committed_schema_matches_the_code() -> None:
    assert json.loads(SCHEMA.read_text()) == json.loads(
        json.dumps(EngineConfig.json_schema())
    ), "run `make schemas`"


def test_schema_describes_every_field_and_defaults_are_valid() -> None:
    schema = EngineConfig.json_schema()
    assert set(schema["properties"]) == set(EngineConfig().to_dict())
    for name, value in EngineConfig().to_dict().items():
        spec = schema["properties"][name]
        if isinstance(value, dict):
            assert set(value) == set(spec["properties"])
        elif "maximum" in spec:
            assert spec.get("minimum", 0) <= value <= spec["maximum"]
