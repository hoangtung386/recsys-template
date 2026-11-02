"""Fitting: item factors, item-item neighbours and popularity from interactions."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray
from scipy import sparse
from scipy.sparse.linalg import svds

from recsys.engine.config import EngineConfig
from recsys.engine.writer import ArtifactPayload
from recsys.training.dataset import Dataset

_CHUNK = 512


def _log_popularity(counts: NDArray[np.float64]) -> NDArray[np.float32]:
    peak = float(np.max(counts)) if counts.size else 0.0
    if peak <= 0:
        return np.zeros(counts.shape, dtype=np.float32)
    scaled: NDArray[np.float32] = (np.log1p(counts) / np.log1p(peak)).astype(np.float32)
    return scaled


def _interaction_matrix(
    dataset: Dataset, mask: NDArray[np.bool_] | None = None
) -> sparse.csr_matrix:
    """Binary item-by-user matrix of the training interactions."""
    items, users = dataset.train_item, dataset.train_user
    if mask is not None:
        items, users = items[mask], users[mask]
    matrix = sparse.csr_matrix(
        (np.ones(items.size), (items, users)),
        shape=(dataset.n_items, dataset.n_users),
    )
    matrix.data[:] = 1.0  # repeated interactions count once
    return matrix


def _inverse_sqrt(values: NDArray[np.float64]) -> NDArray[np.float64]:
    out = np.zeros_like(values, dtype=np.float64)
    np.divide(1.0, np.sqrt(values), out=out, where=values > 0)
    return out


def _latent_factors(
    matrix: sparse.csr_matrix, k: int, seed: int
) -> NDArray[np.float32]:
    """Truncated SVD of the degree-normalised matrix, L2-normalised per item."""
    n_items = matrix.shape[0]
    item_deg = np.asarray(matrix.sum(axis=1)).ravel()
    user_deg = np.asarray(matrix.sum(axis=0)).ravel()
    weighted = (
        sparse.diags(_inverse_sqrt(item_deg))
        @ matrix
        @ sparse.diags(_inverse_sqrt(user_deg))
    ).tocsr()
    rank = min(k, min(weighted.shape) - 1)
    factors = np.zeros((n_items, k), dtype=np.float64)
    if rank >= 1:
        v0 = np.random.default_rng(seed).standard_normal(min(weighted.shape))
        u, s, _ = svds(weighted, k=rank, v0=v0)
        factors[:, :rank] = u * np.sqrt(s)
    norms = np.linalg.norm(factors, axis=1, keepdims=True)
    np.divide(factors, norms, out=factors, where=norms > 0)
    return factors.astype(np.float32)


def _neighbours(
    matrix: sparse.csr_matrix, top_k: int
) -> tuple[NDArray[np.int32], NDArray[np.float32]]:
    """Top-``top_k`` cosine neighbours of every item, computed in row chunks."""
    n = matrix.shape[0]
    top_k = min(top_k, n - 1)
    unit = (
        sparse.diags(_inverse_sqrt(np.asarray(matrix.sum(axis=1)).ravel())) @ matrix
    ).tocsr()
    unit_t = unit.T.tocsr()
    index = np.zeros((n, top_k), dtype=np.int32)
    sim = np.zeros((n, top_k), dtype=np.float32)
    for start in range(0, n, _CHUNK):
        stop = min(start + _CHUNK, n)
        block = (unit[start:stop] @ unit_t).toarray()
        block[np.arange(stop - start), np.arange(start, stop)] = -1.0
        top = np.argpartition(-block, top_k - 1, axis=1)[:, :top_k]
        values = np.take_along_axis(block, top, axis=1)
        order = np.argsort(-values, axis=1, kind="stable")
        index[start:stop] = np.take_along_axis(top, order, axis=1)
        sim[start:stop] = np.clip(np.take_along_axis(values, order, axis=1), 0.0, None)
    return index, sim


def fit_payload(
    dataset: Dataset,
    config: EngineConfig,
    *,
    k: int = 64,
    i2i_k: int = 50,
    trend_days: float = 7.0,
    seed: int = 0,
) -> ArtifactPayload:
    """Fit all item-side parameters and package them for serving.

    Args:
        dataset: Training data.
        config: Engine configuration to serve the result with.
        k: Latent dimension (zero-padded when the data has lower rank).
        i2i_k: Neighbours kept per item.
        trend_days: Window, ending at the split, that defines "trending".
        seed: Seed of the SVD start vector.
    """
    matrix = _interaction_matrix(dataset)
    recent = dataset.train_ts >= dataset.split_day - trend_days
    counts = np.asarray(matrix.sum(axis=1)).ravel()
    trend_counts = np.asarray(_interaction_matrix(dataset, recent).sum(axis=1)).ravel()
    neighbours, similarity = _neighbours(matrix, i2i_k)
    return ArtifactPayload(
        item_ids=dataset.raw.item_ids,
        categories=dataset.raw.categories,
        langs=dataset.raw.langs,
        factors=_latent_factors(matrix, k, seed),
        i2i_index=neighbours,
        i2i_sim=similarity,
        popularity=_log_popularity(counts),
        trend=_log_popularity(trend_counts),
        config=config,
    )
