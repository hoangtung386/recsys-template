"""Engine configuration: immutable, validated, hashable into a ``config_hash``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from recsys.engine.schema import Tier


@dataclass(frozen=True, slots=True)
class TierWeights:
    """Mixing weights of the three scoring signals for one tier."""

    group: float
    content: float
    trend: float

    def __post_init__(self) -> None:
        """Reject negative or all-zero weights."""
        values = (self.group, self.content, self.trend)
        if min(values) < 0 or sum(values) <= 0:
            raise ValueError(f"invalid tier weights: {values}")


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """Everything that changes what the engine returns for a given request."""

    pool_size: int = 500
    mmr_lambda: float = 0.7
    eps: float = 0.05
    recent_decay: float = 0.9
    group_mix: float = 0.5
    lang_bonus: float = 0.2
    lukewarm_min_recent: int = 1
    warm_min_recent: int = 5
    cold: TierWeights = field(default_factory=lambda: TierWeights(0.0, 0.4, 0.6))
    lukewarm: TierWeights = field(default_factory=lambda: TierWeights(0.4, 0.3, 0.3))
    warm: TierWeights = field(default_factory=lambda: TierWeights(0.6, 0.2, 0.2))

    def __post_init__(self) -> None:
        """Validate ranges so that a bad config fails at load, not at serve time."""
        if self.pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        for name in ("mmr_lambda", "eps", "group_mix"):
            if not 0.0 <= getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if not 0.0 < self.recent_decay <= 1.0:
            raise ValueError("recent_decay must be in (0, 1]")
        if not 0 <= self.lukewarm_min_recent <= self.warm_min_recent:
            raise ValueError("require 0 <= lukewarm_min_recent <= warm_min_recent")

    def tier_of(self, n_known_recent: int) -> Tier:
        """Map the number of known recent items to a tier."""
        if n_known_recent >= self.warm_min_recent:
            return Tier.WARM
        if n_known_recent >= max(self.lukewarm_min_recent, 1):
            return Tier.LUKEWARM
        return Tier.COLD

    def weights_for(self, tier: Tier) -> TierWeights:
        """Return the signal weights used for ``tier``."""
        return {
            Tier.COLD: self.cold,
            Tier.LUKEWARM: self.lukewarm,
            Tier.WARM: self.warm,
        }[tier]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible types."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineConfig:
        """Build from a mapping, rejecting unknown keys.

        Raises:
            ValueError: If ``data`` contains keys that are not config fields.
        """
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        tiers: dict[str, TierWeights] = {
            name: TierWeights(**data[name])
            for name in ("cold", "lukewarm", "warm")
            if name in data
        }
        kwargs: dict[str, Any] = {k: v for k, v in data.items() if k not in tiers}
        kwargs.update(tiers)
        return cls(**kwargs)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Return a JSON Schema of the configuration, generated from the fields."""
        weights = {
            "type": "object",
            "additionalProperties": False,
            "required": ["group", "content", "trend"],
            "properties": {
                name: {"type": "number", "minimum": 0}
                for name in ("group", "content", "trend")
            },
        }
        unit = {"type": "number", "minimum": 0, "maximum": 1}
        special: dict[str, dict[str, Any]] = {
            "pool_size": {"type": "integer", "minimum": 1},
            "mmr_lambda": unit,
            "eps": unit,
            "group_mix": unit,
            "recent_decay": {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": 1,
            },
            "lang_bonus": {"type": "number"},
            "lukewarm_min_recent": {"type": "integer", "minimum": 0},
            "warm_min_recent": {"type": "integer", "minimum": 0},
            "cold": weights,
            "lukewarm": weights,
            "warm": weights,
        }
        names = [f.name for f in fields(cls)]
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "EngineConfig",
            "type": "object",
            "additionalProperties": False,
            "properties": {name: special[name] for name in names},
        }

    def config_hash(self) -> str:
        """Return a short, stable digest of the configuration."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]
