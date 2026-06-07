"""
Controlled vocabularies for the OpenVitals schema.

All enums are add-only-extensible: once a value is published, it is never
removed or renamed. Connectors map provider-native strings to these values
and preserve the original in an accompanying ``*_label`` field.
"""

from enum import Enum


class Domain(str, Enum):
    """Top-level data domain — corresponds to a provider category."""

    SLEEP = "sleep"
    ACTIVITY = "activity"
    RECOVERY = "recovery"
    BODY = "body"
    NUTRITION = "nutrition"
    # BIOMARKER and GENETICS are deferred from v0.1 — shapes provisional.
    # BIOMARKER = "biomarker"
    # GENETICS = "genetics"


class RecordType(str, Enum):
    """
    The specific kind of record within a domain.
    One ``record_type`` maps to exactly one ``common`` model.
    """

    # sleep
    SLEEP_SESSION = "sleep_session"
    # activity
    WORKOUT = "workout"
    # recovery
    DAILY_RECOVERY = "daily_recovery"
    # body
    BODY_MEASUREMENT = "body_measurement"
    SEGMENTAL_COMPOSITION = "segmental_composition"
    CARDIOVASCULAR_MARKER = "cardiovascular_marker"
    # nutrition
    FOOD_ENTRY = "food_entry"
    DAILY_SUMMARY = "daily_summary"
    FOOD_DEFINITION = "food_definition"
    # Deferred from v0.1:
    # LAB_RESULT = "lab_result"
    # GENETIC_VARIANT = "genetic_variant"


# ---------------------------------------------------------------------------
# Per-field controlled vocabularies
# ---------------------------------------------------------------------------


class ActivityType(str, Enum):
    """
    Normalized activity type for ``workout`` records.
    Connector maps provider-native sport string here; original is in
    ``activity_label``. Add-only after v1.0.
    """

    RUN = "run"
    RIDE = "ride"
    SWIM = "swim"
    WALK = "walk"
    HIKE = "hike"
    STRENGTH = "strength"
    CARDIO = "cardio"
    SAUNA = "sauna"
    OTHER = "other"


class SetType(str, Enum):
    """Set category within a strength exercise."""

    NORMAL = "normal"
    WARMUP = "warmup"
    DROPSET = "dropset"
    FAILURE = "failure"


class Meal(str, Enum):
    """Meal slot for a food diary entry."""

    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"
    UNCATEGORIZED = "uncategorized"


class BodySegment(str, Enum):
    """Body segment for segmental composition records (Withings)."""

    LEFT_ARM = "left_arm"
    RIGHT_ARM = "right_arm"
    TRUNK = "trunk"
    LEFT_LEG = "left_leg"
    RIGHT_LEG = "right_leg"


class CardiovascularMarkerType(str, Enum):
    """Cardiovascular marker type for device-measured metrics (Withings)."""

    VASCULAR_AGE = "vascular_age"
    PULSE_WAVE_VELOCITY = "pulse_wave_velocity"
    ECG_RESULT = "ecg_result"
    NERVE_HEALTH_SCORE = "nerve_health_score"


class ScoreState(str, Enum):
    """
    Provider's data-completeness state. Surfaced verbatim, never filtered.
    Consumers decide whether to use or ignore non-scored records.
    """

    SCORED = "scored"
    UNSCORED = "unscored"
    PENDING = "pending"
