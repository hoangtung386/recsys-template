"""Logging setup: structured JSON in production, readable text in development.

``structlog`` is optional. Without it the process still logs, as plain text.
Existing ``logger.info("msg", extra={...})`` calls keep working unchanged.
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import Any


def _load_structlog() -> Any:
    """Return the ``structlog`` module, or ``None`` when the extra is absent."""
    try:
        return importlib.import_module("structlog")
    except ImportError:  # pragma: no cover - depends on the environment
        return None


structlog: Any = _load_structlog()

_TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def build_formatter(*, json_output: bool) -> logging.Formatter:
    """Return the formatter for the root handler."""
    if structlog is None:
        return logging.Formatter(_TEXT_FORMAT)
    shared: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    formatter: logging.Formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    return formatter


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    """Install one stderr handler on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(build_formatter(json_output=json_output))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def bind_context(**values: Any) -> None:
    """Attach fields (``app``, ``model_version``...) to every following log line."""
    if structlog is not None:
        structlog.contextvars.bind_contextvars(**values)
