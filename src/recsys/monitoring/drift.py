"""Drift scoring: compare serving-time distributions with ``reference.json``.

This module only *measures*. What to do about the numbers is decided by
:mod:`recsys.monitoring.policy`, so a change of statistic never changes policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from recsys.engine.features import N_RECENT_BINS, SCORE_BINS, country_group
from recsys.monitoring.stats import histogram_shares, psi, psi_binned, shares

PSI_DRIFTED = 0.2
_DISCRETE_INPUTS = ("country_group", "lang", "tier")


@dataclass(frozen=True, slots=True)
class ColumnDrift:
    """Drift of one column."""

    layer: str
    column: str
    score: float
    drifted: bool


@dataclass(frozen=True, slots=True)
class DriftResult:
    """Drift of every comparable column."""

    columns: tuple[ColumnDrift, ...]
    n_current: int

    def max_score(self, layer: str) -> float | None:
        """Largest PSI in ``layer``; ``None`` when the layer has no data."""
        scores = [c.score for c in self.columns if c.layer == layer]
        return max(scores) if scores else None

    @property
    def drifted(self) -> tuple[ColumnDrift, ...]:
        """Columns over the PSI threshold."""
        return tuple(c for c in self.columns if c.drifted)


def distributions_from_served(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate ``rec.served`` records into the shape of ``reference.json``.

    Only requests that reached the engine (they carry a ``tier``) are counted.
    """
    served = [row for row in rows if row.get("tier")]
    n_recent = [row["n_recent"] for row in served]
    firsts = [row["items"][0]["score"] for row in served if row.get("items")]
    sources = [item["src"] for row in served for item in row.get("items", ())]
    return {
        "n": len(served),
        "inputs": {
            "country_group": shares(
                country_group(row.get("country")) for row in served
            ),
            "lang": shares(row["lang"] for row in served if row.get("lang")),
            "tier": shares(row["tier"] for row in served),
            "n_recent": {
                "bins": list(N_RECENT_BINS),
                "p": histogram_shares(n_recent, N_RECENT_BINS),
            },
        },
        "outputs": {
            "src_share": shares(sources),
            "top1_score": {
                "bins": list(SCORE_BINS),
                "p": histogram_shares(firsts, SCORE_BINS),
            },
        },
    }


def _compare(
    layer: str,
    column: str,
    expected: Any,
    actual: Any,
    threshold: float,
) -> ColumnDrift | None:
    if not expected or not actual:
        return None
    if "p" in expected:
        score = psi_binned(expected["p"], actual["p"])
    else:
        score = psi(expected, actual)
    return ColumnDrift(layer, column, score, score > threshold)


def compute_drift(
    reference: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    psi_threshold: float = PSI_DRIFTED,
) -> DriftResult:
    """Score every column present in both distributions.

    Args:
        reference: A ``reference.json`` document.
        current: Output of :func:`distributions_from_served`.
        psi_threshold: PSI above which a column counts as drifted.
    """
    columns: list[ColumnDrift] = []
    pairs = [("inputs", name) for name in (*_DISCRETE_INPUTS, "n_recent")]
    pairs += [("outputs", "src_share"), ("outputs", "top1_score")]
    for layer, column in pairs:
        result = _compare(
            layer,
            column,
            reference.get(layer, {}).get(column),
            current.get(layer, {}).get(column),
            psi_threshold,
        )
        if result:
            columns.append(result)
    return DriftResult(tuple(columns), int(current.get("n", 0)))
