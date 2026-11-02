"""Serving engine. Depends on numpy and the standard library only.

Anything that needs another third-party package belongs outside this package;
``tests/test_layering.py`` enforces the rule.
"""

from recsys.engine.bucketing import Assignment, Experiment, assign_arm
from recsys.engine.bundle import (
    Bundle,
    bundle_from_payload,
    load_bundle,
    verify_artifact,
)
from recsys.engine.config import EngineConfig, TierWeights
from recsys.engine.context import Context, build_context, context_from_request
from recsys.engine.errors import (
    ArtifactError,
    ArtifactExistsError,
    RequestError,
)
from recsys.engine.recommend import recommend
from recsys.engine.request import RecRequest, parse_request, served_message
from recsys.engine.result import RecItem, RecResult
from recsys.engine.schema import ENGINE_VERSION, Arm, Degraded, Reason, Source, Tier
from recsys.engine.state import ServingState
from recsys.engine.writer import ArtifactPayload, write_artifact

__all__ = [
    "ENGINE_VERSION",
    "Arm",
    "ArtifactError",
    "ArtifactExistsError",
    "ArtifactPayload",
    "Assignment",
    "Bundle",
    "Context",
    "Degraded",
    "EngineConfig",
    "Experiment",
    "Reason",
    "RecItem",
    "RecRequest",
    "RecResult",
    "RequestError",
    "ServingState",
    "Source",
    "Tier",
    "TierWeights",
    "assign_arm",
    "build_context",
    "bundle_from_payload",
    "context_from_request",
    "load_bundle",
    "parse_request",
    "recommend",
    "served_message",
    "verify_artifact",
    "write_artifact",
]
