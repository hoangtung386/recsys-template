from __future__ import annotations

import numpy as np
import pytest

from recsys.engine import (
    Bundle,
    Degraded,
    Source,
    Tier,
    build_context,
    recommend,
)
from recsys.engine.diversify import select


def ctx(bundle: Bundle, **kwargs: object):  # type: ignore[no-untyped-def]
    return build_context(bundle, **kwargs)  # type: ignore[arg-type]


def test_deterministic_for_a_seed_and_different_across_seeds(
    random_bundle: Bundle,
) -> None:
    recent = ["i00001", "i00002", "i00003"]
    a = recommend(random_bundle, ctx(random_bundle, recent=recent, seed=7, limit=20))
    b = recommend(random_bundle, ctx(random_bundle, recent=recent, seed=7, limit=20))
    assert a.items == b.items
    other = recommend(
        random_bundle,
        ctx(random_bundle, recent=recent, seed=8, limit=20),
        eps=0.5,
    )
    assert (
        other.items
        != recommend(
            random_bundle, ctx(random_bundle, recent=recent, seed=7, limit=20), eps=0.5
        ).items
    )


def test_never_returns_excluded_or_duplicate_items(random_bundle: Bundle) -> None:
    recent = [f"i{n:05d}" for n in range(10)]
    skip = [f"i{n:05d}" for n in range(10, 40)]
    result = recommend(
        random_bundle,
        ctx(random_bundle, recent=recent, skip=skip, blocked={"i00041"}, limit=100),
    )
    ids = [item.id for item in result.items]
    assert len(ids) == 100
    assert len(set(ids)) == 100
    assert not set(ids) & {*recent, *skip, "i00041"}


@pytest.mark.parametrize(
    ("recent", "tier"),
    [
        ([], Tier.COLD),
        (["i00001"], Tier.LUKEWARM),
        ([f"i{n:05d}" for n in range(6)], Tier.WARM),
    ],
)
def test_tier_follows_known_history(
    random_bundle: Bundle, recent: list[str], tier: Tier
) -> None:
    assert recommend(random_bundle, ctx(random_bundle, recent=recent)).tier is tier


def test_unknown_ids_are_counted_not_fatal(random_bundle: Bundle) -> None:
    c = ctx(random_bundle, recent=["nope", "also-nope", "i00001"])
    assert c.n_unknown == 2
    assert recommend(random_bundle, c).tier is Tier.LUKEWARM


def test_cold_start_without_signals_falls_back_to_trend(random_bundle: Bundle) -> None:
    result = recommend(random_bundle, ctx(random_bundle, limit=10), eps=0.0)
    assert {item.src for item in result.items} == {Source.TREND}


def test_greedy_propensity_when_not_exploring(random_bundle: Bundle) -> None:
    result = recommend(
        random_bundle, ctx(random_bundle, recent=["i00001"], limit=5), eps=0.0
    )
    assert [item.propensity for item in result.items] == [1.0] * 5


def test_exploration_marks_items_and_propensities_are_probabilities(
    random_bundle: Bundle,
) -> None:
    result = recommend(
        random_bundle, ctx(random_bundle, recent=["i00001"], limit=100, seed=3), eps=0.3
    )
    explored = [i for i in result.items if i.src is Source.EXPLORE]
    assert explored
    assert all(0.0 < i.propensity <= 1.0 for i in result.items)
    assert all(i.propensity < 0.3 for i in explored)


def test_everything_excluded_gives_empty_pool(random_bundle: Bundle) -> None:
    everything = list(random_bundle.item_ids)
    result = recommend(random_bundle, ctx(random_bundle, skip=everything))
    assert result.items == ()
    assert result.degraded is Degraded.EMPTY_POOL


def test_deadline_degrades_but_still_fills_the_slate(random_bundle: Bundle) -> None:
    result = recommend(
        random_bundle,
        ctx(random_bundle, recent=["i00001"], limit=50),
        deadline_s=-1.0,
        eps=0.0,
    )
    assert result.degraded is Degraded.DEADLINE
    assert len(result.items) == 50


def test_mmr_trades_relevance_for_diversity() -> None:
    # Two identical top items and one different: pure relevance picks the twins first.
    factors = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
    scores = np.array([1.0, 0.99, 0.9])
    rng = np.random.default_rng(0)
    relevance = select(factors, scores, limit=2, mmr_lambda=1.0, eps=0.0, rng=rng)
    diverse = select(factors, scores, limit=2, mmr_lambda=0.5, eps=0.0, rng=rng)
    assert relevance.positions == (0, 1)
    assert diverse.positions == (0, 2)


def test_limit_larger_than_pool_is_clipped(random_bundle: Bundle) -> None:
    result = recommend(random_bundle, ctx(random_bundle, recent=["i00001"], limit=200))
    assert len(result.items) == 200
