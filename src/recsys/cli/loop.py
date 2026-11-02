"""``rec-loop``: the periodic jobs of the closed loop."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recsys.integrations.wiring import attach_mirror, build_mirror
from recsys.logging_config import configure_logging
from recsys.loop.health import DEFAULT_WINDOW_HOURS as HEALTH_WINDOW_HOURS
from recsys.loop.health import run_health_check
from recsys.loop.runner import DEFAULT_WINDOW_HOURS, run_drift_check, run_rollout_step
from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.registry import RegistryError, SqliteRegistry
from recsys.settings import get_settings
from recsys.training.job import TrainOutcome, TrainParams, run_training
from recsys.training.synthetic import SyntheticSource


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app", required=True)
    parser.add_argument("--served", type=Path, required=True, help="rec.served JSONL")
    parser.add_argument(
        "--interactions", type=Path, required=True, help="interactions JSONL"
    )
    parser.add_argument("--now", help="ISO time to evaluate at (default: now)")


def _now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _dump(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run a loop job. Exit 1 when ``health-check`` alarms, 2 when refused."""
    parser = argparse.ArgumentParser(prog="rec-loop", description=main.__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    drift = sub.add_parser("drift-check", help="measure drift, decide, maybe retrain")
    _common(drift)
    drift.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    drift.add_argument(
        "--dq-failed", action="store_true", help="a blocking DQ check failed"
    )
    drift.add_argument(
        "--execute", action="store_true", help="really retrain on 'retrain'"
    )
    drift.add_argument("--synthetic-items", type=int, default=400)
    drift.add_argument("--synthetic-users", type=int, default=1500)
    step = sub.add_parser("rollout-step", help="judge the release in progress")
    _common(step)
    health = sub.add_parser(
        "health-check", help="SRM of the A/B split and population skew"
    )
    health.add_argument("--app", required=True)
    health.add_argument("--served", type=Path, required=True, help="rec.served JSONL")
    health.add_argument("--now", help="ISO time to evaluate at (default: now)")
    health.add_argument("--window-hours", type=float, default=HEALTH_WINDOW_HOURS)
    health.add_argument(
        "--user-attr",
        type=Path,
        help="JSON of user-table distributions (country_group, lang, tier)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    registry = SqliteRegistry(settings.registry_db, settings.artifact_dir)
    mirror = build_mirror(settings.mlflow_tracking_uri)
    if mirror:
        attach_mirror(registry, mirror)
    metrics = BatchMetrics()
    now = _now(args.now)
    exit_code = 0
    try:
        if args.command == "health-check":
            found = run_health_check(
                registry,
                args.app,
                served_path=args.served,
                now=now,
                window_hours=args.window_hours,
                user_attr_path=args.user_attr,
                metrics=metrics,
            )
            _dump(
                {
                    "healthy": found.healthy,
                    "alarms": list(found.alarms),
                    "arm_devices": found.arm_devices,
                    "expected_share": found.expected_share,
                    "srm_pvalue": found.srm_pvalue,
                    "skew": found.skew.scores if found.skew else None,
                }
            )
            exit_code = 0 if found.healthy else 1
        elif args.command == "drift-check":
            retrain = None
            if args.execute:

                def retrain(revision: int) -> TrainOutcome:
                    return run_training(
                        SyntheticSource(
                            n_items=args.synthetic_items, n_users=args.synthetic_users
                        ),
                        registry,
                        TrainParams(
                            app=args.app,
                            artifacts_root=settings.artifact_dir,
                            revision=revision,
                            tracking_uri=settings.mlflow_tracking_uri,
                        ),
                        mirror=mirror,
                    )

            check = run_drift_check(
                registry,
                args.app,
                served_path=args.served,
                interactions_path=args.interactions,
                now=now,
                window_hours=args.window_hours,
                dq_blocking_ok=not args.dq_failed,
                retrain=retrain,
                metrics=metrics,
            )
            _dump(
                {
                    "action": check.decision.action.value,
                    "reasons": list(check.decision.reasons),
                    "drifted": list(check.drifted),
                    "signals": asdict(check.signals),
                    "retrain": check.retrain.version if check.retrain else None,
                }
            )
        else:
            result = run_rollout_step(
                registry,
                args.app,
                served_path=args.served,
                interactions_path=args.interactions,
                now=now,
                metrics=metrics,
            )
            _dump(
                {
                    "rollout_id": result.rollout_id,
                    "stage": result.stage.value if result.stage else None,
                    "outcome": result.verdict.outcome.value if result.verdict else None,
                    "reasons": list(result.verdict.reasons) if result.verdict else [],
                }
            )
    except RegistryError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    metrics.push(settings.pushgateway_url, args.app, job="rec-loop")
    return exit_code
