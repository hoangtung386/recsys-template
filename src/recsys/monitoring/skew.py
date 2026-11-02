"""Training/serving skew check: what serving sees vs. what users look like.

This replaces a feature store (see section 17.3): features are computed by one
shared function, and this check catches the remaining cause of skew, namely a
client sending a population different from the one in the user table.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from recsys.engine.features import country_group
from recsys.monitoring.stats import psi, shares

SKEW_PSI_WARN = 0.1


@dataclass(frozen=True, slots=True)
class SkewResult:
    """PSI per column and the columns above the threshold."""

    scores: dict[str, float]
    flagged: tuple[str, ...]


def skew_check(
    served: Iterable[Mapping[str, Any]],
    user_attr: Mapping[str, Mapping[str, float]],
    threshold: float = SKEW_PSI_WARN,
) -> SkewResult:
    """Compare ``rec.served`` distributions with user-table distributions.

    Args:
        served: ``rec.served`` rows of one day.
        user_attr: Same-day distributions keyed ``country_group``, ``lang``, ``tier``.
        threshold: PSI above which a column is flagged.
    """
    rows = [r for r in served if r.get("tier")]
    observed = {
        "country_group": shares(country_group(r.get("country")) for r in rows),
        "lang": shares(r["lang"] for r in rows if r.get("lang")),
        "tier": shares(r["tier"] for r in rows),
    }
    scores = {
        name: psi(user_attr[name], dist)
        for name, dist in observed.items()
        if name in user_attr and user_attr[name] and dist
    }
    return SkewResult(
        scores, tuple(sorted(n for n, s in scores.items() if s > threshold))
    )
