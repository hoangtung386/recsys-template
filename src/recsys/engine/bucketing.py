"""Deterministic experiment assignment.

A device always lands in the same arm for the same experiment. The permanent
holdout is hashed independently of the experiment so it survives experiments.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from recsys.engine.schema import Arm

BUCKETS = 10_000
_HOLDOUT_ID = "holdout"


@dataclass(frozen=True, slots=True)
class Experiment:
    """Traffic split and rollout controls for one app."""

    experiment_id: str = "default"
    salt: str = ""
    treatment_pct: float = 50.0
    holdout_pct: float = 5.0
    canary_version: str | None = None
    canary_pct: float = 0.0
    shadow_version: str | None = None
    shadow_pct: float = 0.0

    def __post_init__(self) -> None:
        """Validate percentages and version/percentage consistency."""
        for name in ("treatment_pct", "holdout_pct", "canary_pct", "shadow_pct"):
            if not 0.0 <= getattr(self, name) <= 100.0:
                raise ValueError(f"{name} must be in [0, 100]")
        if self.canary_pct > 0 and not self.canary_version:
            raise ValueError("canary_pct > 0 requires canary_version")
        if self.shadow_pct > 0 and not self.shadow_version:
            raise ValueError("shadow_pct > 0 requires shadow_version")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible types."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Experiment:
        """Build from a mapping, rejecting unknown keys."""
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown experiment keys: {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class Assignment:
    """Outcome of :func:`assign_arm`.

    Attributes:
        arm: The assigned arm.
        version: Model version to serve instead of the active one, if any.
        bucket: The experiment bucket in ``[0, BUCKETS)``, for diagnostics.
    """

    arm: Arm
    version: str | None
    bucket: int


def bucket_of(app: str, experiment_id: str, salt: str, device_id: str) -> int:
    """Hash ``(app, experiment, salt, device)`` into ``[0, BUCKETS)``."""
    digest = hashlib.sha256(
        f"{app}|{experiment_id}|{salt}|{device_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def _below(bucket: int, pct: float) -> bool:
    return bucket < round(pct * BUCKETS / 100)


def assign_arm(app: str, device_id: str, experiment: Experiment) -> Assignment:
    """Assign a device to an arm.

    Order of precedence: permanent holdout, shadow slice, control/treatment
    split, then canary inside the treatment arm.
    """
    exp = experiment
    main = bucket_of(app, exp.experiment_id, exp.salt, device_id)
    if _below(bucket_of(app, _HOLDOUT_ID, "", device_id), exp.holdout_pct):
        return Assignment(Arm.HOLDOUT, None, main)
    if exp.shadow_version and _below(
        bucket_of(app, exp.experiment_id, f"{exp.salt}:shadow", device_id),
        exp.shadow_pct,
    ):
        return Assignment(Arm.SHADOW, exp.shadow_version, main)
    arm_bucket = bucket_of(app, exp.experiment_id, f"{exp.salt}:arm", device_id)
    if not _below(arm_bucket, exp.treatment_pct):
        return Assignment(Arm.CONTROL, None, main)
    if exp.canary_version and _below(
        bucket_of(app, exp.experiment_id, f"{exp.salt}:canary", device_id),
        exp.canary_pct,
    ):
        return Assignment(Arm.TREATMENT, exp.canary_version, main)
    return Assignment(Arm.TREATMENT, None, main)
