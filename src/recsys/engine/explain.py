"""Explaining a slate: how much each signal contributed to each item."""

from __future__ import annotations

from typing import Any

from recsys.engine.bundle import Bundle
from recsys.engine.context import Context
from recsys.engine.features import normalise_max
from recsys.engine.ranking import content_score, group_score
from recsys.engine.recommend import recommend


def explain(bundle: Bundle, ctx: Context, *, eps: float = 0.0) -> dict[str, Any]:
    """Return the slate with the weighted contribution of every signal.

    Args:
        bundle: Model to explain.
        ctx: Caller context.
        eps: Exploration rate; ``0`` shows the pure policy.
    """
    result = recommend(bundle, ctx, eps=eps)
    weights = bundle.config.weights_for(result.tier)
    group = group_score(bundle, ctx)
    content = content_score(bundle, ctx)
    trend = normalise_max(bundle.trend.astype(float))
    rows = []
    for item in result.items:
        i = bundle.index[item.id]
        rows.append(
            {
                "id": item.id,
                "src": item.src.value,
                "score": round(item.score, 5),
                "propensity": round(item.propensity, 5),
                "category": bundle.categories[int(bundle.category[i])],
                "group": round(weights.group * float(group[i]), 5),
                "content": round(weights.content * float(content[i]), 5),
                "trend": round(weights.trend * float(trend[i]), 5),
            }
        )
    return {
        "version": bundle.version,
        "tier": result.tier.value,
        "weights": {
            "group": weights.group,
            "content": weights.content,
            "trend": weights.trend,
        },
        "degraded": result.degraded.value if result.degraded else None,
        "n_known_recent": int(ctx.recent.size),
        "n_unknown_recent": ctx.n_unknown,
        "items": rows,
    }
