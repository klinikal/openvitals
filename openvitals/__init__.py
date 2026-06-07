"""
OpenVitals — open-source connector layer for consumer health data.

Public surface:

    from openvitals import OpenVitals, Record, RecordType
    from openvitals import OpenVitalsError, AuthRequired, RateLimited
    from openvitals.connectors.base import BaseConnector, Account
    from openvitals.storage.credentials import CredentialStore, EncryptedFileStore
"""

from openvitals.errors import (
    AuthRequired,
    MissingScope,
    NoData,
    OpenVitalsError,
    PartialFetchError,
    ProviderPayloadError,
    ProviderUnavailable,
    RateLimited,
)
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
    RECORD_TYPE_TO_COMMON,
    SegmentalCompositionCommon,
    SleepSessionCommon,
    WorkoutCommon,
)
from openvitals.surfaces.library import OpenVitals

__all__ = [
    # Core record
    "Record",
    "RECORD_TYPE_TO_COMMON",
    # Domain common models
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
    # Errors
    "OpenVitalsError",
    "AuthRequired",
    "MissingScope",
    "RateLimited",
    "ProviderUnavailable",
    "ProviderPayloadError",
    "NoData",
    "PartialFetchError",
    # Library
    "OpenVitals",
]
