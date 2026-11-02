"""Connecting the registry to the MLflow mirror, and checking they agree."""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from recsys.integrations.mlflow_mirror import MlflowMirror
from recsys.registry import SqliteRegistry


def build_mirror(tracking_uri: str | None) -> MlflowMirror | None:
    """Return a mirror when a server is configured and the extra is installed."""
    if not tracking_uri:
        return None
    try:
        client_class = importlib.import_module("mlflow").MlflowClient
    except (ImportError, AttributeError):
        return None
    return MlflowMirror(client_class(tracking_uri))


def _mlflow_version(
    registry: SqliteRegistry, app: str, version: str | None
) -> str | None:
    if version is None:
        return None
    found = registry.get(app, version)
    value = found.data.get("mlflow_model_version") if found else None
    return str(value) if value is not None else None


def attach_mirror(registry: SqliteRegistry, mirror: MlflowMirror) -> None:
    """Make promotions and releases update the MLflow aliases.

    The hooks translate registry versions into MLflow version numbers using the
    ``mlflow_model_version`` recorded at training time.
    """

    def on_promote(app: str, version: str, previous: str | None) -> None:
        mirror.on_promote(
            app,
            _mlflow_version(registry, app, version),
            _mlflow_version(registry, app, previous),
        )

    def on_challenger(app: str, version: str | None) -> None:
        mirror.set_challenger(app, _mlflow_version(registry, app, version))

    registry.set_hooks(on_promote=on_promote, on_challenger=on_challenger)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Result of comparing the registry with the MLflow ``production`` alias.

    Attributes:
        app: App slug.
        expected: MLflow version of the active registry version, if mirrored.
        actual: MLflow version the ``production`` alias points at.
    """

    app: str
    expected: str | None
    actual: str | None

    @property
    def in_sync(self) -> bool:
        """``True`` when both sides agree (including "nothing mirrored yet")."""
        return self.expected == self.actual


def reconcile(
    registry: SqliteRegistry, mirror: MlflowMirror, app: str
) -> Reconciliation:
    """Compare the registry's active version with the mirror's ``production`` alias."""
    active = registry.active_version(app)
    return Reconciliation(
        app=app,
        expected=_mlflow_version(registry, app, active),
        actual=mirror.alias_version(app, "production"),
    )
