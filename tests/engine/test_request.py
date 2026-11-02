from __future__ import annotations

import json
from typing import Any

import pytest

from recsys.engine import RequestError, parse_request, served_message
from recsys.engine.result import RecItem
from recsys.engine.schema import Arm, Reason, Source, Tier


def body(**overrides: Any) -> bytes:
    data = {"app": "demo", "device_id": "dev-1", "recent": ["a", "b"], **overrides}
    return json.dumps({k: v for k, v in data.items() if v is not ...}).encode()


def test_minimal_request_gets_defaults() -> None:
    req = parse_request(body(recent=...), country="us")
    assert req.limit == 20
    assert req.recent == ()
    assert req.country == "US"
    assert req.request_id  # generated


def test_unknown_fields_are_ignored() -> None:
    assert parse_request(body(future_field=1)).app == "demo"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"app": ...}, "app"),
        ({"device_id": ""}, "device_id"),
        ({"device_id": 5}, "device_id"),
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"limit": True}, "limit"),
        ({"recent": "abc"}, "recent"),
        ({"recent": [1]}, "recent"),
        ({"recent": [f"i{n}" for n in range(31)]}, "recent"),
        ({"lang": "english-language"}, "lang"),
        ({"seed": "x"}, "seed"),
    ],
)
def test_contract_violations_name_the_field(
    overrides: dict[str, Any], field: str
) -> None:
    with pytest.raises(RequestError) as caught:
        parse_request(body(**overrides))
    assert caught.value.field == field


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"\xff\xfe"])
def test_garbage_bodies(raw: bytes) -> None:
    with pytest.raises(RequestError):
        parse_request(raw)


def test_seed_is_stable_and_overridable() -> None:
    a = parse_request(body(request_id="r1"))
    assert a.derived_seed() == parse_request(body(request_id="r1")).derived_seed()
    assert a.derived_seed() != parse_request(body(request_id="r2")).derived_seed()
    assert parse_request(body(seed=42)).derived_seed() == 42


def test_served_message_has_no_ip_and_logs_every_field() -> None:
    req = parse_request(body(request_id="r1"), country="vn")
    msg = served_message(
        req,
        arm=Arm.TREATMENT,
        model_version="v1",
        config_hash="abc",
        experiment_id="default",
        tier=Tier.WARM,
        reason=None,
        items=[RecItem("x", 0.5, Source.GROUP, 0.96)],
        degraded=None,
        latency_ms=1.23456,
        trace_id="t1",
    )
    assert msg["items"] == [
        {"id": "x", "src": "rec_group", "score": 0.5, "propensity": 0.96}
    ]
    assert msg["arm"] == "treatment"
    assert msg["trace_id"] == "t1"
    assert not any(key == "ip" or key.endswith("_ip") for key in msg)
    failed = served_message(
        req,
        arm=None,
        model_version=None,
        config_hash=None,
        experiment_id=None,
        tier=None,
        reason=Reason.BAD_REQUEST,
        items=[],
        degraded=None,
        latency_ms=0.0,
    )
    assert failed["reason"] == "bad_request"
    assert failed["items"] == []
