"""Applies release verdicts to the registry: the glue an orchestrator calls."""

from __future__ import annotations

from recsys.registry import RegistryError, Rollout, RolloutStage, SqliteRegistry
from recsys.rollout.policy import (
    CanaryStats,
    Outcome,
    RolloutThresholds,
    ShadowStats,
    Verdict,
    canary_verdict,
    shadow_verdict,
)

AUTO_PROMOTE_LEVEL = "L3"
DEFAULT_CANARY_PCT = 10.0


def apply_shadow(
    registry: SqliteRegistry,
    rollout_id: str,
    stats: ShadowStats,
    *,
    actor: str,
    thresholds: RolloutThresholds = RolloutThresholds(),  # noqa: B008
    canary_pct: float = DEFAULT_CANARY_PCT,
) -> Verdict:
    """Judge the shadow stage and move the rollout accordingly.

    ``PASS`` starts the canary, ``FAIL`` rejects the version and freezes the
    app, ``WAIT`` changes nothing.
    """
    rollout = _require_stage(registry, rollout_id, RolloutStage.SHADOW)
    verdict = shadow_verdict(stats, thresholds)
    detail = {
        "stage": "shadow",
        "outcome": verdict.outcome.value,
        "reasons": list(verdict.reasons),
    }
    if verdict.outcome is Outcome.PASS:
        registry.advance_to_canary(
            rollout.rollout_id, pct=canary_pct, actor=actor, verdict=detail
        )
    elif verdict.outcome is Outcome.FAIL:
        registry.end_rollout(
            rollout.rollout_id,
            RolloutStage.REJECTED,
            actor=actor,
            reason=",".join(verdict.reasons),
            verdict=detail,
        )
    return verdict


def apply_canary(
    registry: SqliteRegistry,
    rollout_id: str,
    stats: CanaryStats,
    *,
    actor: str,
    thresholds: RolloutThresholds = RolloutThresholds(),  # noqa: B008
) -> Verdict:
    """Judge the canary and move the rollout accordingly.

    ``PASS`` promotes at autonomy ``L3`` and otherwise waits for a human;
    ``ROLLBACK`` ends the release, keeps the active model and freezes the app.
    """
    rollout = _require_stage(registry, rollout_id, RolloutStage.CANARY)
    verdict = canary_verdict(stats, thresholds)
    detail = {
        "stage": "canary",
        "outcome": verdict.outcome.value,
        "reasons": list(verdict.reasons),
    }
    if verdict.outcome is Outcome.ROLLBACK:
        registry.end_rollout(
            rollout.rollout_id,
            RolloutStage.ROLLED_BACK,
            actor=actor,
            reason=",".join(verdict.reasons),
            verdict=detail,
        )
    elif verdict.outcome is Outcome.PASS:
        if rollout.autonomy == AUTO_PROMOTE_LEVEL:
            registry.end_rollout(
                rollout.rollout_id,
                RolloutStage.PROMOTED,
                actor=actor,
                reason="canary passed",
                verdict=detail,
            )
        else:
            registry.advance_to_approval(
                rollout.rollout_id, actor=actor, verdict=detail
            )
    return verdict


def _require_stage(
    registry: SqliteRegistry, rollout_id: str, stage: RolloutStage
) -> Rollout:
    rollout = registry.get_rollout(rollout_id)
    if rollout is None:
        raise RegistryError(f"unknown rollout {rollout_id}")
    if rollout.stage is not stage:
        raise RegistryError(f"{rollout_id} is {rollout.stage}, expected {stage}")
    return rollout
