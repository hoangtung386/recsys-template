"""Exception types raised by the engine."""

from __future__ import annotations


class RequestError(ValueError):
    """The request violates the v1 contract.

    Attributes:
        field: Name of the offending field, used as a metric label.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


class ArtifactError(RuntimeError):
    """An artifact is missing, incomplete, corrupt or incompatible."""


class ArtifactExistsError(ArtifactError):
    """The artifact version has already been published."""
