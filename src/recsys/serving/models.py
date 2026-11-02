"""Pydantic models used only to document the response in OpenAPI.

Requests are *not* validated by pydantic: the v1 contract never returns 4xx, so
validation lives in :func:`recsys.engine.request.parse_request`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ItemModel(BaseModel):
    """One recommended item."""

    id: str
    score: float
    src: str


class RecResponse(BaseModel):
    """Response of ``POST /v1/rec``. Always HTTP 200."""

    model_config = ConfigDict(protected_namespaces=())

    request_id: str
    items: list[ItemModel]
    model_version: str | None = None
    arm: str | None = None
    tier: str | None = None
    reason: str | None = None
    degraded: str | None = None
