"""The v1 request contract and the ``rec.served`` log record.

Validation is hand written on purpose: the v1 contract never answers 4xx. A bad
request becomes ``200`` with ``reason=bad_request`` so clients keep one code path.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from recsys.engine.errors import RequestError
from recsys.engine.result import RecItem
from recsys.engine.schema import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_ONBOARDING,
    MAX_RECENT,
    MAX_SKIP,
    Arm,
    Degraded,
    Reason,
    Tier,
)

_ID_PATTERN = re.compile(r"^[\w.:\-]{1,64}$")
_LANG_PATTERN = re.compile(r"^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})?$")
_MAX_DEVICE_ID = 128


@dataclass(frozen=True, slots=True)
class RecRequest:
    """A validated request. Collections are tuples, newest ``recent`` first."""

    app: str
    device_id: str
    recent: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    onb: tuple[str, ...] = ()
    lang: str | None = None
    limit: int = DEFAULT_LIMIT
    surface: str = "home"
    request_id: str = ""
    country: str | None = None
    seed: int | None = None

    def derived_seed(self) -> int:
        """Return ``seed`` or a stable seed derived from the request identity."""
        if self.seed is not None:
            return self.seed
        digest = hashlib.sha256(
            f"{self.app}|{self.device_id}|{self.request_id}".encode()
        ).digest()
        return int.from_bytes(digest[:8], "big")


def _string_list(data: dict[str, Any], key: str, max_len: int) -> tuple[str, ...]:
    value = data.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RequestError(key, "must be a list of strings")
    if len(value) > max_len:
        raise RequestError(key, f"at most {max_len} entries")
    for entry in value:
        if not isinstance(entry, str) or not _ID_PATTERN.match(entry):
            raise RequestError(key, "entries must be short id strings")
    return tuple(value)


def _optional_str(
    data: dict[str, Any], key: str, pattern: re.Pattern[str]
) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise RequestError(key, "invalid format")
    return value


def parse_request(
    body: bytes | str,
    *,
    country: str | None = None,
) -> RecRequest:
    """Parse and validate a request body.

    Unknown fields are ignored so clients can roll out new fields first.

    Args:
        body: Raw JSON body.
        country: Country code supplied by the edge (header), not by the client.

    Raises:
        RequestError: If the body violates the contract.
    """
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RequestError("body", "not valid JSON") from exc
    if not isinstance(data, dict):
        raise RequestError("body", "must be a JSON object")

    app = data.get("app")
    if not isinstance(app, str) or not _ID_PATTERN.match(app):
        raise RequestError("app", "required id string")
    device_id = data.get("device_id")
    if not isinstance(device_id, str) or not 0 < len(device_id) <= _MAX_DEVICE_ID:
        raise RequestError("device_id", "required string up to 128 chars")

    limit = data.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise RequestError("limit", "must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise RequestError("limit", f"must be in [1, {MAX_LIMIT}]")

    surface = data.get("surface", "home")
    if not isinstance(surface, str) or not _ID_PATTERN.match(surface):
        raise RequestError("surface", "invalid format")

    request_id = data.get("request_id") or uuid.uuid4().hex
    if not isinstance(request_id, str) or not _ID_PATTERN.match(request_id):
        raise RequestError("request_id", "invalid format")

    seed = data.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise RequestError("seed", "must be an integer")

    return RecRequest(
        app=app,
        device_id=device_id,
        recent=_string_list(data, "recent", MAX_RECENT),
        skip=_string_list(data, "skip", MAX_SKIP),
        onb=_string_list(data, "onb", MAX_ONBOARDING),
        lang=_optional_str(data, "lang", _LANG_PATTERN),
        limit=limit,
        surface=surface,
        request_id=request_id,
        country=country.upper() if country and len(country) == 2 else None,
        seed=seed,
    )


def served_message(
    req: RecRequest,
    *,
    arm: Arm | None,
    model_version: str | None,
    config_hash: str | None,
    experiment_id: str | None,
    tier: Tier | None,
    reason: Reason | None,
    items: Sequence[RecItem],
    degraded: Degraded | None,
    latency_ms: float,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``rec.served`` record. Emitted for *every* request.

    Logging control, holdout and failed requests too is what makes the
    intention-to-treat analysis possible. The record never contains an IP.
    """
    return {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "request_id": req.request_id,
        "trace_id": trace_id,
        "app_id": req.app,
        "device_id": req.device_id,
        "surface": req.surface,
        "country": req.country,
        "lang": req.lang,
        "n_recent": len(req.recent),
        "limit": req.limit,
        "experiment_id": experiment_id,
        "arm": arm.value if arm else None,
        "model_version": model_version,
        "config_hash": config_hash,
        "tier": tier.value if tier else None,
        "reason": reason.value if reason else None,
        "degraded": degraded.value if degraded else None,
        "recent": list(req.recent),
        "skip": list(req.skip),
        "onb": list(req.onb),
        "seed": req.derived_seed(),
        "latency_ms": round(latency_ms, 3),
        "items": [
            {
                "id": item.id,
                "src": item.src.value,
                "score": round(item.score, 5),
                "propensity": round(item.propensity, 6),
            }
            for item in items
        ],
    }
