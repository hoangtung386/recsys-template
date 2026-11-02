"""Readers for the JSON-lines logs: ``rec.served`` and ``interactions``.

Interaction records look like
``{"ts", "device_id", "request_id", "item_id", "event"}`` with
``event`` in ``click``, ``start``, ``complete``. Timestamps are ISO-8601.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are taken as UTC."""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the JSON objects of a file, skipping (and counting) corrupt lines."""
    bad = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(record, dict):
                yield record
    if bad:
        log.warning("jsonl.corrupt_lines", extra={"path": str(path), "count": bad})


def load_since(path: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """Load records with ``ts >= since`` (all when ``since`` is ``None``)."""
    rows = read_jsonl(path)
    if since is None:
        return list(rows)
    return [r for r in rows if "ts" in r and parse_ts(r["ts"]) >= since]


def clicks_by_request(interactions: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Map each request id to the items clicked within it."""
    clicks: dict[str, set[str]] = defaultdict(set)
    for record in interactions:
        if record.get("event") == "click" and record.get("request_id"):
            clicks[record["request_id"]].add(str(record.get("item_id", "")))
    return clicks
