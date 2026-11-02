"""Typed, documented environment configuration.

Per-app model configuration lives in :class:`recsys.engine.EngineConfig`; this
class only holds process-level knobs read from ``RECSYS_*`` variables.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Process settings. Secrets are ``SecretStr`` so they never reach logs."""

    model_config = SettingsConfigDict(
        env_prefix="RECSYS_", extra="ignore", populate_by_name=True
    )

    artifact_dir: Path = Path("artifacts/serving")
    registry_db: Path = Path("artifacts/registry.sqlite3")
    apps: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["demo"])

    log_json: bool = False
    log_level: str = "INFO"
    metrics_enabled: bool = True

    # Serving. REC_MAX_INFLIGHT keeps its historical, unprefixed name.
    max_inflight: int = Field(
        2,
        ge=1,
        validation_alias=AliasChoices("REC_MAX_INFLIGHT", "RECSYS_MAX_INFLIGHT"),
    )
    engine_deadline_s: float = Field(0.03, gt=0)
    hard_timeout_s: float = Field(0.05, gt=0)
    refresh_interval_s: float = Field(60.0, gt=0)
    refresh_jitter_s: float = Field(120.0, ge=0)
    verify_artifacts: bool = True
    served_log_path: Path | None = None
    served_queue_size: int = Field(10_000, ge=1)

    # Provenance written to every manifest.
    code_commit: str | None = None
    image_digest: str = "local"

    mlflow_tracking_uri: str | None = Field(
        None,
        validation_alias=AliasChoices(
            "MLFLOW_TRACKING_URI", "RECSYS_MLFLOW_TRACKING_URI"
        ),
    )
    registry_dsn: SecretStr | None = None
    pushgateway_url: str | None = None

    @field_validator("apps", mode="before")
    @classmethod
    def _split_apps(cls, value: object) -> object:
        """Accept ``RECSYS_APPS=a,b`` as well as a real list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``cache_clear`` in tests)."""
    return Settings()
