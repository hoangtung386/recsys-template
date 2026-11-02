"""Time-based train/test split of the interaction log."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from numpy.typing import NDArray

from recsys.engine.schema import MAX_RECENT
from recsys.training.synthetic import RawData


@dataclass(frozen=True, slots=True, eq=False)
class Dataset:
    """A global time split: everything before ``split_day`` trains, the rest tests.

    Attributes:
        raw: The unsplit data (catalog and user attributes live here).
        train_user: User index of each training interaction.
        train_item: Item index of each training interaction.
        train_ts: Time of each training interaction, ascending.
        test_user: User index of each test interaction.
        test_item: Item index of each test interaction.
        split_day: First day (since ``raw.start``) of the test period.
        window_start: Calendar date of the first training day.
        window_end: Calendar date of the last day of data.
    """

    raw: RawData
    train_user: NDArray[np.int64]
    train_item: NDArray[np.int64]
    train_ts: NDArray[np.float64]
    test_user: NDArray[np.int64]
    test_item: NDArray[np.int64]
    split_day: float
    window_start: date
    window_end: date

    @property
    def n_items(self) -> int:
        """Catalog size."""
        return len(self.raw.item_ids)

    @property
    def n_users(self) -> int:
        """Number of users."""
        return self.raw.n_users

    def recent_by_user(
        self, max_recent: int = MAX_RECENT
    ) -> dict[int, NDArray[np.int64]]:
        """Return each user's latest training items, newest first."""
        if self.train_user.size == 0:
            return {}
        order = np.lexsort((self.train_ts, self.train_user))
        users, items = self.train_user[order], self.train_item[order]
        starts = np.concatenate([[0], np.flatnonzero(np.diff(users)) + 1])
        groups = np.split(items, starts[1:])
        return {
            int(user): group[::-1][:max_recent]
            for user, group in zip(users[starts], groups, strict=True)
        }


def build_dataset(raw: RawData, *, test_days: int = 7) -> Dataset:
    """Split ``raw`` so the last ``test_days`` days form the test set.

    Raises:
        ValueError: If the window is too short to leave training data.
    """
    if not 0 < test_days < raw.n_days:
        raise ValueError("test_days must be in (0, n_days)")
    split_day = float(raw.n_days - test_days)
    train = raw.ts < split_day
    return Dataset(
        raw=raw,
        train_user=raw.user_idx[train],
        train_item=raw.item_idx[train],
        train_ts=raw.ts[train],
        test_user=raw.user_idx[~train],
        test_item=raw.item_idx[~train],
        split_day=split_day,
        window_start=raw.start,
        window_end=raw.start + timedelta(days=raw.n_days - 1),
    )
