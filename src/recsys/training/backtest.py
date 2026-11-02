"""Offline evaluation, run through the same engine that serves production."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from recsys.engine.bundle import Bundle
from recsys.engine.context import Context
from recsys.engine.recommend import recommend
from recsys.engine.schema import Source
from recsys.monitoring.stats import gini
from recsys.training.dataset import Dataset

DEFAULT_K = 10


@dataclass(frozen=True, slots=True)
class Ranked:
    """One user's ranked list as produced by a ranker.

    Attributes:
        items: Catalog indices, best first.
        top1_score: Score of the first item, ``None`` for rankers without scores.
        sources: Signal label of each item.
        tier: Tier label, ``""`` for rankers without tiers.
    """

    items: list[int]
    top1_score: float | None = None
    sources: tuple[str, ...] = ()
    tier: str = ""


Ranker = Callable[[Dataset, int, NDArray[np.int64]], Ranked]


@dataclass(frozen=True, slots=True, eq=False)
class BacktestReport:
    """Offline quality and exposure metrics.

    Attributes:
        k: List length the metrics are computed at.
        n_users: Number of evaluated users.
        hit: Mean share of users with at least one hit in the top ``k``.
        ndcg: Mean binary-relevance NDCG at ``k``.
        coverage: Share of the catalog recommended at least once.
        gini: Concentration of exposure over items.
        users: Evaluated user indices.
        n_recent: Number of recent items each evaluated user had.
        top1_scores: Score of the first item per user (engine rankers only).
        src_share: Share of slots per signal label.
        tier_share: Share of users per tier.
    """

    k: int
    n_users: int
    hit: float
    ndcg: float
    coverage: float
    gini: float
    users: NDArray[np.int64] = field(repr=False)
    n_recent: NDArray[np.int64] = field(repr=False)
    top1_scores: NDArray[np.float64] = field(repr=False)
    src_share: dict[str, float] = field(default_factory=dict)
    tier_share: dict[str, float] = field(default_factory=dict)

    def metrics(self) -> dict[str, float]:
        """Return the scalar metrics, keyed with the cut-off."""
        return {
            f"ndcg@{self.k}": self.ndcg,
            f"hit@{self.k}": self.hit,
            "coverage": self.coverage,
            "gini": self.gini,
            "n_users": float(self.n_users),
        }


def engine_ranker(bundle: Bundle, k: int = DEFAULT_K) -> Ranker:
    """Rank with the production engine, without exploration."""

    def rank(dataset: Dataset, user: int, recent: NDArray[np.int64]) -> Ranked:
        ctx = Context(
            recent=recent,
            exclude=np.unique(recent),
            lang=dataset.raw.user_lang[user],
            limit=k,
            seed=user,
        )
        result = recommend(bundle, ctx, eps=0.0)
        return Ranked(
            items=[bundle.index[item.id] for item in result.items],
            top1_score=result.items[0].score if result.items else None,
            sources=tuple(item.src.value for item in result.items),
            tier=result.tier.value,
        )

    return rank


def popularity_ranker(bundle: Bundle, k: int = DEFAULT_K) -> Ranker:
    """Baseline: the most popular items the user has not seen."""
    order = np.argsort(-bundle.popularity, kind="stable")

    def rank(dataset: Dataset, user: int, recent: NDArray[np.int64]) -> Ranked:  # noqa: ARG001
        seen = set(recent.tolist())
        return Ranked(items=[int(i) for i in order if int(i) not in seen][:k])

    return rank


def _ndcg(ranked: list[int], relevant: set[int], k: int) -> float:
    gains = sum(
        1.0 / np.log2(pos + 2)
        for pos, item in enumerate(ranked[:k])
        if item in relevant
    )
    ideal = sum(1.0 / np.log2(pos + 2) for pos in range(min(len(relevant), k)))
    return float(gains / ideal) if ideal else 0.0


def evaluate(dataset: Dataset, ranker: Ranker, *, k: int = DEFAULT_K) -> BacktestReport:
    """Evaluate ``ranker`` on every user who has test-period interactions.

    A user's ``recent`` is their latest training items (cold users have none);
    targets already seen in ``recent`` are not counted.
    """
    recents = dataset.recent_by_user()
    targets: dict[int, set[int]] = defaultdict(set)
    for user, item in zip(
        dataset.test_user.tolist(), dataset.test_item.tolist(), strict=True
    ):
        targets[user].add(item)

    exposure = np.zeros(dataset.n_items)
    users: list[int] = []
    n_recent: list[int] = []
    top1: list[float] = []
    ndcgs: list[float] = []
    hits = 0
    sources: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    empty = np.empty(0, dtype=np.int64)
    for user, wanted in sorted(targets.items()):
        recent = recents.get(user, empty)
        relevant = wanted - set(recent.tolist())
        if not relevant:
            continue
        ranked = ranker(dataset, user, recent)
        users.append(user)
        n_recent.append(int(recent.size))
        ndcgs.append(_ndcg(ranked.items, relevant, k))
        hits += any(item in relevant for item in ranked.items[:k])
        np.add.at(exposure, ranked.items[:k], 1.0)
        if ranked.top1_score is not None:
            top1.append(ranked.top1_score)
        sources.update(ranked.sources[:k])
        if ranked.tier:
            tiers[ranked.tier] += 1

    n = len(users)
    total_slots = sum(sources.values())
    return BacktestReport(
        k=k,
        n_users=n,
        hit=hits / n if n else 0.0,
        ndcg=float(np.mean(ndcgs)) if ndcgs else 0.0,
        coverage=float((exposure > 0).sum() / dataset.n_items),
        gini=gini(exposure),
        users=np.asarray(users, dtype=np.int64),
        n_recent=np.asarray(n_recent, dtype=np.int64),
        top1_scores=np.asarray(top1, dtype=np.float64),
        src_share={
            s.value: sources[s.value] / total_slots for s in Source if sources[s.value]
        }
        if total_slots
        else {},
        tier_share={t: c / n for t, c in sorted(tiers.items())} if n else {},
    )
