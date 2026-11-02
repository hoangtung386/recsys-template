"""Turning a request into the index-based :class:`Context` the engine ranks for."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from recsys.engine.bundle import Bundle
from recsys.engine.features import resolve_ids
from recsys.engine.request import RecRequest
from recsys.engine.schema import DEFAULT_LIMIT


@dataclass(frozen=True, slots=True)
class Context:
    """Everything the engine needs about the caller, as catalog indices.

    Attributes:
        recent: Known recent item indices, newest first, unique.
        exclude: Indices that must never be returned (recent, skipped, blocked).
        lang: Preferred language code, if any.
        onb: Onboarding category names.
        limit: Number of items wanted.
        seed: Seed of the exploration RNG; same seed, same response.
        n_unknown: How many ``recent`` ids were not in this catalog.
    """

    recent: NDArray[np.int64]
    exclude: NDArray[np.int64]
    lang: str | None = None
    onb: tuple[str, ...] = ()
    limit: int = DEFAULT_LIMIT
    seed: int = 0
    n_unknown: int = 0


def build_context(
    bundle: Bundle,
    *,
    recent: Iterable[str] = (),
    skip: Iterable[str] = (),
    blocked: Collection[str] = (),
    lang: str | None = None,
    onb: tuple[str, ...] = (),
    limit: int = DEFAULT_LIMIT,
    seed: int = 0,
) -> Context:
    """Resolve item ids against ``bundle`` and assemble a :class:`Context`."""
    recent_idx, unknown = resolve_ids(bundle.index, recent)
    skip_idx, _ = resolve_ids(bundle.index, skip)
    blocked_idx, _ = resolve_ids(bundle.index, blocked)
    exclude = np.unique(np.concatenate([recent_idx, skip_idx, blocked_idx]))
    return Context(
        recent=recent_idx,
        exclude=exclude,
        lang=lang,
        onb=onb,
        limit=limit,
        seed=seed,
        n_unknown=unknown,
    )


def context_from_request(
    bundle: Bundle, req: RecRequest, blocked: Collection[str] = ()
) -> Context:
    """Build a :class:`Context` from a validated request."""
    return build_context(
        bundle,
        recent=req.recent,
        skip=req.skip,
        blocked=blocked,
        lang=req.lang,
        onb=req.onb,
        limit=req.limit,
        seed=req.derived_seed(),
    )


def context_from_served(
    bundle: Bundle, row: Mapping[str, Any], blocked: Collection[str] = ()
) -> Context:
    """Rebuild the :class:`Context` of a logged ``rec.served`` record.

    Together with the logged ``seed`` this replays the request offline.
    """
    return build_context(
        bundle,
        recent=row.get("recent", ()),
        skip=row.get("skip", ()),
        blocked=blocked,
        lang=row.get("lang"),
        onb=tuple(row.get("onb", ())),
        limit=int(row.get("limit", DEFAULT_LIMIT)),
        seed=int(row.get("seed", 0)),
    )
