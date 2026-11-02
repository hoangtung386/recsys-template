"""``rec-train``: train, gate, publish and register one model version."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from recsys.integrations.wiring import attach_mirror, build_mirror
from recsys.logging_config import bind_context, configure_logging
from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.registry import SqliteRegistry
from recsys.settings import get_settings
from recsys.training.job import TrainParams, run_training
from recsys.training.synthetic import SyntheticSource

EXIT_REJECTED = 3


def main(argv: Sequence[str] | None = None) -> int:
    """Run one training job; exit 3 if the gate rejected the model."""
    parser = argparse.ArgumentParser(prog="rec-train", description=main.__doc__)
    parser.add_argument("--app", required=True, help="app slug")
    parser.add_argument(
        "--revision",
        type=int,
        help="force a new version for an already-trained data day",
    )
    parser.add_argument("--test-days", type=int, default=7)
    parser.add_argument("--k", type=int, default=64, help="latent dimension")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--synthetic-items", type=int, default=400)
    parser.add_argument("--synthetic-users", type=int, default=1500)
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    bind_context(app=args.app)
    registry = SqliteRegistry(settings.registry_db, settings.artifact_dir)
    mirror = build_mirror(settings.mlflow_tracking_uri)
    if mirror:
        attach_mirror(registry, mirror)
    outcome = run_training(
        SyntheticSource(
            n_items=args.synthetic_items, n_users=args.synthetic_users, seed=args.seed
        ),
        registry,
        TrainParams(
            app=args.app,
            artifacts_root=settings.artifact_dir,
            revision=args.revision,
            test_days=args.test_days,
            k=args.k,
            seed=args.seed,
            tracking_uri=settings.mlflow_tracking_uri,
            lineage={
                "code_commit": settings.code_commit or "unknown",
                "image_digest": settings.image_digest,
            },
        ),
        mirror=mirror,
    )
    if outcome.status != "exists":
        batch = BatchMetrics()
        batch.record_training(
            args.app,
            passed=outcome.status == "validated",
            ndcg=outcome.metrics.get("ndcg@10", 0.0),
        )
        batch.push(settings.pushgateway_url, args.app, job="rec-train")
    json.dump(
        {
            "status": outcome.status,
            "version": outcome.version,
            "artifact": str(outcome.artifact_path),
            "metrics": dict(outcome.metrics),
            "gate": outcome.gate.to_dict() if outcome.gate else None,
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return EXIT_REJECTED if outcome.status == "rejected" else 0
