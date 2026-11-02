"""``reference.json``: training-time distributions that drift is measured against."""

from __future__ import annotations

from typing import Any

import numpy as np

from recsys.engine.features import N_RECENT_BINS, SCORE_BINS, country_group
from recsys.monitoring.stats import gini, histogram_shares, shares, top_share
from recsys.training.backtest import BacktestReport
from recsys.training.dataset import Dataset

REFERENCE_SCHEMA = 1


def build_reference(
    dataset: Dataset, report: BacktestReport, model_version: str
) -> dict[str, Any]:
    """Snapshot the input and output distributions seen while training.

    Inputs describe the evaluated users (what serving will receive); outputs
    describe what the model produced for them.
    """
    raw = dataset.raw
    users = report.users.tolist()
    counts = np.bincount(dataset.train_item, minlength=dataset.n_items)
    categories = [raw.categories[i] for i in dataset.train_item.tolist()]
    seen = set(dataset.train_item.tolist())
    unseen_test = [i for i in dataset.test_item.tolist() if i not in seen]
    return {
        "schema": REFERENCE_SCHEMA,
        "model_version": model_version,
        "window": {
            "start": dataset.window_start.isoformat(),
            "end": dataset.window_end.isoformat(),
        },
        "inputs": {
            "country_group": shares(country_group(raw.user_country[u]) for u in users),
            "lang": shares(raw.user_lang[u] for u in users),
            "tier": dict(report.tier_share),
            "n_recent": {
                "bins": list(N_RECENT_BINS),
                "p": histogram_shares(report.n_recent, N_RECENT_BINS),
            },
            "category_share": shares(categories),
            "item_popularity": {
                "gini": gini(counts),
                "top1pct_share": top_share(counts, 0.01),
            },
            "new_item_share": len(unseen_test) / max(dataset.test_item.size, 1),
        },
        "outputs": {
            "top1_score": {
                "bins": list(SCORE_BINS),
                "p": histogram_shares(report.top1_scores, SCORE_BINS),
            },
            "src_share": dict(report.src_share),
            "coverage": report.coverage,
            "gini": report.gini,
        },
        "metrics": report.metrics(),
    }
