"""The engine entry point: a pure function from ``(bundle, context)`` to a slate."""

from __future__ import annotations

import time

import numpy as np

from recsys.engine.bundle import Bundle
from recsys.engine.context import Context
from recsys.engine.diversify import select
from recsys.engine.ranking import build_pool
from recsys.engine.result import RecItem, RecResult
from recsys.engine.schema import Degraded, Source


def recommend(
    bundle: Bundle,
    ctx: Context,
    *,
    deadline_s: float | None = None,
    eps: float | None = None,
) -> RecResult:
    """Rank the catalog for ``ctx``.

    The function has no side effects and is deterministic for a given
    ``ctx.seed``, so any served slate can be replayed offline.

    Args:
        bundle: The model to rank with.
        ctx: Caller context.
        deadline_s: Soft budget. When exceeded the slate is completed in score
            order and the result is flagged ``Degraded.DEADLINE``.
        eps: Override of the configured exploration rate (use ``0`` offline).
    """
    started = time.perf_counter()
    cfg = bundle.config
    tier = cfg.tier_of(int(ctx.recent.size))
    pool = build_pool(bundle, ctx, tier)
    if pool.index.size == 0:
        elapsed = time.perf_counter() - started
        return RecResult((), tier, 0, Degraded.EMPTY_POOL, elapsed)

    selection = select(
        bundle.factors[pool.index],
        pool.score,
        limit=ctx.limit,
        mmr_lambda=cfg.mmr_lambda,
        eps=cfg.eps if eps is None else eps,
        rng=np.random.default_rng(ctx.seed),
        deadline_at=None if deadline_s is None else started + deadline_s,
    )
    items = tuple(
        RecItem(
            id=bundle.item_ids[int(pool.index[pos])],
            score=float(pool.score[pos]),
            src=Source.EXPLORE if was_explore else pool.source[pos],
            propensity=prob,
        )
        for pos, was_explore, prob in zip(
            selection.positions, selection.explored, selection.propensity, strict=True
        )
    )
    degraded = Degraded.DEADLINE if selection.timed_out else pool.degraded
    return RecResult(
        items=items,
        tier=tier,
        pool_size=int(pool.index.size),
        degraded=degraded,
        engine_seconds=time.perf_counter() - started,
    )
