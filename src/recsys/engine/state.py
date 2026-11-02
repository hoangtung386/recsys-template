"""Serving control state: what the API reads every refresh to decide what to do."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from recsys.engine.bucketing import Experiment


@dataclass(frozen=True, slots=True)
class ServingState:
    """Contents of ``active.json``.

    Attributes:
        app: App slug.
        version: Active model version, ``None`` before the first promotion.
        kill: Kill-switch. When set, every request gets ``reason=kill_switch``.
        blocked: Item ids that must never be served.
        experiment: Traffic split, canary and shadow controls.
        updated_at: ISO-8601 UTC timestamp of the last change.
    """

    app: str
    version: str | None = None
    kill: bool = False
    blocked: frozenset[str] = field(default_factory=frozenset)
    experiment: Experiment = field(default_factory=Experiment)
    updated_at: str = ""

    def required_versions(self) -> frozenset[str]:
        """Versions whose bundles must be loaded to serve this state."""
        wanted = {
            self.version,
            self.experiment.canary_version,
            self.experiment.shadow_version,
        }
        return frozenset(v for v in wanted if v)

    def to_json(self) -> str:
        """Serialise deterministically."""
        payload = {
            "app": self.app,
            "version": self.version,
            "kill": self.kill,
            "blocked": sorted(self.blocked),
            "experiment": self.experiment.to_dict(),
            "updated_at": self.updated_at,
        }
        return json.dumps(payload, sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> ServingState:
        """Parse ``active.json``.

        Raises:
            ValueError: On malformed content.
        """
        data: dict[str, Any] = json.loads(text)
        return cls(
            app=str(data["app"]),
            version=data.get("version"),
            kill=bool(data.get("kill", False)),
            blocked=frozenset(data.get("blocked", ())),
            experiment=Experiment.from_dict(data.get("experiment", {})),
            updated_at=str(data.get("updated_at", "")),
        )
