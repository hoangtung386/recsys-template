"""``rec-admin``: operate the registry (promote, rollback, kill, releases, freeze)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from recsys.cli import _tools
from recsys.integrations.wiring import attach_mirror, build_mirror, reconcile
from recsys.monitoring.batch_metrics import BatchMetrics
from recsys.registry import RegistryError, RolloutStage, SqliteRegistry
from recsys.settings import get_settings

Handler = Callable[[SqliteRegistry, argparse.Namespace], Any]


@dataclass(frozen=True, slots=True)
class Result:
    """A command result with a non-default exit code."""

    data: Any
    code: int = 0


_END_RESULTS = {
    "promoted": RolloutStage.PROMOTED,
    "rolled_back": RolloutStage.ROLLED_BACK,
    "rejected": RolloutStage.REJECTED,
    "aborted": RolloutStage.ABORTED,
}


def _status(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    active = reg.get_active(args.app)
    return {
        "active": active.model_version if active else None,
        "run_id": active.run_id if active else None,
        "state": json.loads(reg.serving_state(args.app).to_json()),
        "loop": asdict(reg.loop_state(args.app)),
        "rollout": asdict(r) if (r := reg.active_rollout(args.app)) else None,
    }


def _versions(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    return [
        {
            "version": v.model_version,
            "status": v.status.value,
            "created_at": v.created_at,
        }
        for v in reg.list_versions(args.app)
    ]


def _promote(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    reg.set_active(args.app, args.version, actor=args.actor, reason=args.reason)
    return {"active": args.version}


def _rollback(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    return {"active": reg.rollback(args.app, actor=args.actor, reason=args.reason)}


def _kill(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    reg.set_kill(args.app, args.command == "kill", actor=args.actor, reason=args.reason)
    return {"kill": args.command == "kill"}


def _block(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    blocked = reg.block_items(
        args.app,
        args.items,
        actor=args.actor,
        unblock=args.command == "unblock",
        reason=args.reason,
    )
    return {"blocked": sorted(blocked)}


def _freeze(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    if args.command == "freeze":
        reg.freeze(args.app, actor=args.actor, reason=args.reason)
    else:
        reg.unfreeze(args.app, actor=args.actor, reason=args.reason)
    return asdict(reg.loop_state(args.app))


def _audit(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    return [asdict(entry) for entry in reg.audit_log(args.app, args.limit)]


def _experiment(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    if args.action == "shadow":
        rollout = reg.start_rollout(
            args.app,
            args.version,
            actor=args.actor,
            autonomy=args.autonomy,
            shadow_pct=args.pct,
        )
    elif args.action == "canary":
        rollout = reg.active_rollout(args.app) or reg.start_rollout(
            args.app,
            args.version,
            actor=args.actor,
            autonomy=args.autonomy,
            shadow_pct=0.0,
        )
        rollout = reg.advance_to_canary(
            rollout.rollout_id, pct=args.pct, actor=args.actor
        )
    else:
        rollout = reg.end_rollout(
            args.rollout,
            _END_RESULTS[args.result],
            actor=args.actor,
            reason=args.reason,
        )
    return asdict(rollout)


def _autonomy(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    if args.level:
        reg.set_autonomy(
            args.app, args.level, actor=args.actor, reason=args.reason, force=args.force
        )
    return {"autonomy": reg.get_autonomy(args.app)}


def _reconcile(reg: SqliteRegistry, args: argparse.Namespace) -> Any:
    mirror = build_mirror(get_settings().mlflow_tracking_uri)
    if mirror is None:
        raise RegistryError("MLFLOW_TRACKING_URI is not set or mlflow is not installed")
    found = reconcile(reg, mirror, args.app)
    metrics = BatchMetrics()
    metrics.mirror_drift.labels(args.app).set(int(not found.in_sync))
    for op, count in mirror.failures.items():
        metrics.mirror_failures.labels(args.app, op).set(count)
    metrics.push(get_settings().pushgateway_url, args.app, job="rec-admin")
    return Result(
        {"in_sync": found.in_sync, "expected": found.expected, "actual": found.actual},
        0 if found.in_sync else 1,
    )


def _add_offline_tools(
    sub: Any,
    command: Callable[..., argparse.ArgumentParser],
) -> None:
    """Register explain, simulate, diff, ips, validate-config and schema."""
    explain = command("explain", _tools.explain_request)
    explain.add_argument("--request", required=True, help="JSON body, or @file")
    explain.add_argument("--version")
    explain.add_argument("--eps", type=float, default=0.0)
    for name, handler in (
        ("simulate", _tools.simulate),
        ("diff", _tools.diff_versions),
    ):
        tool = command(name, handler)
        tool.add_argument("--served", type=Path, required=True)
        tool.add_argument("--limit", type=int, default=200)
        if name == "simulate":
            tool.add_argument("--version")
        else:
            tool.add_argument("--a", required=True)
            tool.add_argument("--b", required=True)
            tool.add_argument("--k", type=int, default=10)
    ips = command("ips", _tools.ips_evaluate)
    ips.add_argument("--served", type=Path, required=True)
    ips.add_argument("--interactions", type=Path, required=True)
    ips.add_argument("--version")
    ips.add_argument("--limit", type=int, default=2000)
    validate = sub.add_parser("validate-config")
    validate.add_argument("--app", default="-")
    validate.add_argument("--file", type=Path, required=True)
    validate.set_defaults(handler=_tools.validate_config)
    schema = sub.add_parser("schema")
    schema.add_argument("--app", default="-")
    schema.set_defaults(handler=_tools.print_schema)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rec-admin", description=__doc__)
    parser.add_argument("--actor", default=getpass.getuser())
    sub = parser.add_subparsers(dest="command", required=True)

    def command(
        name: str, handler: Handler, *, reason: bool = False
    ) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--app", required=True)
        if reason:
            p.add_argument("--reason", default="")
        p.set_defaults(handler=handler)
        return p

    command("status", _status)
    command("versions", _versions)
    command("promote", _promote, reason=True).add_argument("--version", required=True)
    command("rollback", _rollback, reason=True)
    command("kill", _kill, reason=True)
    command("unkill", _kill, reason=True)
    for name in ("block", "unblock"):
        command(name, _block, reason=True).add_argument("items", nargs="+")
    command("freeze", _freeze, reason=True)
    unfreeze = command("unfreeze", _freeze)
    unfreeze.add_argument("--reason", required=True)
    command("audit", _audit).add_argument("--limit", type=int, default=20)

    auto = command("autonomy", _autonomy, reason=True)
    auto.add_argument("--level", choices=["L0", "L1", "L2", "L3"])
    auto.add_argument(
        "--force", action="store_true", help="skip the 14-days-at-L2 rule"
    )
    command("reconcile-mlflow", _reconcile)

    _add_offline_tools(sub, command)

    exp = command("experiment", _experiment, reason=True)
    exp.add_argument("action", choices=["shadow", "canary", "end"])
    exp.add_argument("--version")
    exp.add_argument("--pct", type=float, default=10.0)
    exp.add_argument("--autonomy", default="L1", choices=["L0", "L1", "L2", "L3"])
    exp.add_argument("--rollout")
    exp.add_argument("--result", choices=sorted(_END_RESULTS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one admin command; prints JSON, exits 2 on a refused operation."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "experiment":
        if args.action != "end" and not args.version:
            parser.error("--version is required for shadow and canary")
        if args.action == "end" and not (args.rollout and args.result):
            parser.error("end needs --rollout and --result")
    settings = get_settings()
    registry = SqliteRegistry(settings.registry_db, settings.artifact_dir)
    mirror = build_mirror(settings.mlflow_tracking_uri)
    if mirror:
        attach_mirror(registry, mirror)
    try:
        result = args.handler(registry, args)
    except RegistryError as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    code = 0
    if isinstance(result, Result):
        result, code = result.data, result.code
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return code
