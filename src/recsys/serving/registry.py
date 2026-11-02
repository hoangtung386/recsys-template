"""Serving-side view of the registry: ``active.json`` plus the bundles it names.

Refreshing happens on a background thread. A request only ever reads a
``Snapshot`` reference; swapping that reference is atomic, so requests never
wait on disk or hashing.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from recsys.engine.bundle import Bundle, load_bundle
from recsys.engine.errors import ArtifactError
from recsys.engine.schema import STATE_FILE
from recsys.engine.state import ServingState
from recsys.serving.metrics import Metrics

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Snapshot:
    """An immutable view of what to serve.

    Attributes:
        state: Controls read from ``active.json``.
        bundles: Loaded bundles by version (active, canary and shadow).
        loaded_at: ``time.monotonic()`` of the refresh that produced it.
    """

    state: ServingState
    bundles: Mapping[str, Bundle]
    loaded_at: float

    @property
    def active(self) -> Bundle | None:
        """Bundle of the active version, if it is loaded."""
        return self.bundles.get(self.state.version) if self.state.version else None


class RecRegistry:
    """Keeps one app's :class:`Snapshot` fresh."""

    def __init__(
        self,
        root: Path,
        app: str,
        *,
        interval_s: float = 60.0,
        jitter_s: float = 0.0,
        verify: bool = True,
        metrics: Metrics | None = None,
    ) -> None:
        """Configure the registry; call :meth:`start` to begin refreshing.

        Args:
            root: Artifact root holding ``<app>/active.json`` and version dirs.
            app: App slug.
            interval_s: Seconds between refreshes.
            jitter_s: Random extra delay so pods do not refresh in lockstep.
            verify: Re-hash artifacts on load.
            metrics: Where to report load failures, age and served version.
        """
        self._root = root
        self.app = app
        self._interval = interval_s
        self._jitter = jitter_s
        self._verify = verify
        self._metrics = metrics
        self._snapshot: Snapshot | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._version_label: str | None = None
        if metrics:
            metrics.registry_age.labels(app).set_function(self._age_seconds)

    @property
    def snapshot(self) -> Snapshot | None:
        """The current snapshot; reading it needs no lock."""
        return self._snapshot

    @property
    def ready(self) -> bool:
        """``True`` once the active bundle is loaded."""
        snap = self._snapshot
        return snap is not None and snap.active is not None

    @property
    def version(self) -> str | None:
        """Active version being served."""
        snap = self._snapshot
        return snap.state.version if snap else None

    def _age_seconds(self) -> float:
        snap = self._snapshot
        return time.monotonic() - snap.loaded_at if snap else float("inf")

    def refresh_once(self) -> bool:
        """Re-read ``active.json`` and load any new bundles.

        Returns:
            ``True`` if a snapshot was installed; ``False`` if there is no state
            yet or the refresh failed (the previous snapshot is kept).
        """
        path = self._root / self.app / STATE_FILE
        if not path.is_file():
            return False
        try:
            state = ServingState.from_json(path.read_text())
            current = self._snapshot.bundles if self._snapshot else {}
            bundles: dict[str, Bundle] = {}
            for version in state.required_versions():
                bundles[version] = current.get(version) or self._load(version)
        except (OSError, ValueError, KeyError, ArtifactError):
            log.exception("registry.refresh_failed", extra={"app": self.app})
            if self._metrics:
                self._metrics.load_failures.labels(self.app).inc()
            return False
        self._snapshot = Snapshot(state, bundles, time.monotonic())
        self._report_version(state.version)
        return True

    def _load(self, version: str) -> Bundle:
        return load_bundle(self._root / self.app / version, verify=self._verify)

    def _report_version(self, version: str | None) -> None:
        if not self._metrics or version == self._version_label:
            return
        if self._version_label is not None:
            self._metrics.version_info.remove(self.app, self._version_label)
        if version is not None:
            self._metrics.version_info.labels(self.app, version).set(1)
        self._version_label = version

    def start(self) -> None:
        """Load once synchronously, then keep refreshing in the background."""
        self.refresh_once()
        self._thread = threading.Thread(
            target=self._loop, name=f"rec-registry-{self.app}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval + random.uniform(0, self._jitter)):  # noqa: S311
            self.refresh_once()
