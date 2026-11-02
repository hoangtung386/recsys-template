"""Value objects returned by the engine."""

from __future__ import annotations

from dataclasses import dataclass

from recsys.engine.schema import Degraded, Source, Tier


@dataclass(frozen=True, slots=True)
class RecItem:
    """One recommended item.

    Attributes:
        id: Item id.
        score: Blended score, comparable only within one response.
        src: Signal that selected the item.
        propensity: Probability with which the selection policy chose this item
            at its slot; needed for inverse-propensity-scored evaluation.
    """

    id: str
    score: float
    src: Source
    propensity: float


@dataclass(frozen=True, slots=True)
class RecResult:
    """A ranked list plus the facts needed to monitor how it was produced."""

    items: tuple[RecItem, ...]
    tier: Tier
    pool_size: int
    degraded: Degraded | None
    engine_seconds: float
