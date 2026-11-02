"""Loading and verifying a model artifact into an immutable :class:`Bundle`."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
from numpy.typing import NDArray

from recsys.engine import schema
from recsys.engine.config import EngineConfig
from recsys.engine.errors import ArtifactError

if TYPE_CHECKING:
    from recsys.engine.writer import ArtifactPayload

_HASH_CHUNK = 1 << 20


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


@dataclass(frozen=True, slots=True)
class Bundle:
    """All arrays and metadata needed to serve one model version.

    Arrays are memory-mapped read-only when loaded from disk, so several
    processes on one host share the page cache.
    """

    version: str
    manifest: Mapping[str, Any]
    config: EngineConfig
    item_ids: tuple[str, ...]
    index: Mapping[str, int]
    category: NDArray[np.int16]
    categories: tuple[str, ...]
    lang: NDArray[np.int16]
    langs: tuple[str, ...]
    factors: NDArray[np.float32]
    i2i_index: NDArray[np.int32]
    i2i_sim: NDArray[np.float32]
    popularity: NDArray[np.float32]
    trend: NDArray[np.float32]

    @property
    def n_items(self) -> int:
        """Catalog size."""
        return len(self.item_ids)

    @property
    def config_hash(self) -> str:
        """Digest of the engine configuration stored in the manifest."""
        return str(self.manifest.get("config_hash", self.config.config_hash()))


def bundle_from_payload(payload: ArtifactPayload, version: str = "candidate") -> Bundle:
    """Build an in-memory :class:`Bundle` without touching the disk.

    Lets the training job backtest a model with the very code that will serve it
    before deciding whether to publish it.
    """
    payload.validate()
    categories = tuple(sorted(set(payload.categories)))
    langs = tuple(sorted(set(payload.langs)))
    category_lookup = {name: i for i, name in enumerate(categories)}
    lang_lookup = {name: i for i, name in enumerate(langs)}
    ids = tuple(payload.item_ids)
    return Bundle(
        version=version,
        manifest={
            "model_version": version,
            "config_hash": payload.config.config_hash(),
        },
        config=payload.config,
        item_ids=ids,
        index={item_id: i for i, item_id in enumerate(ids)},
        category=np.array([category_lookup[c] for c in payload.categories], np.int16),
        categories=categories,
        lang=np.array([lang_lookup[v] for v in payload.langs], np.int16),
        langs=langs,
        factors=payload.factors.astype(np.float32, copy=False),
        i2i_index=payload.i2i_index.astype(np.int32, copy=False),
        i2i_sim=payload.i2i_sim.astype(np.float32, copy=False),
        popularity=payload.popularity.astype(np.float32, copy=False),
        trend=payload.trend.astype(np.float32, copy=False),
    )


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, streaming it."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(root: Path) -> dict[str, Any]:
    """Read and sanity-check ``manifest.json``.

    Raises:
        ArtifactError: If the artifact is incomplete or incompatible.
    """
    if not (root / schema.SUCCESS_MARKER).is_file():
        raise ArtifactError(f"{root}: missing {schema.SUCCESS_MARKER}")
    try:
        manifest: dict[str, Any] = json.loads((root / schema.MANIFEST).read_text())
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"{root}: unreadable manifest") from exc
    if manifest.get("schema") != schema.ARTIFACT_SCHEMA:
        raise ArtifactError(f"{root}: unsupported schema {manifest.get('schema')}")
    required = manifest.get("engine_min_version", schema.ENGINE_VERSION)
    if _version_tuple(required) > _version_tuple(schema.ENGINE_VERSION):
        raise ArtifactError(f"{root}: needs engine >= {required}")
    return manifest


def verify_artifact(root: Path, manifest: Mapping[str, Any] | None = None) -> None:
    """Check size and SHA-256 of every file listed in the manifest.

    Raises:
        ArtifactError: On a missing file or a digest mismatch.
    """
    manifest = manifest or read_manifest(root)
    for name, meta in manifest["files"].items():
        path = root / name
        if not path.is_file():
            raise ArtifactError(f"{root}: missing file {name}")
        if path.stat().st_size != meta["bytes"] or sha256_file(path) != meta["sha256"]:
            raise ArtifactError(f"{root}: {name} does not match the manifest")


def load_bundle(root: Path, *, verify: bool = True, mmap: bool = True) -> Bundle:
    """Load an artifact directory.

    Args:
        root: Directory of one published model version.
        verify: Re-hash every file against the manifest.
        mmap: Memory-map the arrays instead of reading them into the heap.

    Raises:
        ArtifactError: If the artifact is missing, corrupt or incompatible.
    """
    manifest = read_manifest(root)
    if verify:
        verify_artifact(root, manifest)
    mode: Literal["r"] | None = "r" if mmap else None

    def array(name: str) -> Any:
        return np.load(root / name, mmap_mode=mode, allow_pickle=False)

    items = json.loads((root / schema.ITEMS).read_text())
    ids = tuple(items["ids"])
    categories = tuple(sorted(set(items["categories"])))
    langs = tuple(sorted(set(items["langs"])))
    category_lookup = {name: i for i, name in enumerate(categories)}
    lang_lookup = {name: i for i, name in enumerate(langs)}
    return Bundle(
        version=str(manifest["model_version"]),
        manifest=manifest,
        config=EngineConfig.from_dict(manifest["config"]),
        item_ids=ids,
        index={item_id: i for i, item_id in enumerate(ids)},
        category=np.array([category_lookup[c] for c in items["categories"]], np.int16),
        categories=categories,
        lang=np.array([lang_lookup[v] for v in items["langs"]], np.int16),
        langs=langs,
        factors=array(schema.FACTORS),
        i2i_index=array(schema.I2I_INDEX),
        i2i_sim=array(schema.I2I_SIM),
        popularity=array(schema.POPULARITY),
        trend=array(schema.TREND),
    )
