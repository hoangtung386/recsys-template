"""Off-policy evaluation from logged propensities (IPS and SNIPS).

The serving policy explores with a known probability per slot and logs it. That
makes it possible to estimate how a *different* policy would have performed on
the same traffic, without an A/B test.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IpsResult:
    """Estimated clicks of a target policy.

    Attributes:
        ips: Clicks per slot, plain inverse-propensity estimate (unbiased, noisy).
        snips: Self-normalised estimate: weighted click rate over shared slots
            (small bias, much lower variance).
        n_requests: Requests for which the target supplied a slate.
        n_matched: Slots shared between the logged and the target slates.
    """

    ips: float
    snips: float
    n_requests: int
    n_matched: int


def ips_estimate(
    served: Iterable[Mapping[str, Any]],
    target: Mapping[str, Sequence[str]],
    clicks: Mapping[str, set[str]],
) -> IpsResult:
    """Estimate how the ``target`` policy would have done on the logged traffic.

    For a request with logged slate ``L`` and target slate ``T``, let
    ``n = sum over i in L and T of click_i / p_i``. IPS averages ``n / |T|``
    over requests. SNIPS divides ``sum n`` by ``sum 1 / p_i`` over the same
    shared slots.

    Args:
        served: ``rec.served`` rows (items carry ``propensity``).
        target: Slate the target policy would have served, by ``request_id``.
        clicks: Clicked item ids by ``request_id``.
    """
    per_slot = numerator = weights = 0.0
    requests = matched = 0
    for row in served:
        slate = target.get(row.get("request_id", ""))
        if not slate:
            continue
        requests += 1
        wanted = set(slate)
        got = clicks.get(row["request_id"], set())
        request_numerator = 0.0
        for item in row.get("items", ()):
            if item["id"] in wanted and item.get("propensity", 0) > 0:
                weight = 1.0 / item["propensity"]
                request_numerator += weight * (item["id"] in got)
                weights += weight
                matched += 1
        numerator += request_numerator
        per_slot += request_numerator / len(slate)
    return IpsResult(
        ips=per_slot / requests if requests else 0.0,
        snips=numerator / weights if weights else 0.0,
        n_requests=requests,
        n_matched=matched,
    )
