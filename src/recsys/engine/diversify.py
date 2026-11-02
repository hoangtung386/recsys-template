"""Final slate selection: MMR diversification with epsilon-greedy exploration."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class Selection:
    """Chosen pool positions, in slate order.

    Attributes:
        positions: Indices into the pool.
        explored: ``True`` where the slot was filled by exploration.
        propensity: Probability the policy had of choosing each slot's item.
        timed_out: The deadline hit and the tail was filled greedily.
    """

    positions: tuple[int, ...]
    explored: tuple[bool, ...]
    propensity: tuple[float, ...]
    timed_out: bool


def select(
    factors: NDArray[np.float32],
    scores: NDArray[np.float64],
    *,
    limit: int,
    mmr_lambda: float,
    eps: float,
    rng: np.random.Generator,
    deadline_at: float | None = None,
) -> Selection:
    """Pick ``limit`` items from a score-sorted pool.

    Each slot takes the item with the best MMR value (relevance minus
    similarity to what is already chosen). With probability ``eps`` the slot is
    filled by a uniformly random remaining item instead, and the propensity of
    every choice is recorded so offline evaluation can be inverse-propensity
    weighted.

    Args:
        factors: ``(P, k)`` unit vectors of the pool items.
        scores: ``(P,)`` non-negative scores, sorted descending.
        limit: Slate length (clipped to the pool size).
        mmr_lambda: 1 = pure relevance, 0 = pure diversity.
        eps: Exploration probability per slot.
        rng: Source of randomness; seed it for reproducible slates.
        deadline_at: ``time.perf_counter()`` value after which the remaining
            slots are filled in score order.
    """
    pool_size = scores.size
    limit = min(limit, pool_size)
    peak = float(np.max(scores)) if pool_size else 0.0
    relevance = scores / peak if peak > 0 else np.zeros(pool_size)
    base = mmr_lambda * relevance
    penalty = 1.0 - mmr_lambda
    max_sim = np.zeros(pool_size)
    taken = np.zeros(pool_size)  # +inf once an item is chosen
    value = base.copy()
    positions: list[int] = []
    explored: list[bool] = []
    propensity: list[float] = []
    timed_out = False

    while len(positions) < limit:
        if deadline_at is not None and time.perf_counter() > deadline_at:
            timed_out = True
            break
        n_available = pool_size - len(positions)
        greedy = int(np.argmax(value))
        pick, is_explore = greedy, False
        if eps > 0.0 and rng.random() < eps:
            open_slots = np.flatnonzero(taken == 0.0)
            pick = int(open_slots[rng.integers(open_slots.size)])
            is_explore = pick != greedy
        positions.append(pick)
        explored.append(is_explore)
        propensity.append(eps / n_available + (0.0 if is_explore else 1.0 - eps))
        taken[pick] = np.inf
        if penalty > 0.0:
            np.maximum(max_sim, factors @ factors[pick], out=max_sim)
            np.subtract(base, penalty * max_sim, out=value)
        value -= taken

    if timed_out:
        rest = np.flatnonzero(taken == 0.0)[: limit - len(positions)]
        positions.extend(int(p) for p in rest)
        explored.extend(False for _ in rest)
        propensity.extend(1.0 for _ in rest)
    return Selection(tuple(positions), tuple(explored), tuple(propensity), timed_out)
