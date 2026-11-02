"""Destinations for ``rec.served`` records. Sinks never block a request."""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

_STOP = object()


class ServedSink(Protocol):
    """Receives one record per served request."""

    def send(self, message: dict[str, Any]) -> bool:
        """Enqueue ``message``; return ``False`` if it was dropped. Never blocks."""
        ...

    def close(self) -> None:
        """Flush pending records and release resources."""
        ...


class NullSink:
    """Discards everything."""

    def send(self, message: dict[str, Any]) -> bool:  # noqa: ARG002
        """Accept and drop ``message``."""
        return True

    def close(self) -> None:
        """Nothing to release."""


class MemorySink:
    """Keeps records in a list. For tests."""

    def __init__(self) -> None:
        """Start empty."""
        self.messages: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def send(self, message: dict[str, Any]) -> bool:
        """Store ``message``."""
        with self._lock:
            self.messages.append(message)
        return True

    def close(self) -> None:
        """Nothing to release."""


class JsonlSink:
    """Appends records to a JSON-lines file from a background thread.

    The queue is bounded: when the writer cannot keep up, new records are
    dropped and counted rather than slowing the request path.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_queue: int = 10_000,
        on_drop: Callable[[], None] | None = None,
    ) -> None:
        """Open ``path`` for appending and start the writer thread."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max_queue)
        self._on_drop = on_drop
        self._path = path
        self._thread = threading.Thread(
            target=self._drain, name="served-sink", daemon=True
        )
        self._thread.start()

    def send(self, message: dict[str, Any]) -> bool:
        """Enqueue ``message`` without blocking."""
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            if self._on_drop:
                self._on_drop()
            return False
        return True

    def close(self) -> None:
        """Wait for the queue to drain, then stop the writer."""
        self._queue.put(_STOP)
        self._thread.join(timeout=10)

    def _drain(self) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            while (item := self._queue.get()) is not _STOP:
                try:
                    handle.write(json.dumps(item, separators=(",", ":")) + "\n")
                    if self._queue.empty():
                        handle.flush()
                except Exception:
                    log.exception("sink.write_failed")
            handle.flush()
