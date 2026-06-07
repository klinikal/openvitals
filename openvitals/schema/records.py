"""
OpenVitals Record envelope and per-domain ``common`` models.

Design rules (from CLAUDE.md and record-schema-v1.md):
- ``raw`` is byte-faithful/verbatim — never decoded, renamed, restructured, or
  dropped.  All decoded/normalized values live in the envelope + ``common``.
- Every ``common`` field is nullable — sources don't fill them all.
- Timestamps are canonical UTC ISO-8601.  ``timezone`` is IANA.
- ``local_date`` for day-based records preserves the provider's own attribution.
- Units are in field names: _kg, _minutes, _pct, _bpm, _ms, etc.

v0.1 scope: sleep, activity, recovery, body, nutrition.
DEFERRED: biomarker / lab_result, genetics / genetic_variant.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Union

from pydantic import BaseModel, Field

from openvitals.schema.domains import (
    ActivityType,
    BodySegment,
    CardiovascularMarkerType,
    Meal,
    RecordType,
    ScoreState,
    SetType,
)


# ---------------------------------------------------------------------------
# Micronutrients (canonical set — add-only after v1.0)
# ---------------------------------------------------------------------------


class Micronutrients(BaseModel):
    """
    Canonical micronutrient set.  Every field is nullable — present only when
    the source provides it.  Units are in field names.  The full provider
    nutrient panel always remains in ``raw``.  Add-only after v1.0.
    """

    model_config = {"extra": "forbid"}

    # Vitamins
    vitamin_a_mcg: float | None = None
    vitamin_c_mg: float | None = None
    vitamin_d_mcg: float | None = None
    vitamin_e_mg: float | None = None
    vitamin_k_mcg: float | None = None
    thiamin_b1_mg: float | None = None
    riboflavin_b2_mg: float | None = None
    niacin_b3_mg: float | None = None
    vitamin_b6_mg: float | None = None
    folate_b9_mcg: float | None = None
    vitamin_b12_mcg: float | None = None
    # Minerals
    calcium_mg: float | None = None
    iron_mg: float | None = None
    magnesium_mg: float | None = None
    zinc_mg: float | None = None
    potassium_mg: float | None = None
    sodium_mg: float | None = None
    selenium_mcg: float | None = None
    iodine_mcg: float | None = None
    phosphorus_mg: float | None = None
    # Fats of interest
    omega3_g: float | None = None
    omega6_g: float | None = None
    saturated_fat_g: float | None = None
    cholesterol_mg: float | None = None


# ---------------------------------------------------------------------------
# Nested structures
# ---------------------------------------------------------------------------


class ExerciseSet(BaseModel):
    """One set within an exercise. ``set_order`` is required — order is load-bearing."""

    model_config = {"extra": "forbid"}

    set_order: int = Field(..., description="Position within the exercise. REQUIRED — never lose ordering.")
    set_ref_id: str | None = None
    set_type: SetType = SetType.NORMAL
    weight_kg: float | None = None
    reps: int | None = None
    rpe: float | None = None
    distance_meters: float | None = None
    duration_seconds: float | None = None


class Exercise(BaseModel):
    """One exercise within a workout. ``exercise_order`` is required — order is load-bearing."""

    model_config = {"extra": "forbid"}

    exercise_order: int = Field(..., description="Position in the workout. REQUIRED — never lose ordering.")
    exercise_name: str
    exercise_ref_id: str | None = None
    primary_muscle: str | None = None
    superset_id: str | None = None
    notes: str | None = None
    sets: list[ExerciseSet] = Field(default_factory=list)


class BodySegmentComposition(BaseModel):
    """Composition for one body segment (Withings segmental analysis)."""

    model_config = {"extra": "forbid"}

    segment: BodySegment
    fat_pct: float | None = None
    muscle_mass_kg: float | None = None


# ---------------------------------------------------------------------------
# Domain ``common`` models — one per record_type in v0.1 scope
# ---------------------------------------------------------------------------


class SleepSessionCommon(BaseModel):
    """sleep → sleep_session"""

    model_config = {"extra": "forbid"}

    total_sleep_minutes: int | None = None
    time_in_bed_minutes: int | None = None
    sleep_efficiency_pct: float | None = None
    sleep_latency_minutes: int | None = None
    awakenings_count: int | None = None
    stage_rem_minutes: int | None = None
    stage_deep_minutes: int | None = None
    stage_light_minutes: int | None = None
    stage_awake_minutes: int | None = None
    avg_heart_rate_bpm: int | None = None
    avg_hrv_ms: float | None = None
    respiratory_rate_bpm: float | None = None
    avg_spo2_pct: float | None = None
    provider_sleep_score: int | None = None
    score_state: ScoreState | None = None


class WorkoutCommon(BaseModel):
    """activity → workout (cardio and strength share this type)"""

    model_config = {"extra": "forbid"}

    activity_type: ActivityType | None = None
    activity_label: str | None = None
    duration_minutes: float | None = None
    elapsed_minutes: float | None = None
    distance_meters: float | None = None
    elevation_gain_meters: float | None = None
    avg_heart_rate_bpm: int | None = None
    max_heart_rate_bpm: int | None = None
    energy_kcal: float | None = None
    avg_speed_mps: float | None = None
    avg_power_watts: float | None = None
    total_volume_kg: float | None = None
    set_count: int | None = None
    exercises: list[Exercise] = Field(default_factory=list)
    provider_strain: float | None = None
    score_state: ScoreState | None = None


class DailyRecoveryCommon(BaseModel):
    """recovery → daily_recovery (one per day)"""

    model_config = {"extra": "forbid"}

    hrv_ms: float | None = None
    resting_heart_rate_bpm: int | None = None
    provider_recovery_score: int | None = None
    avg_spo2_pct: float | None = None
    skin_temp_celsius: float | None = None
    respiratory_rate_bpm: float | None = None
    score_state: ScoreState | None = None


class BodyMeasurementCommon(BaseModel):
    """body → body_measurement"""

    model_config = {"extra": "forbid"}

    weight_kg: float | None = None
    body_fat_pct: float | None = None
    fat_mass_kg: float | None = None
    lean_mass_kg: float | None = None
    muscle_mass_kg: float | None = None
    bone_mass_kg: float | None = None
    body_water_pct: float | None = None
    visceral_fat_index: float | None = None
    bmi: float | None = None


class SegmentalCompositionCommon(BaseModel):
    """body → segmental_composition (per-limb composition from Withings)"""

    model_config = {"extra": "forbid"}

    segments: list[BodySegmentComposition] = Field(default_factory=list)


class CardiovascularMarkerCommon(BaseModel):
    """body → cardiovascular_marker (device-measured cardiovascular metrics)"""

    model_config = {"extra": "forbid"}

    marker_type: CardiovascularMarkerType | None = None
    value: float | str | None = None
    unit: str | None = None


class FoodEntryCommon(BaseModel):
    """nutrition → food_entry (one per logged food item in the diary)"""

    model_config = {"extra": "forbid"}

    food_name: str | None = None
    food_ref_id: str | None = None
    food_tags: list[str] = Field(default_factory=list)
    food_category: str | None = None
    food_source: str | None = None
    brand: str | None = None
    serving_amount: float | None = None
    serving_unit: str | None = None
    meal: Meal | None = None
    energy_kcal: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    sugar_g: float | None = None
    alcohol_g: float | None = None
    micronutrients: Micronutrients | None = None


class DailySummaryCommon(BaseModel):
    """nutrition → daily_summary (one per day)"""

    model_config = {"extra": "forbid"}

    energy_kcal: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    sugar_g: float | None = None
    alcohol_g: float | None = None
    energy_target_kcal: float | None = None
    micronutrients: Micronutrients | None = None


class FoodDefinitionCommon(BaseModel):
    """
    nutrition → food_definition (reference food data, not a diary event).
    Reference-style, account-scoped — may include personal custom foods.
    """

    model_config = {"extra": "forbid"}

    food_ref_id: str | None = None
    food_name: str | None = None
    food_category: str | None = None
    food_source: str | None = None
    food_tags: list[str] = Field(default_factory=list)
    barcode: str | None = None
    energy_kcal: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    reference_serving_amount: float | None = None
    reference_serving_unit: str | None = None
    micronutrients: Micronutrients | None = None


# ---------------------------------------------------------------------------
# Discriminated union for ``common``
# ---------------------------------------------------------------------------

CommonModel = Union[
    SleepSessionCommon,
    WorkoutCommon,
    DailyRecoveryCommon,
    BodyMeasurementCommon,
    SegmentalCompositionCommon,
    CardiovascularMarkerCommon,
    FoodEntryCommon,
    DailySummaryCommon,
    FoodDefinitionCommon,
]

# Map record_type → common model class (used by connector authors + tests)
RECORD_TYPE_TO_COMMON: dict[RecordType, type[BaseModel]] = {
    RecordType.SLEEP_SESSION: SleepSessionCommon,
    RecordType.WORKOUT: WorkoutCommon,
    RecordType.DAILY_RECOVERY: DailyRecoveryCommon,
    RecordType.BODY_MEASUREMENT: BodyMeasurementCommon,
    RecordType.SEGMENTAL_COMPOSITION: SegmentalCompositionCommon,
    RecordType.CARDIOVASCULAR_MARKER: CardiovascularMarkerCommon,
    RecordType.FOOD_ENTRY: FoodEntryCommon,
    RecordType.DAILY_SUMMARY: DailySummaryCommon,
    RecordType.FOOD_DEFINITION: FoodDefinitionCommon,
}


# ---------------------------------------------------------------------------
# The Record envelope
# ---------------------------------------------------------------------------


class Record(BaseModel):
    """
    The OpenVitals Record envelope.

    ``raw`` carries the verbatim provider payload — never decoded, renamed,
    restructured, or dropped.  All decoded/normalized values live in ``common``
    or the envelope fields.

    ``source_record_id`` identity rules (from connector-contract.md §2):
    - Provider-native ID exists → use it verbatim.
    - Daily/aggregate records → compound ``account_ref:local_date:record_type``.
    - Derived-from-group → ``<measure_id>:<marker_type>`` etc.
    Dedup/upsert key: ``source + account_ref + record_type + source_record_id``.
    """

    model_config = {"extra": "forbid"}

    # Schema version — always "0.1" for v0.1 records
    schema_version: str = Field(default="0.1", description="OpenVitals schema version.")

    # Provenance
    source: str = Field(..., description="Provider slug, e.g. 'whoop', 'strava', 'withings'.")
    domain: str = Field(..., description="Domain slug, e.g. 'sleep', 'activity'. Use Domain enum values.")
    record_type: RecordType

    # Identity
    source_record_id: str = Field(
        ...,
        description=(
            "Stable ID for this record from the provider. "
            "Daily records use compound 'account_ref:local_date:record_type'. "
            "Must be stable across re-fetches."
        ),
    )
    account_ref: str = Field(..., description="Opaque account identifier (provider-scoped user ID).")

    # Timestamps — all UTC ISO-8601
    occurred_at: datetime = Field(..., description="Event start instant, UTC.")
    occurred_end: datetime | None = Field(None, description="Session end instant, UTC (if known).")
    ingested_at: datetime = Field(..., description="When OpenVitals fetched this record, UTC.")

    # Local time context
    timezone: str | None = Field(
        None,
        description="IANA timezone (e.g. 'Europe/London') — lets consumers reconstruct local time exactly.",
    )
    local_date: date | None = Field(
        None,
        description=(
            "Calendar date the provider attributes this record to (day-based records). "
            "UTC alone can't hold this safely for records that cross midnight."
        ),
    )

    # Payload
    common: CommonModel = Field(
        ...,
        description="Typed per-record_type model. All fields nullable — sources vary.",
    )
    raw: dict[str, Any] = Field(
        ...,
        description=(
            "Verbatim provider payload — byte-faithful, never decoded or restructured. "
            "The no-data-loss snapshot for debugging. All decoding lives in common."
        ),
    )
    source_extras: dict[str, Any] | None = Field(
        None,
        description=(
            "Provider-native headline values that don't generalize across sources. "
            "Must be documented per connector. Not a junk drawer."
        ),
    )
