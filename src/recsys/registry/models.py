"""Registry records and the model lifecycle state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ModelStatus(StrEnum):
    """Lifecycle state of one model version."""

    CANDIDATE = "candidate"
    VALIDATED = "validated"
    REJECTED = "rejected"
    ACTIVE = "active"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


# A version moves only along these edges; anything else is a bug or an operator error.
ALLOWED_TRANSITIONS: dict[ModelStatus, frozenset[ModelStatus]] = {
    ModelStatus.CANDIDATE: frozenset({ModelStatus.VALIDATED, ModelStatus.REJECTED}),
    ModelStatus.VALIDATED: frozenset(
        {ModelStatus.ACTIVE, ModelStatus.REJECTED, ModelStatus.ROLLED_BACK}
    ),
    ModelStatus.ACTIVE: frozenset({ModelStatus.RETIRED, ModelStatus.ROLLED_BACK}),
    ModelStatus.RETIRED: frozenset({ModelStatus.ACTIVE}),
    ModelStatus.REJECTED: frozenset(),
    ModelStatus.ROLLED_BACK: frozenset(),
}


class RolloutStage(StrEnum):
    """Stage of one release of a validated model."""

    SHADOW = "shadow"
    CANARY = "canary"
    AWAITING_APPROVAL = "awaiting_approval"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    ABORTED = "aborted"


IN_PROGRESS_STAGES = frozenset(
    {RolloutStage.SHADOW, RolloutStage.CANARY, RolloutStage.AWAITING_APPROVAL}
)


class RegistryError(RuntimeError):
    """An operation is not allowed in the current registry state."""


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """One trained model version.

    Attributes:
        app_id: App slug.
        model_version: Version string, ``YYYYMMDD-<config hash>[-rN]``.
        status: Lifecycle state.
        config_hash: Digest of the engine configuration.
        artifact_path: Where the artifact lives.
        run_id: Experiment-tracking run that produced it.
        created_at: ISO-8601 UTC timestamp.
        data: Free-form lineage: data window, gate and backtest results.
    """

    app_id: str
    model_version: str
    status: ModelStatus
    config_hash: str
    artifact_path: str
    run_id: str | None = None
    created_at: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Rollout:
    """One release attempt of a validated model."""

    rollout_id: str
    app_id: str
    model_version: str
    baseline_version: str | None
    stage: RolloutStage
    autonomy: str
    started_at: str
    stage_since: str
    verdict: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoopState:
    """The freeze flag that stops the closed loop after a failed release."""

    app_id: str
    frozen: bool = False
    frozen_by: str | None = None
    frozen_at: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One line of the audit trail."""

    ts: str
    app_id: str
    actor: str
    action: str
    model_version: str | None
    reason: str
