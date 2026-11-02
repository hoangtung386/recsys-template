"""Offline inspection tools behind ``rec-admin``: explain, simulate, diff, schema."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from recsys.engine import (
    EngineConfig,
    context_from_request,
    load_bundle,
    parse_request,
    recommend,
)
from recsys.engine.bundle import Bundle
from recsys.engine.context import context_from_served
from recsys.engine.explain import explain
from recsys.monitoring.served_log import clicks_by_request, read_jsonl
from recsys.registry import RegistryError, SqliteRegistry
from recsys.settings import get_settings
from recsys.training.ips import ips_estimate

TOP_K = 10


def _load(reg: SqliteRegistry, app: str, version: str | None) -> Bundle:
    """Load ``version`` (default: the active one) of ``app``."""
    chosen = version or reg.active_version(app)
    if chosen is None:
        raise RegistryError(f"{app} has no active version; pass --version")
    path = get_settings().artifact_dir / app / chosen
    if not path.is_dir():
        raise RegistryError(f"no artifact at {path}")
    return load_bundle(path, verify=False)


def _rows(path: Path, limit: int) -> list[dict[str, Any]]:
    rows = [r for r in read_jsonl(path) if "recent" in r]
    return rows[:limit]


def _jaccard(a: list[str], b: list[str]) -> float:
    union = set(a) | set(b)
    return len(set(a) & set(b)) / len(union) if union else 1.0


def explain_request(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    """Show how each signal contributed to every item of one request."""
    bundle = _load(reg, args.app, args.version)
    raw = args.request
    body = Path(raw[1:]).read_text() if raw.startswith("@") else raw
    request = parse_request(body)
    blocked = reg.serving_state(args.app).blocked
    return explain(bundle, context_from_request(bundle, request, blocked), eps=args.eps)


def simulate(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    """Replay logged requests on a version and compare with what was served."""
    bundle = _load(reg, args.app, args.version)
    out = []
    for row in _rows(args.served, args.limit):
        replay = recommend(bundle, context_from_served(bundle, row))
        logged = [i["id"] for i in row.get("items", ())]
        replayed = [i.id for i in replay.items]
        out.append(
            {
                "request_id": row.get("request_id"),
                "logged_version": row.get("model_version"),
                "identical": logged == replayed,
                "jaccard": round(_jaccard(logged, replayed), 4),
                "replay": replayed[:TOP_K],
            }
        )
    return {
        "version": bundle.version,
        "n": len(out),
        "identical_share": float(np.mean([o["identical"] for o in out]))
        if out
        else None,
        "requests": out,
    }


def ips_evaluate(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    """Estimate offline how a version would have done on logged traffic.

    Replays the logged requests on ``--version`` without exploration and weighs
    the logged clicks by their propensities (IPS and SNIPS). Only meaningful on
    logs written with ``eps > 0``; with a deterministic log, shared slots all
    have propensity 1 and this is plain replay accuracy.
    """
    bundle = _load(reg, args.app, args.version)
    rows = _rows(args.served, args.limit)
    target = {
        r["request_id"]: [
            i.id
            for i in recommend(bundle, context_from_served(bundle, r), eps=0.0).items
        ][:TOP_K]
        for r in rows
        if r.get("request_id")
    }
    clicks = clicks_by_request(read_jsonl(args.interactions))
    result = ips_estimate(rows, target, clicks)
    return {"version": bundle.version, **asdict(result)}


def diff_versions(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    """Compare the top-k of two versions over logged requests."""
    first = _load(reg, args.app, args.a)
    second = _load(reg, args.app, args.b)
    scores = []
    for row in _rows(args.served, args.limit):
        top = [
            [i.id for i in recommend(b, context_from_served(b, row), eps=0.0).items][
                : args.k
            ]
            for b in (first, second)
        ]
        scores.append(_jaccard(*top))
    return {
        "a": first.version,
        "b": second.version,
        "n": len(scores),
        f"mean_jaccard@{args.k}": float(np.mean(scores)) if scores else None,
        "min_jaccard": float(np.min(scores)) if scores else None,
    }


def validate_config(_reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    """Validate an engine configuration file and print its hash."""
    try:
        config = EngineConfig.from_dict(json.loads(Path(args.file).read_text()))
    except (ValueError, TypeError, OSError) as exc:
        raise RegistryError(f"invalid config: {exc}") from exc
    return {"valid": True, "config_hash": config.config_hash()}


def print_schema(_reg: SqliteRegistry, _args: argparse.Namespace) -> Any:
    """Return the JSON Schema of the engine configuration."""
    return EngineConfig.json_schema()
