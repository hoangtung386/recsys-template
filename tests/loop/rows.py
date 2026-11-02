"""Builders of realistic ``rec.served`` and interaction rows for the loop tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from recsys.engine import (
    Arm,
    Bundle,
    Reason,
    context_from_request,
    parse_request,
    recommend,
    served_message,
)


def served_row(
    bundle: Bundle,
    *,
    n: int,
    when: datetime,
    arm: Arm = Arm.TREATMENT,
    country: str = "US",
    n_recent: int = 3,
    latency_ms: float = 2.0,
    reason: Reason | None = None,
    empty: bool = False,
) -> dict[str, Any]:
    """One served record produced by the real engine."""
    recent = [f"i{(n * 3 + j) % 500:05d}" for j in range(n_recent)]
    body = {
        "app": "demo",
        "device_id": f"dev{n}",
        "recent": recent,
        "lang": "en",
        "request_id": f"{arm.value}-{bundle.version}-{n}",
        "limit": 10,
    }
    req = parse_request(json.dumps(body), country=country)
    result = recommend(bundle, context_from_request(bundle, req))
    row = served_message(
        req,
        arm=arm,
        model_version=bundle.version,
        config_hash=bundle.config_hash,
        experiment_id="default",
        tier=result.tier,
        reason=reason,
        items=() if empty else result.items,
        degraded=None,
        latency_ms=latency_ms,
    )
    row["ts"] = when.isoformat()
    return row


def click(row: dict[str, Any], when: datetime) -> dict[str, Any]:
    """A click on the first item of ``row``."""
    return {
        "ts": (when + timedelta(seconds=5)).isoformat(),
        "device_id": row["device_id"],
        "request_id": row["request_id"],
        "item_id": row["items"][0]["id"],
        "event": "click",
    }
