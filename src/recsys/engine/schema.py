"""Constants shared by the artifact writer, the loader and the serving layer."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

ENGINE_VERSION: Final = "1.0.0"
ARTIFACT_SCHEMA: Final = 1

# Artifact files. ``_SUCCESS`` is written last: no marker, no artifact.
SUCCESS_MARKER: Final = "_SUCCESS"
MANIFEST: Final = "manifest.json"
ITEMS: Final = "items.json"
FACTORS: Final = "item_factors.npy"
I2I_INDEX: Final = "i2i_index.npy"
I2I_SIM: Final = "i2i_sim.npy"
POPULARITY: Final = "popularity.npy"
TREND: Final = "trend.npy"
REFERENCE: Final = "reference.json"

# Serving control file, one per app, next to the version directories.
STATE_FILE: Final = "active.json"

MAX_RECENT: Final = 30
MAX_SKIP: Final = 100
MAX_ONBOARDING: Final = 10
MAX_LIMIT: Final = 200
DEFAULT_LIMIT: Final = 20


class Arm(StrEnum):
    """Experiment arm a request was assigned to."""

    TREATMENT = "treatment"
    CONTROL = "control"
    HOLDOUT = "holdout"
    SHADOW = "shadow"


class Tier(StrEnum):
    """How much history the caller has."""

    COLD = "cold"
    LUKEWARM = "lukewarm"
    WARM = "warm"


class Source(StrEnum):
    """Which signal put an item into the response."""

    GROUP = "rec_group"
    CONTENT = "rec_content"
    TREND = "rec_trend"
    EXPLORE = "rec_explore"


class Reason(StrEnum):
    """Why a response carries no items. Business errors never become 5xx."""

    BAD_REQUEST = "bad_request"
    NO_ARTIFACT = "no_artifact"
    KILL_SWITCH = "kill_switch"
    CONTROL = "control"
    HOLDOUT = "holdout"
    SHADOW = "shadow"
    OVERLOAD = "overload"
    TIMEOUT = "timeout"
    ERROR = "error"


class Degraded(StrEnum):
    """Why a non-empty response is of reduced quality."""

    DEADLINE = "deadline"
    EMPTY_POOL = "empty_pool"
