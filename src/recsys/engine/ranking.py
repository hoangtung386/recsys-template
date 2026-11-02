"""Candidate scoring: blends collaborative, content and trend signals."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from recsys.engine.bundle import Bundle
from recsys.engine.context import Context
from recsys.engine.features import normalise_max, recency_weights
from recsys.engine.schema import Degraded, Source, Tier

_POPULARITY_TIEBREAK = 1e-3
_SOURCES = (Source.GROUP, Source.CONTENT, Source.TREND)


@dataclass(frozen=True, slots=True)
class Pool:
    """Top candidates, sorted by descending score.

    Attributes:
        index: Catalog indices.
        score: Blended scores aligned with ``index``.
        source: Dominant signal per candidate, aligned with ``index``.
        degraded: ``EMPTY_POOL`` if personalised signals were all zero and the
            pool fell back to popularity.
    """

    index: NDArray[np.int64]
    score: NDArray[np.float64]
    source: tuple[Source, ...]
    degraded: Degraded | None = None


def group_score(bundle: Bundle, ctx: Context) -> NDArray[np.float64]:
    """Collaborative signal: latent-factor affinity mixed with item-item links."""
    n = bundle.n_items
    if ctx.recent.size == 0:
        return np.zeros(n)
    cfg = bundle.config
    weights = recency_weights(ctx.recent.size, cfg.recent_decay)
    profile = (weights @ bundle.factors[ctx.recent]).astype(np.float32)
    latent = (bundle.factors @ profile).astype(np.float64)
    neighbours = bundle.i2i_index[ctx.recent].ravel()
    link_weights = (weights[:, None] * bundle.i2i_sim[ctx.recent]).ravel()
    linked = np.bincount(neighbours, weights=link_weights, minlength=n)
    return cfg.group_mix * normalise_max(latent) + (1 - cfg.group_mix) * normalise_max(
        linked
    )


def content_score(bundle: Bundle, ctx: Context) -> NDArray[np.float64]:
    """Content signal: category affinity from history and onboarding, plus language."""
    cfg = bundle.config
    profile = np.zeros(len(bundle.categories))
    if ctx.recent.size:
        weights = recency_weights(ctx.recent.size, cfg.recent_decay)
        profile += np.bincount(
            bundle.category[ctx.recent], weights=weights, minlength=profile.size
        )
    for name in ctx.onb:
        if name in bundle.categories:
            profile[bundle.categories.index(name)] += 1.0
    score = normalise_max(profile[bundle.category])
    if ctx.lang in bundle.langs:
        match = bundle.lang == bundle.langs.index(str(ctx.lang))
        score = normalise_max(score + cfg.lang_bonus * match)
    return score


def build_pool(bundle: Bundle, ctx: Context, tier: Tier) -> Pool:
    """Score the catalog and keep the best ``pool_size`` candidates.

    Returns an empty pool only when every item is excluded.
    """
    cfg = bundle.config
    weights = cfg.weights_for(tier)
    parts = np.stack(
        [
            weights.group * group_score(bundle, ctx),
            weights.content * content_score(bundle, ctx),
            weights.trend * normalise_max(bundle.trend.astype(np.float64)),
        ]
    )
    popularity = bundle.popularity.astype(np.float64)
    signal = parts.sum(axis=0)
    total = signal + _POPULARITY_TIEBREAK * popularity
    dominant = parts.argmax(axis=0)

    allowed = np.ones(bundle.n_items, dtype=bool)
    allowed[ctx.exclude] = False
    candidates = np.flatnonzero(allowed)
    if candidates.size == 0:
        return Pool(np.empty(0, np.int64), np.empty(0), ())

    degraded = None
    if float(np.max(signal[candidates])) <= 0.0:
        total, dominant = popularity + 1e-9, np.full(bundle.n_items, 2)
        degraded = Degraded.EMPTY_POOL

    scores = total[candidates]
    keep = min(cfg.pool_size, candidates.size)
    if keep < candidates.size:
        top = np.argpartition(-scores, keep - 1)[:keep]
    else:
        top = np.arange(candidates.size)
    chosen = candidates[top]
    order = np.lexsort((chosen, -scores[top]))
    index = chosen[order]
    return Pool(
        index=index,
        score=total[index],
        source=tuple(_SOURCES[code] for code in dominant[index]),
        degraded=degraded,
    )
