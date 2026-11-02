"""Experiment tracking. A no-op unless MLflow is installed *and* configured.

Dev machines and CI therefore need no tracking server, and a tracking outage
never fails a training run.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
from collections.abc import Iterator, Mapping
from typing import Any

log = logging.getLogger(__name__)


def _mlflow() -> Any:
    """Import MLflow on first use; it is slow to import and optional (extra)."""
    return importlib.import_module("mlflow")


def flatten_metrics(prefix: str, data: Mapping[str, Any]) -> dict[str, float]:
    """Flatten nested mappings into ``a.b.c -> float``, dropping non-numbers."""
    out: dict[str, float] = {}
    for key, value in data.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(flatten_metrics(name, value))
        elif isinstance(value, int | float) and not isinstance(value, bool):
            out[name] = float(value)
    return out


class Tracker:
    """Handle on one run. Every method does nothing when ``run_id`` is ``None``."""

    def __init__(self, run_id: str | None = None) -> None:
        """Wrap a run id (``None`` for the no-op tracker)."""
        self.run_id = run_id

    def params(self, values: Mapping[str, Any]) -> None:
        """Log parameters (values are stringified and truncated)."""
        self._safely(
            lambda: _mlflow().log_params({k: str(v)[:250] for k, v in values.items()})
        )

    def metrics(self, values: Mapping[str, Any], step: int | None = None) -> None:
        """Log numeric metrics, flattening nested mappings."""
        self._safely(
            lambda: _mlflow().log_metrics(flatten_metrics("", values), step=step)
        )

    def artifact(self, path: str) -> None:
        """Attach a local file to the run."""
        self._safely(lambda: _mlflow().log_artifact(path))

    def _safely(self, call: Any) -> None:
        if not self.run_id:
            return
        try:
            call()
        except Exception:
            log.exception("tracking.call_failed")


@contextlib.contextmanager
def start_run(
    experiment: str,
    name: str,
    tags: Mapping[str, str] | None = None,
    *,
    tracking_uri: str | None = None,
) -> Iterator[Tracker]:
    """Open a tracked run, or yield a no-op :class:`Tracker`.

    Args:
        experiment: Experiment name.
        name: Run name.
        tags: Run tags.
        tracking_uri: MLflow server; ``None`` disables tracking.
    """
    if not tracking_uri:
        yield Tracker(None)
        return
    try:
        mlflow = _mlflow()
        mlflow.set_tracking_uri(tracking_uri)
        mlflow.set_experiment(experiment)
        context = mlflow.start_run(run_name=name, tags=dict(tags or {}))
        active = context.__enter__()
    except Exception:  # missing extra, unreachable server...: train anyway
        log.exception("tracking.start_failed")
        yield Tracker(None)
        return
    try:
        yield Tracker(active.info.run_id)
    except BaseException as exc:
        context.__exit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        context.__exit__(None, None, None)
