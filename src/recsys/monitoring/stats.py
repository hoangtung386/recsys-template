"""Distribution statistics shared by the reference snapshot, drift and the gate."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

import numpy as np
from numpy.typing import ArrayLike

_EPS = 1e-4  # avoids log(0) when a bucket is empty


def shares(values: Iterable[str]) -> dict[str, float]:
    """Return the relative frequency of each distinct value."""
    counts = Counter(values)
    total = sum(counts.values())
    return (
        {key: count / total for key, count in sorted(counts.items())} if total else {}
    )


def histogram_shares(values: ArrayLike, edges: Sequence[float]) -> list[float]:
    """Return the share of ``values`` in each bin of ``edges``.

    The last bin is closed on the right and values outside the range are
    clipped into the first or last bin.
    """
    array = np.asarray(values, dtype=np.float64)
    if array.size == 0:
        return [0.0] * (len(edges) - 1)
    clipped = np.clip(array, edges[0], edges[-1])
    counts, _ = np.histogram(clipped, bins=np.asarray(edges, dtype=np.float64))
    return [float(c) / array.size for c in counts]


def psi(expected: Mapping[str, float], actual: Mapping[str, float]) -> float:
    """Population Stability Index of two discrete distributions.

    Rule of thumb: below 0.1 stable, 0.1 to 0.2 watch, above 0.2 drifted.
    """
    keys = sorted(set(expected) | set(actual))
    return psi_binned(
        [expected.get(k, 0.0) for k in keys], [actual.get(k, 0.0) for k in keys]
    )


def psi_binned(expected: Sequence[float], actual: Sequence[float]) -> float:
    """PSI of two distributions given as aligned probability vectors."""
    e = np.maximum(np.asarray(expected, dtype=np.float64), _EPS)
    a = np.maximum(np.asarray(actual, dtype=np.float64), _EPS)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def gini(counts: ArrayLike) -> float:
    """Gini coefficient of non-negative ``counts``: 0 even, towards 1 concentrated."""
    values = np.sort(np.asarray(counts, dtype=np.float64))
    total = values.sum()
    if values.size == 0 or total <= 0:
        return 0.0
    ranks = np.arange(1, values.size + 1)
    return float(
        (2 * np.sum(ranks * values)) / (values.size * total)
        - (values.size + 1) / values.size
    )


def top_share(counts: ArrayLike, fraction: float = 0.01) -> float:
    """Share of all mass held by the top ``fraction`` of entries."""
    values = np.sort(np.asarray(counts, dtype=np.float64))[::-1]
    total = values.sum()
    if values.size == 0 or total <= 0:
        return 0.0
    keep = max(1, round(values.size * fraction))
    return float(values[:keep].sum() / total)
