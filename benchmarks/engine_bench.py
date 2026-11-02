"""Latency benchmark of the engine on a realistic catalog (CI regression guard).

Usage:
    uv run python benchmarks/engine_bench.py --max-p95-ms 5 --out bench.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from recsys.engine import build_context, load_bundle, recommend, write_artifact
from recsys.training.synthetic import random_payload

LIMITS = (20, 100, 200)
WARMUP = 50


def measure(n_items: int, n_requests: int) -> dict[str, dict[str, float]]:
    """Return p50/p95 latency in ms per slate size."""
    with tempfile.TemporaryDirectory() as tmp:
        path = write_artifact(Path(tmp), "bench", random_payload(n_items))
        bundle = load_bundle(path)
        results: dict[str, dict[str, float]] = {}
        for seed in range(WARMUP):  # fault the memory-mapped pages in
            recommend(bundle, build_context(bundle, recent=["i00001"], seed=seed))
        for limit in LIMITS:
            samples = []
            for seed in range(n_requests):
                recent = [f"i{(seed * 7 + j) % n_items:05d}" for j in range(10)]
                ctx = build_context(bundle, recent=recent, limit=limit, seed=seed)
                started = time.perf_counter()
                recommend(bundle, ctx)
                samples.append((time.perf_counter() - started) * 1000)
            results[str(limit)] = {
                "p50_ms": float(np.percentile(samples, 50)),
                "p95_ms": float(np.percentile(samples, 95)),
            }
    return results


def main() -> int:
    """Run the benchmark; exit 1 when any p95 exceeds ``--max-p95-ms``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=8308)
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--max-p95-ms", type=float, default=5.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    results = measure(args.items, args.requests)
    for limit, stats in results.items():
        p50, p95 = stats["p50_ms"], stats["p95_ms"]
        print(f"limit={limit:>3}  p50={p50:.2f} ms  p95={p95:.2f} ms")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2))
    worst = max(stats["p95_ms"] for stats in results.values())
    if worst > args.max_p95_ms:
        print(f"FAIL: p95 {worst:.2f} ms > {args.max_p95_ms} ms", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
