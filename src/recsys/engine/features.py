"""Feature definitions shared by training and serving.

This module is the single definition of every feature, so training and serving
cannot drift apart (no training/serving skew, hence no feature store).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
from numpy.typing import NDArray

# Bin edges of the "number of recent items" distribution (see reference.json).
N_RECENT_BINS: tuple[int, ...] = (0, 1, 2, 5, 10, 20, 31)
# Bin edges of the top-1 score distribution (see reference.json).
SCORE_BINS: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
MAJOR_COUNTRIES: tuple[str, ...] = ("US", "JP")
OTHER_GROUP = "other"


def recency_weights(n: int, decay: float) -> NDArray[np.float64]:
    """Return weights ``decay**position`` for ``n`` items, newest first."""
    return np.power(decay, np.arange(n, dtype=np.float64))


def resolve_ids(
    index: Mapping[str, int], ids: Iterable[str]
) -> tuple[NDArray[np.int64], int]:
    """Map item ids to catalog indices, de-duplicated, order preserved.

    Args:
        index: Mapping from item id to catalog index.
        ids: Item ids, newest first.

    Returns:
        The known indices and the number of ids not in the catalog.
    """
    seen: set[int] = set()
    known: list[int] = []
    unknown = 0
    for item_id in ids:
        position = index.get(item_id)
        if position is None:
            unknown += 1
        elif position not in seen:
            seen.add(position)
            known.append(position)
    return np.asarray(known, dtype=np.int64), unknown


def country_group(country: str | None) -> str:
    """Collapse a country code into a coarse group used for drift tracking."""
    if country and country.upper() in MAJOR_COUNTRIES:
        return country.upper()
    return OTHER_GROUP


def normalise_max(values: NDArray[np.float64]) -> NDArray[np.float64]:
    """Scale non-negative ``values`` by their maximum; all-zero stays zero."""
    clipped = np.clip(values, 0.0, None)
    peak = float(np.max(clipped)) if clipped.size else 0.0
    return clipped / peak if peak > 0.0 else clipped
