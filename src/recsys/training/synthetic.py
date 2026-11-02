"""Synthetic data: a seedable stand-in for the warehouse, used by tests and CI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from recsys.engine.config import EngineConfig
from recsys.engine.writer import ArtifactPayload

_LANGS = ("en", "ja", "vi")
_LANG_SHARE = (0.6, 0.25, 0.15)
_COUNTRIES = ("US", "JP", "VN", "DE")
_COUNTRY_SHARE = (0.4, 0.2, 0.25, 0.15)


@dataclass(frozen=True, slots=True)
class RawData:
    """Catalog, user attributes and an interaction log.

    Attributes:
        item_ids: Catalog ids.
        categories: Category per item.
        langs: Language per item.
        user_country: Country code per user.
        user_lang: Language per user.
        user_idx: User index of each interaction.
        item_idx: Item index of each interaction.
        ts: Time of each interaction, in days since ``start``; sorted ascending.
        start: Calendar date of ``ts == 0``.
        n_days: Length of the observed window in days.
    """

    item_ids: tuple[str, ...]
    categories: tuple[str, ...]
    langs: tuple[str, ...]
    user_country: tuple[str, ...]
    user_lang: tuple[str, ...]
    user_idx: NDArray[np.int64]
    item_idx: NDArray[np.int64]
    ts: NDArray[np.float64]
    start: date
    n_days: int

    @property
    def n_users(self) -> int:
        """Number of users."""
        return len(self.user_country)


class DataSource(Protocol):
    """Where training data comes from (synthetic, Parquet, the warehouse...)."""

    def load(self) -> RawData:
        """Return the raw data for one training run."""
        ...


@dataclass(frozen=True, slots=True)
class SyntheticSource:
    """A :class:`DataSource` with latent category preferences.

    Users prefer a few categories, so a collaborative model has real signal to
    find and should clearly beat a popularity baseline.
    """

    n_items: int = 400
    n_users: int = 1500
    n_categories: int = 8
    n_days: int = 42
    mean_events: float = 6.0
    seed: int = 7
    start: date = date(2026, 9, 20)

    def load(self) -> RawData:
        """Generate the dataset."""
        rng = np.random.default_rng(self.seed)
        item_cat = rng.integers(0, self.n_categories, self.n_items)
        weight = rng.pareto(1.5, self.n_items) + 1.0
        by_category = [np.flatnonzero(item_cat == c) for c in range(self.n_categories)]
        by_category_p = [
            weight[ix] / weight[ix].sum() if ix.size else ix for ix in by_category
        ]
        preference = rng.dirichlet(np.full(self.n_categories, 0.3), self.n_users)

        users: list[NDArray[np.int64]] = []
        items: list[NDArray[np.int64]] = []
        times: list[NDArray[np.float64]] = []
        for user in range(self.n_users):
            count = 1 + int(rng.poisson(self.mean_events - 1))
            drawn = rng.choice(self.n_categories, size=count, p=preference[user])
            picked = np.empty(count, dtype=np.int64)
            for slot, category in enumerate(drawn):
                if by_category[category].size == 0:
                    picked[slot] = int(rng.integers(self.n_items))
                else:
                    picked[slot] = rng.choice(
                        by_category[category], p=by_category_p[category]
                    )
            users.append(np.full(count, user, dtype=np.int64))
            items.append(picked)
            times.append(rng.uniform(0, self.n_days, count))

        user_idx = np.concatenate(users)
        item_idx = np.concatenate(items)
        ts = np.concatenate(times)
        order = np.argsort(ts, kind="stable")
        return RawData(
            item_ids=tuple(f"i{i:05d}" for i in range(self.n_items)),
            categories=tuple(f"cat{c}" for c in item_cat),
            langs=tuple(rng.choice(_LANGS, self.n_items, p=_LANG_SHARE)),
            user_country=tuple(rng.choice(_COUNTRIES, self.n_users, p=_COUNTRY_SHARE)),
            user_lang=tuple(rng.choice(_LANGS, self.n_users, p=_LANG_SHARE)),
            user_idx=user_idx[order],
            item_idx=item_idx[order],
            ts=ts[order],
            start=self.start,
            n_days=self.n_days,
        )


def random_payload(
    n_items: int = 8308,
    *,
    k: int = 64,
    i2i_k: int = 50,
    n_categories: int = 40,
    seed: int = 0,
    config: EngineConfig | None = None,
) -> ArtifactPayload:
    """Build a random artifact payload of realistic size, without training.

    Used by benchmarks and tests that only need *a* bundle, not a good one.
    """
    rng = np.random.default_rng(seed)
    factors = rng.standard_normal((n_items, k)).astype(np.float32)
    factors /= np.linalg.norm(factors, axis=1, keepdims=True)
    i2i_k = min(i2i_k, n_items - 1)
    neighbours = np.stack(
        [rng.choice(n_items - 1, i2i_k, replace=False) for _ in range(n_items)]
    )
    neighbours = neighbours + (neighbours >= np.arange(n_items)[:, None])
    popularity = (1.0 / np.arange(1, n_items + 1)) ** 0.6
    return ArtifactPayload(
        item_ids=[f"i{i:05d}" for i in range(n_items)],
        categories=[f"cat{c}" for c in rng.integers(0, n_categories, n_items)],
        langs=[str(v) for v in rng.choice(_LANGS, n_items, p=_LANG_SHARE)],
        factors=factors,
        i2i_index=neighbours.astype(np.int32),
        i2i_sim=rng.uniform(0.1, 1.0, (n_items, i2i_k)).astype(np.float32),
        popularity=popularity.astype(np.float32),
        trend=rng.permutation(popularity).astype(np.float32),
        config=config or EngineConfig(),
    )
