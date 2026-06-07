"""OpenVitals schema package — domains, records, and micronutrients."""

from openvitals.schema.domains import (
    ActivityType,
    BodySegment,
    CardiovascularMarkerType,
    Domain,
    Meal,
    RecordType,
    ScoreState,
    SetType,
)
from openvitals.schema.records import (
    BodyMeasurementCommon,
    CardiovascularMarkerCommon,
    DailyRecoveryCommon,
    DailySummaryCommon,
    Exercise,
    ExerciseSet,
    FoodDefinitionCommon,
    FoodEntryCommon,
    Micronutrients,
    Record,
    SegmentalCompositionCommon,
    SleepSessionCommon,
    WorkoutCommon,
)

__all__ = [
    # Record envelope
    "Record",
    # Common models
    "SleepSessionCommon",
    "WorkoutCommon",
    "DailyRecoveryCommon",
    "BodyMeasurementCommon",
    "SegmentalCompositionCommon",
    "CardiovascularMarkerCommon",
    "FoodEntryCommon",
    "DailySummaryCommon",
    "FoodDefinitionCommon",
    "Micronutrients",
    "Exercise",
    "ExerciseSet",
    # Enums
    "Domain",
    "RecordType",
    "ActivityType",
    "SetType",
    "Meal",
    "BodySegment",
    "CardiovascularMarkerType",
    "ScoreState",
]
