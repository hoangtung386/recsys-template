"""One-way mirror of the registry into the MLflow Model Registry.

The SQL registry stays the source of truth; MLflow only shows it. Mirror
failures are logged and counted but never propagate: a tracking outage must not
block training or a promotion.
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter
from typing import Any

log = logging.getLogger(__name__)


class MlflowMirror:
    """Mirrors versions and promotion aliases into an MLflow client."""

    def __init__(self, client: Any) -> None:
        """Wrap an ``MlflowClient``-like object."""
        self._client = client
        self.failures: Counter[str] = Counter()

    def register(
        self, app: str, model_version: str, run_id: str | None, pointer: str
    ) -> str | None:
        """Register a version; return its MLflow version number or ``None``."""
        name = f"rec-{app}"
        try:
            with contextlib.suppress(Exception):  # already exists
                self._client.create_registered_model(name)
            created = self._client.create_model_version(
                name=name,
                source=pointer,
                run_id=run_id,
                tags={"rec_model_version": model_version},
            )
            return str(created.version)
        except Exception:
            self._fail("register")
            return None

    def on_promote(
        self, app: str, new_mlflow_version: str | None, old: str | None
    ) -> None:
        """Move the ``production`` / ``previous`` aliases after a promotion."""
        if new_mlflow_version is None:
            return
        name = f"rec-{app}"
        try:
            self._client.set_registered_model_alias(
                name, "production", new_mlflow_version
            )
            if old:
                self._client.set_registered_model_alias(name, "previous", old)
            with contextlib.suppress(Exception):
                self._client.delete_registered_model_alias(name, "challenger")
        except Exception:
            self._fail("promote")

    def set_challenger(self, app: str, mlflow_version: str | None) -> None:
        """Mark the release candidate with the ``challenger`` alias, or clear it."""
        name = f"rec-{app}"
        try:
            if mlflow_version is None:
                with contextlib.suppress(Exception):  # nothing to clear
                    self._client.delete_registered_model_alias(name, "challenger")
            else:
                self._client.set_registered_model_alias(
                    name, "challenger", mlflow_version
                )
        except Exception:
            self._fail("challenger")

    def alias_version(self, app: str, alias: str) -> str | None:
        """Return the MLflow version an alias points at, or ``None``."""
        try:
            found = self._client.get_model_version_by_alias(f"rec-{app}", alias)
        except Exception:
            return None
        return str(found.version)

    def _fail(self, op: str) -> None:
        log.exception("mlflow.mirror_failed", extra={"op": op})
        self.failures[op] += 1
