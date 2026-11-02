"""Publishing a model artifact atomically."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from recsys.engine import schema
from recsys.engine.bundle import sha256_file
from recsys.engine.config import EngineConfig
from recsys.engine.errors import ArtifactError, ArtifactExistsError


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    """Everything that goes into one artifact.

    Attributes:
        item_ids: Catalog ids; row ``i`` of every array belongs to ``item_ids[i]``.
        categories: Category label per item.
        langs: Language label per item.
        factors: ``(n, k)`` L2-normalised latent item vectors.
        i2i_index: ``(n, K)`` indices of each item's nearest neighbours.
        i2i_sim: ``(n, K)`` similarities matching ``i2i_index``.
        popularity: ``(n,)`` all-time popularity in ``[0, 1]``.
        trend: ``(n,)`` recent popularity in ``[0, 1]``.
        config: Engine configuration to serve this model with.
        reference: Training-time distributions kept for drift monitoring.
        extra_manifest: Lineage, data window, gate and backtest results.
    """

    item_ids: Sequence[str]
    categories: Sequence[str]
    langs: Sequence[str]
    factors: NDArray[np.float32]
    i2i_index: NDArray[np.int32]
    i2i_sim: NDArray[np.float32]
    popularity: NDArray[np.float32]
    trend: NDArray[np.float32]
    config: EngineConfig
    reference: Mapping[str, Any] | None = None
    extra_manifest: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Check that all per-item arrays agree on the catalog size.

        Raises:
            ArtifactError: If shapes disagree or ids are not unique.
        """
        n = len(self.item_ids)
        if n == 0 or len(set(self.item_ids)) != n:
            raise ArtifactError("item ids must be non-empty and unique")
        sizes = {
            "categories": len(self.categories),
            "langs": len(self.langs),
            "factors": self.factors.shape[0],
            "i2i_index": self.i2i_index.shape[0],
            "i2i_sim": self.i2i_sim.shape[0],
            "popularity": self.popularity.shape[0],
            "trend": self.trend.shape[0],
        }
        bad = {name: size for name, size in sizes.items() if size != n}
        if bad or self.i2i_index.shape != self.i2i_sim.shape:
            raise ArtifactError(f"shape mismatch with {n} items: {bad}")
        if self.i2i_index.size and int(np.max(self.i2i_index)) >= n:
            raise ArtifactError("i2i_index points outside the catalog")


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    )


def write_artifact(root: Path, version: str, payload: ArtifactPayload) -> Path:
    """Publish ``payload`` as ``root/version``.

    The directory is built under a temporary name and renamed into place, so
    readers see either nothing or a complete artifact.

    Raises:
        ArtifactExistsError: If ``root/version`` already exists.
        ArtifactError: If the payload is inconsistent.
    """
    payload.validate()
    final = root / version
    if final.exists():
        raise ArtifactExistsError(f"{final} already exists")
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".tmp-{version}-{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    try:
        _write_json(
            staging / schema.ITEMS,
            {
                "ids": list(payload.item_ids),
                "categories": list(payload.categories),
                "langs": list(payload.langs),
            },
        )
        arrays = {
            schema.FACTORS: payload.factors.astype(np.float32, copy=False),
            schema.I2I_INDEX: payload.i2i_index.astype(np.int32, copy=False),
            schema.I2I_SIM: payload.i2i_sim.astype(np.float32, copy=False),
            schema.POPULARITY: payload.popularity.astype(np.float32, copy=False),
            schema.TREND: payload.trend.astype(np.float32, copy=False),
        }
        for name, array in arrays.items():
            np.save(staging / name, np.ascontiguousarray(array), allow_pickle=False)
        if payload.reference is not None:
            _write_json(staging / schema.REFERENCE, payload.reference)

        files = {
            path.name: {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            for path in sorted(staging.iterdir())
        }
        manifest = {
            **payload.extra_manifest,
            "schema": schema.ARTIFACT_SCHEMA,
            "engine_version": schema.ENGINE_VERSION,
            "engine_min_version": schema.ENGINE_VERSION,
            "model_version": version,
            "config": payload.config.to_dict(),
            "config_hash": payload.config.config_hash(),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "n_items": len(payload.item_ids),
            "k": int(payload.factors.shape[1]),
            "i2i_k": int(payload.i2i_index.shape[1]),
            "files": files,
        }
        _write_json(staging / schema.MANIFEST, manifest)
        (staging / schema.SUCCESS_MARKER).write_text("")
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final
