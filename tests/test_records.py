"""
Tests for the Record envelope and all v0.1 common models.

Coverage:
- A Record for each in-scope record_type validates (minimal valid instance).
- ``raw`` round-trips untouched (no-data-loss helper).
- Ordering fields on exercises/sets are required.
- Deferred record types (lab_result, genetic_variant) are NOT in RecordType.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from openvitals.schema.domains import (
    ActivityType,
    BodySegment,
    CardiovascularMarkerType,
    Meal,
    RecordType,
    ScoreState,
    SetType,
)
from openvitals.schema.records import (
    BodyMeasurementCommon,
    BodySegmentComposition,
    CardiovascularMarkerCommon,
    DailyRecoveryCommon,
    DailySummaryCommon,
    Exercise,
    ExerciseSet,
    FoodDefinitionCommon,
    FoodEntryCommon,
    Micronutrients,
    Record,
    RECORD_TYPE_TO_COMMON,
    SegmentalCompositionCommon,
    SleepSessionCommon,
    WorkoutCommon,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_envelope(record_type: RecordType, common, domain: str, raw: dict | None = None) -> Record:
    return Record(
        source="test",
        domain=domain,
        record_type=record_type,
        source_record_id="rid_001",
        account_ref="acct_001",
        occurred_at=datetime(2026, 6, 6, 8, 0, 0, tzinfo=UTC),
        ingested_at=datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC),
        common=common,
        raw=raw if raw is not None else {"provider_field": "value"},
    )


# ---------------------------------------------------------------------------
# Record envelope basics
# ---------------------------------------------------------------------------


class TestRecordEnvelope:
    def test_schema_version_default(self):
        r = make_envelope(RecordType.SLEEP_SESSION, SleepSessionCommon(), "sleep")
        assert r.schema_version == "0.1"

    def test_optional_fields_default_none(self):
        r = make_envelope(RecordType.SLEEP_SESSION, SleepSessionCommon(), "sleep")
        assert r.occurred_end is None
        assert r.timezone is None
        assert r.local_date is None
        assert r.source_extras is None

    def test_local_date_accepted(self):
        r = make_envelope(RecordType.DAILY_RECOVERY, DailyRecoveryCommon(), "recovery")
        # Patch in a local_date via model_copy
        r2 = r.model_copy(update={"local_date": date(2026, 6, 6)})
        assert r2.local_date == date(2026, 6, 6)

    def test_extra_fields_rejected(self):
        """The envelope must not silently swallow unknown fields."""
        with pytest.raises(ValidationError):
            Record(
                source="test",
                domain="sleep",
                record_type=RecordType.SLEEP_SESSION,
                source_record_id="x",
                account_ref="acct",
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
                common=SleepSessionCommon(),
                raw={},
                unknown_extra_field="oops",  # should be rejected
            )

    def test_raw_round_trip(self):
        """``raw`` must be stored verbatim — no decoding, renaming, or dropping."""
        provider_payload = {
            "id": "abc123",
            "score": {"stage_summary": {"total_slow_wave_sleep_time_milli": 7200000}},
            "nested": {"deeply": {"buried": [1, 2, 3]}},
        }
        r = make_envelope(RecordType.SLEEP_SESSION, SleepSessionCommon(), "sleep", raw=provider_payload)
        assert r.raw == provider_payload
        # Serialise and deserialise — round-trip
        r2 = Record.model_validate(r.model_dump())
        assert r2.raw == provider_payload

    def test_raw_arbitrary_dict_accepted(self):
        """``raw`` accepts any dict — the schema places no structural constraint on it."""
        payload = {"x": [1, None, {"y": True}], "z": 3.14}
        r = make_envelope(RecordType.WORKOUT, WorkoutCommon(), "activity", raw=payload)
        assert r.raw["x"][2]["y"] is True


# ---------------------------------------------------------------------------
# sleep → sleep_session
# ---------------------------------------------------------------------------


class TestSleepSession:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.SLEEP_SESSION, SleepSessionCommon(), "sleep")
        assert r.record_type == RecordType.SLEEP_SESSION

    def test_all_fields(self):
        common = SleepSessionCommon(
            total_sleep_minutes=420,
            time_in_bed_minutes=480,
            sleep_efficiency_pct=87.5,
            sleep_latency_minutes=10,
            awakenings_count=3,
            stage_rem_minutes=90,
            stage_deep_minutes=75,
            stage_light_minutes=255,
            stage_awake_minutes=60,
            avg_heart_rate_bpm=54,
            avg_hrv_ms=68.3,
            respiratory_rate_bpm=15.2,
            avg_spo2_pct=97.1,
            provider_sleep_score=82,
            score_state=ScoreState.SCORED,
        )
        r = make_envelope(RecordType.SLEEP_SESSION, common, "sleep")
        assert r.common.total_sleep_minutes == 420
        assert r.common.score_state == ScoreState.SCORED

    def test_all_nullable(self):
        common = SleepSessionCommon()
        assert common.total_sleep_minutes is None
        assert common.score_state is None

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            SleepSessionCommon(nonexistent_field=1)


# ---------------------------------------------------------------------------
# activity → workout (incl. exercises/sets)
# ---------------------------------------------------------------------------


class TestWorkout:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.WORKOUT, WorkoutCommon(), "activity")
        assert r.record_type == RecordType.WORKOUT

    def test_cardio_fields(self):
        common = WorkoutCommon(
            activity_type=ActivityType.RUN,
            activity_label="TrailRun",
            duration_minutes=62.5,
            distance_meters=10500.0,
            avg_heart_rate_bpm=148,
            energy_kcal=620.0,
        )
        r = make_envelope(RecordType.WORKOUT, common, "activity")
        assert r.common.activity_type == ActivityType.RUN
        assert r.common.distance_meters == 10500.0

    def test_strength_exercises(self):
        sets = [
            ExerciseSet(set_order=1, set_type=SetType.WARMUP, weight_kg=60.0, reps=5),
            ExerciseSet(set_order=2, set_type=SetType.NORMAL, weight_kg=100.0, reps=3, rpe=8.5),
            ExerciseSet(set_order=3, set_type=SetType.NORMAL, weight_kg=100.0, reps=3),
        ]
        exercises = [
            Exercise(
                exercise_order=1,
                exercise_name="Back Squat",
                exercise_ref_id="ex_001",
                primary_muscle="quadriceps",
                sets=sets,
            )
        ]
        common = WorkoutCommon(
            activity_type=ActivityType.STRENGTH,
            total_volume_kg=2400.0,
            set_count=3,
            exercises=exercises,
        )
        r = make_envelope(RecordType.WORKOUT, common, "activity")
        assert len(r.common.exercises) == 1
        assert r.common.exercises[0].exercise_order == 1
        assert r.common.exercises[0].sets[1].weight_kg == 100.0
        assert r.common.exercises[0].sets[1].rpe == 8.5

    def test_exercise_order_required(self):
        with pytest.raises(ValidationError):
            Exercise(exercise_name="Squat", sets=[])  # missing exercise_order

    def test_set_order_required(self):
        with pytest.raises(ValidationError):
            ExerciseSet(set_type=SetType.NORMAL, weight_kg=80.0, reps=5)  # missing set_order

    def test_sauna_activity_type(self):
        """Wellness activities like sauna must be emittable — never dropped."""
        common = WorkoutCommon(activity_type=ActivityType.SAUNA, activity_label="Sauna")
        assert common.activity_type == ActivityType.SAUNA

    def test_score_state_surfaced(self):
        """score_state is surfaced, never filtered."""
        common = WorkoutCommon(score_state=ScoreState.UNSCORED)
        assert common.score_state == ScoreState.UNSCORED


# ---------------------------------------------------------------------------
# recovery → daily_recovery
# ---------------------------------------------------------------------------


class TestDailyRecovery:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.DAILY_RECOVERY, DailyRecoveryCommon(), "recovery")
        assert r.record_type == RecordType.DAILY_RECOVERY

    def test_all_fields(self):
        common = DailyRecoveryCommon(
            hrv_ms=72.4,
            resting_heart_rate_bpm=52,
            provider_recovery_score=85,
            avg_spo2_pct=97.5,
            skin_temp_celsius=36.8,
            respiratory_rate_bpm=14.8,
            score_state=ScoreState.SCORED,
        )
        assert common.provider_recovery_score == 85


# ---------------------------------------------------------------------------
# body → body_measurement
# ---------------------------------------------------------------------------


class TestBodyMeasurement:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.BODY_MEASUREMENT, BodyMeasurementCommon(), "body")
        assert r.record_type == RecordType.BODY_MEASUREMENT

    def test_all_fields(self):
        common = BodyMeasurementCommon(
            weight_kg=82.3,
            body_fat_pct=18.5,
            fat_mass_kg=15.2,
            lean_mass_kg=67.1,
            muscle_mass_kg=62.0,
            bone_mass_kg=3.4,
            body_water_pct=58.2,
            visceral_fat_index=7.0,
            bmi=24.1,
        )
        assert common.weight_kg == 82.3


# ---------------------------------------------------------------------------
# body → segmental_composition
# ---------------------------------------------------------------------------


class TestSegmentalComposition:
    def test_minimal_valid(self):
        r = make_envelope(
            RecordType.SEGMENTAL_COMPOSITION,
            SegmentalCompositionCommon(),
            "body",
        )
        assert r.record_type == RecordType.SEGMENTAL_COMPOSITION

    def test_with_segments(self):
        common = SegmentalCompositionCommon(
            segments=[
                BodySegmentComposition(segment=BodySegment.LEFT_ARM, fat_pct=22.1, muscle_mass_kg=3.2),
                BodySegmentComposition(segment=BodySegment.RIGHT_ARM, fat_pct=21.8, muscle_mass_kg=3.3),
                BodySegmentComposition(segment=BodySegment.TRUNK, fat_pct=16.5, muscle_mass_kg=28.0),
                BodySegmentComposition(segment=BodySegment.LEFT_LEG, fat_pct=20.0, muscle_mass_kg=10.2),
                BodySegmentComposition(segment=BodySegment.RIGHT_LEG, fat_pct=19.8, muscle_mass_kg=10.4),
            ]
        )
        assert len(common.segments) == 5
        assert common.segments[2].segment == BodySegment.TRUNK


# ---------------------------------------------------------------------------
# body → cardiovascular_marker
# ---------------------------------------------------------------------------


class TestCardiovascularMarker:
    def test_numeric_value(self):
        common = CardiovascularMarkerCommon(
            marker_type=CardiovascularMarkerType.VASCULAR_AGE,
            value=38.0,
            unit="years",
        )
        r = make_envelope(RecordType.CARDIOVASCULAR_MARKER, common, "body")
        assert r.common.value == 38.0

    def test_categorical_value_ecg(self):
        """ECG result can be a string (categorical)."""
        common = CardiovascularMarkerCommon(
            marker_type=CardiovascularMarkerType.ECG_RESULT,
            value="normal_sinus_rhythm",
            unit=None,
        )
        assert isinstance(common.value, str)

    def test_minimal_valid(self):
        r = make_envelope(RecordType.CARDIOVASCULAR_MARKER, CardiovascularMarkerCommon(), "body")
        assert r.record_type == RecordType.CARDIOVASCULAR_MARKER


# ---------------------------------------------------------------------------
# nutrition → food_entry
# ---------------------------------------------------------------------------


class TestFoodEntry:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.FOOD_ENTRY, FoodEntryCommon(), "nutrition")
        assert r.record_type == RecordType.FOOD_ENTRY

    def test_with_micronutrients(self):
        nutrients = Micronutrients(
            vitamin_c_mg=90.0,
            iron_mg=8.5,
            omega3_g=1.2,
        )
        common = FoodEntryCommon(
            food_name="Wild Salmon",
            food_ref_id="food_salmon_001",
            food_tags=["Whole", "Animal"],
            food_source="NCCDB",
            meal=Meal.DINNER,
            energy_kcal=280.0,
            protein_g=38.0,
            carbs_g=0.0,
            fat_g=12.5,
            micronutrients=nutrients,
        )
        r = make_envelope(RecordType.FOOD_ENTRY, common, "nutrition")
        assert r.common.micronutrients.omega3_g == 1.2
        assert r.common.meal == Meal.DINNER

    def test_micronutrients_all_nullable(self):
        nutrients = Micronutrients()
        assert nutrients.vitamin_a_mcg is None
        assert nutrients.cholesterol_mg is None

    def test_micronutrients_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            Micronutrients(nonexistent_nutrient_mg=5.0)


# ---------------------------------------------------------------------------
# nutrition → daily_summary
# ---------------------------------------------------------------------------


class TestDailySummary:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.DAILY_SUMMARY, DailySummaryCommon(), "nutrition")
        assert r.record_type == RecordType.DAILY_SUMMARY

    def test_with_all_macros(self):
        common = DailySummaryCommon(
            energy_kcal=2200.0,
            protein_g=175.0,
            carbs_g=220.0,
            fat_g=75.0,
            fiber_g=32.0,
            sugar_g=45.0,
            alcohol_g=0.0,
            energy_target_kcal=2300.0,
            micronutrients=Micronutrients(magnesium_mg=420.0),
        )
        assert common.protein_g == 175.0
        assert common.micronutrients.magnesium_mg == 420.0


# ---------------------------------------------------------------------------
# nutrition → food_definition
# ---------------------------------------------------------------------------


class TestFoodDefinition:
    def test_minimal_valid(self):
        r = make_envelope(RecordType.FOOD_DEFINITION, FoodDefinitionCommon(), "nutrition")
        assert r.record_type == RecordType.FOOD_DEFINITION

    def test_full_definition(self):
        common = FoodDefinitionCommon(
            food_ref_id="fd_001",
            food_name="Oat Bran",
            food_category="Grains",
            food_source="USDA",
            food_tags=["Whole", "Vegan"],
            barcode="012345678901",
            energy_kcal=246.0,
            protein_g=17.3,
            carbs_g=66.2,
            fat_g=7.0,
            reference_serving_amount=100.0,
            reference_serving_unit="g",
            micronutrients=Micronutrients(iron_mg=6.0, magnesium_mg=235.0),
        )
        assert common.food_ref_id == "fd_001"
        assert common.micronutrients.magnesium_mg == 235.0


# ---------------------------------------------------------------------------
# RECORD_TYPE_TO_COMMON completeness
# ---------------------------------------------------------------------------


class TestRecordTypeMapping:
    def test_all_inscope_types_are_mapped(self):
        """Every in-scope RecordType must have a common model registered."""
        inscope = {
            RecordType.SLEEP_SESSION,
            RecordType.WORKOUT,
            RecordType.DAILY_RECOVERY,
            RecordType.BODY_MEASUREMENT,
            RecordType.SEGMENTAL_COMPOSITION,
            RecordType.CARDIOVASCULAR_MARKER,
            RecordType.FOOD_ENTRY,
            RecordType.DAILY_SUMMARY,
            RecordType.FOOD_DEFINITION,
        }
        assert inscope == set(RECORD_TYPE_TO_COMMON.keys())

    def test_deferred_types_not_in_record_type_enum(self):
        """lab_result and genetic_variant must NOT appear in RecordType (deferred)."""
        all_values = {rt.value for rt in RecordType}
        assert "lab_result" not in all_values
        assert "genetic_variant" not in all_values
