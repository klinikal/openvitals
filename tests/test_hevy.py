"""
Tests for HevyConnector.

Coverage (connector-contract.md §6):
- Unit: fixture → valid workout + body_measurement Records (every common field).
- Contract: every emitted Record validates against the schema.
- raw faithfulness: raw == provider payload (no-data-loss).
- Exercise/set ordering: exercise_order and set_order are preserved correctly.
- Set field mapping: set_type, weight_kg, reps, rpe, duration_seconds, distance_meters.
- Workout-level aggregates: total_volume_kg, set_count.
- Identity: workout = Hevy UUID; body_measurement = compound key.
- Auth: AuthRequired raised when key missing or invalid.
- Deleted events: surfaced as Records with source_extras.deleted = True.
- Pagination: multi-page draining.
- No live API calls — all network access is mocked via respx or unittest.mock.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest
from pydantic import ValidationError

from openvitals.connectors.hevy.connector import (
    HevyConnector,
    _map_exercises,
    _compute_volume,
    _workout_to_record,
    _body_measurement_to_record,
    _hevy_get,
    _paginate_all,
)
from openvitals.errors import AuthRequired, ProviderPayloadError, RateLimited
from openvitals.schema.domains import ActivityType, RecordType, SetType
from openvitals.schema.records import (
    BodyMeasurementCommon,
    Exercise,
    ExerciseSet,
    Record,
    WorkoutCommon,
)
from openvitals.storage.credentials import CredentialStore

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "hevy"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCOUNT_REF = "hevy:testkey8"
INGESTED_AT = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)


class InMemoryCredentialStore(CredentialStore):
    """Minimal in-memory CredentialStore for tests — no Fernet, no disk."""

    def __init__(self, initial: dict[tuple[str, str], dict] | None = None):
        self._data: dict[str, dict] = {}
        for (src, ref), cred in (initial or {}).items():
            self._data[f"{src}:{ref}"] = cred

    def get(self, source: str, account_ref: str) -> dict | None:
        return self._data.get(f"{source}:{account_ref}")

    def set(self, source: str, account_ref: str, credential: dict) -> None:
        self._data[f"{source}:{account_ref}"] = credential

    def delete(self, source: str, account_ref: str) -> None:
        self._data.pop(f"{source}:{account_ref}", None)


def make_store_with_key(api_key: str = "test-api-key-12345678") -> InMemoryCredentialStore:
    store = InMemoryCredentialStore()
    store.set("hevy", ACCOUNT_REF, {"hevy_api_key": api_key})
    return store


def make_account(account_ref: str = ACCOUNT_REF) -> Any:
    from openvitals.connectors.base import Account
    return Account(account_ref=account_ref, source="hevy")


# ---------------------------------------------------------------------------
# Exercise/set mapping unit tests
# ---------------------------------------------------------------------------


class TestMapExercises:
    def test_exercise_order_preserved(self):
        """exercise_order matches Hevy 'index'; exercises sorted correctly."""
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        assert len(exercises) == 3
        assert [ex.exercise_order for ex in exercises] == [0, 1, 2]

    def test_exercise_names_correct(self):
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        assert exercises[0].exercise_name == "Bench Press (Barbell)"
        assert exercises[1].exercise_name == "Overhead Press (Barbell)"
        assert exercises[2].exercise_name == "Plank"

    def test_exercise_ref_id_set(self):
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        assert exercises[0].exercise_ref_id == "tmpl-bench-001"
        assert exercises[1].exercise_ref_id == "tmpl-ohp-002"
        assert exercises[2].exercise_ref_id == "tmpl-plank-003"

    def test_set_order_preserved(self):
        """set_order matches Hevy set 'index' within each exercise."""
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        bench_sets = exercises[0].sets
        assert len(bench_sets) == 4
        assert [s.set_order for s in bench_sets] == [0, 1, 2, 3]

        ohp_sets = exercises[1].sets
        assert len(ohp_sets) == 3
        assert [s.set_order for s in ohp_sets] == [0, 1, 2]

    def test_set_type_mapping(self):
        """All four set types map correctly."""
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        bench_sets = exercises[0].sets
        assert bench_sets[0].set_type == SetType.WARMUP   # index 0
        assert bench_sets[1].set_type == SetType.NORMAL   # index 1
        assert bench_sets[2].set_type == SetType.NORMAL   # index 2
        assert bench_sets[3].set_type == SetType.FAILURE  # index 3

        ohp_sets = exercises[1].sets
        assert ohp_sets[2].set_type == SetType.DROPSET    # index 2

    def test_set_weight_reps_rpe(self):
        """weight_kg, reps, rpe map correctly for normal sets."""
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        bench_normal = exercises[0].sets[1]  # set_order=1, type=normal
        assert bench_normal.weight_kg == 100.0
        assert bench_normal.reps == 5
        assert bench_normal.rpe == 8.0

    def test_set_duration_seconds(self):
        """duration_seconds maps correctly for timed sets (Plank)."""
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        plank_set = exercises[2].sets[0]
        assert plank_set.duration_seconds == 60.0
        assert plank_set.weight_kg is None
        assert plank_set.reps is None

    def test_notes_and_superset_id(self):
        """notes maps correctly; superset_id is None when not present."""
        raw = load_fixture("workout_event.json")["workout"]
        exercises = _map_exercises(raw["exercises"])

        assert exercises[0].notes == "Felt strong today"
        assert exercises[1].notes is None
        assert exercises[0].superset_id is None

    def test_ordering_preserved_even_if_api_returns_out_of_order(self):
        """Connector sorts on 'index' — order is correct even if list is shuffled."""
        raw_ex = [
            {"index": 2, "title": "C", "exercise_template_id": "t3", "supersets_id": None, "notes": None, "sets": []},
            {"index": 0, "title": "A", "exercise_template_id": "t1", "supersets_id": None, "notes": None, "sets": []},
            {"index": 1, "title": "B", "exercise_template_id": "t2", "supersets_id": None, "notes": None, "sets": []},
        ]
        exercises = _map_exercises(raw_ex)
        assert [ex.exercise_order for ex in exercises] == [0, 1, 2]
        assert [ex.exercise_name for ex in exercises] == ["A", "B", "C"]

    def test_set_ordering_preserved_even_if_shuffled(self):
        """Set 'index' is used as sort key regardless of list order."""
        raw_ex = [
            {
                "index": 0,
                "title": "Squat",
                "exercise_template_id": "t1",
                "supersets_id": None,
                "notes": None,
                "sets": [
                    {"index": 2, "type": "normal", "weight_kg": 100.0, "reps": 5, "rpe": None, "distance_meters": None, "duration_seconds": None, "custom_metric": None},
                    {"index": 0, "type": "warmup", "weight_kg": 60.0, "reps": 10, "rpe": None, "distance_meters": None, "duration_seconds": None, "custom_metric": None},
                    {"index": 1, "type": "normal", "weight_kg": 100.0, "reps": 5, "rpe": None, "distance_meters": None, "duration_seconds": None, "custom_metric": None},
                ],
            }
        ]
        exercises = _map_exercises(raw_ex)
        sets = exercises[0].sets
        assert [s.set_order for s in sets] == [0, 1, 2]
        assert sets[0].set_type == SetType.WARMUP
        assert sets[1].set_type == SetType.NORMAL
        assert sets[2].set_type == SetType.NORMAL


# ---------------------------------------------------------------------------
# Volume computation
# ---------------------------------------------------------------------------


class TestComputeVolume:
    def test_total_volume_kg(self):
        """Σ(weight × reps) across all sets where both are non-null."""
        raw = load_fixture("workout_event.json")["workout"]
        # Bench: warmup 60×10=600, normal 100×5=500, normal 100×5=500, failure 100×4=400 = 2000
        # OHP: normal 70×6=420, normal 70×6=420, dropset 60×8=480 = 1320
        # Plank: no weight/reps → contributes 0
        vol, count = _compute_volume(raw["exercises"])
        assert vol == pytest.approx(3320.0)

    def test_set_count(self):
        """set_count = total sets across all exercises."""
        raw = load_fixture("workout_event.json")["workout"]
        _, count = _compute_volume(raw["exercises"])
        assert count == 8  # 4 bench + 3 ohp + 1 plank

    def test_no_weighted_sets_returns_none_volume(self):
        """When no sets have weight+reps, volume should be None (not 0)."""
        raw_ex = [
            {
                "index": 0,
                "title": "Plank",
                "sets": [
                    {"index": 0, "type": "normal", "weight_kg": None, "reps": None, "rpe": None, "distance_meters": None, "duration_seconds": 60.0, "custom_metric": None}
                ],
            }
        ]
        vol, count = _compute_volume(raw_ex)
        assert vol is None
        assert count == 1


# ---------------------------------------------------------------------------
# Workout Record mapping
# ---------------------------------------------------------------------------


class TestWorkoutToRecord:
    def test_record_validates(self):
        """_workout_to_record produces a valid Record."""
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert isinstance(record, Record)

    def test_source_and_domain(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.source == "hevy"
        assert record.domain == "activity"
        assert record.record_type == RecordType.WORKOUT

    def test_identity(self):
        """source_record_id = Hevy workout UUID (provider-native)."""
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.source_record_id == "b459cba5-1111-2222-3333-000000000001"
        assert record.account_ref == ACCOUNT_REF

    def test_timestamps(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.occurred_at == datetime(2026, 6, 6, 9, 0, 0, tzinfo=UTC)
        assert record.occurred_end == datetime(2026, 6, 6, 10, 5, 0, tzinfo=UTC)

    def test_duration_minutes(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        common = record.common
        assert isinstance(common, WorkoutCommon)
        assert common.duration_minutes == pytest.approx(65.0)

    def test_activity_type(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.common.activity_type == ActivityType.STRENGTH

    def test_activity_label(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.common.activity_label == "Upper Body — Push"

    def test_total_volume_kg_and_set_count(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        common = record.common
        assert common.total_volume_kg == pytest.approx(3320.0)
        assert common.set_count == 8

    def test_exercises_present(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert len(record.common.exercises) == 3

    def test_raw_is_verbatim(self):
        """raw must equal the provider payload exactly — no mutation, no extra keys."""
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.raw == raw

    def test_deleted_flag_in_source_extras(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT, deleted=True)
        assert record.source_extras is not None
        assert record.source_extras.get("deleted") is True

    def test_description_in_source_extras(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        # description is "Chest and shoulders focus" in fixture
        assert record.source_extras is not None
        assert record.source_extras.get("description") == "Chest and shoulders focus"

    def test_local_date_is_utc_date_of_start_time(self):
        """Hevy workouts have no local-date field; local_date = UTC date of occurred_at.

        ASSUMPTION: Hevy does not expose a timezone or local-date field in the
        workout payload. We use the UTC date of start_time as a proxy.
        Consumers in non-UTC timezones should apply their own offset.
        """
        raw = load_fixture("workout_event.json")["workout"]
        # start_time = 2026-06-06T09:00:00Z → local_date = 2026-06-06 (UTC)
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        assert record.local_date == date(2026, 6, 6)

    def test_missing_start_time_raises(self):
        raw = load_fixture("workout_event.json")["workout"]
        raw_copy = {**raw, "start_time": None}
        with pytest.raises(ProviderPayloadError, match="start_time"):
            _workout_to_record(raw_copy, ACCOUNT_REF, INGESTED_AT)


# ---------------------------------------------------------------------------
# Body measurement Record mapping
# ---------------------------------------------------------------------------


class TestBodyMeasurementToRecord:
    def _first_measurement(self) -> dict:
        return load_fixture("body_measurements_page.json")["body_measurements"][0]

    def test_record_validates(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert isinstance(record, Record)

    def test_source_and_domain(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.source == "hevy"
        assert record.domain == "body"
        assert record.record_type == RecordType.BODY_MEASUREMENT

    def test_identity_compound(self):
        """source_record_id = account_ref:local_date:body_measurement (compound)."""
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.source_record_id == f"{ACCOUNT_REF}:2026-06-06:body_measurement"

    def test_local_date(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.local_date == date(2026, 6, 6)

    def test_occurred_at_midnight_utc(self):
        """occurred_at should be midnight UTC of the local date (stable sentinel)."""
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.occurred_at == datetime(2026, 6, 6, 0, 0, 0, tzinfo=UTC)

    def test_common_weight_kg(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        common = record.common
        assert isinstance(common, BodyMeasurementCommon)
        assert common.weight_kg == 82.5

    def test_common_lean_mass_kg(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.common.lean_mass_kg == 68.0

    def test_common_body_fat_pct(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.common.body_fat_pct == pytest.approx(17.6)

    def test_circumferences_in_source_extras(self):
        m = {**self._first_measurement(), "waist": 80.5, "left_bicep_cm": 35.0}
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.source_extras == {"waist": 80.5, "left_bicep_cm": 35.0}

    def test_null_fields_stay_null(self):
        """Second measurement has only weight_kg; all others should be null."""
        m = load_fixture("body_measurements_page.json")["body_measurements"][1]
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.common.weight_kg == 83.1
        assert record.common.lean_mass_kg is None
        assert record.common.body_fat_pct is None

    def test_raw_is_verbatim(self):
        m = self._first_measurement()
        record = _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)
        assert record.raw == m

    def test_missing_date_raises(self):
        m = {**self._first_measurement(), "date": None}
        with pytest.raises(ProviderPayloadError, match="date"):
            _body_measurement_to_record(m, ACCOUNT_REF, INGESTED_AT)


# ---------------------------------------------------------------------------
# Ordering preservation — end-to-end through full workout record
# ---------------------------------------------------------------------------


class TestOrderingEndToEnd:
    """Assert that exercise_order and set_order survive the full mapping pipeline."""

    def test_exercise_order_end_to_end(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        orders = [ex.exercise_order for ex in record.common.exercises]
        assert orders == sorted(orders), "exercise_order must be monotonically increasing"
        assert orders == [0, 1, 2]

    def test_set_order_end_to_end_per_exercise(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        for ex in record.common.exercises:
            orders = [s.set_order for s in ex.sets]
            assert orders == sorted(orders), (
                f"set_order must be monotonically increasing in exercise '{ex.exercise_name}'"
            )

    def test_set_order_first_exercise(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        bench = record.common.exercises[0]  # exercise_order=0
        assert [s.set_order for s in bench.sets] == [0, 1, 2, 3]

    def test_all_set_types_correct_after_ordering(self):
        raw = load_fixture("workout_event.json")["workout"]
        record = _workout_to_record(raw, ACCOUNT_REF, INGESTED_AT)
        bench = record.common.exercises[0]
        types = [s.set_type for s in bench.sets]
        assert types == [SetType.WARMUP, SetType.NORMAL, SetType.NORMAL, SetType.FAILURE]


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    def test_connect_raises_auth_required_when_no_key(self):
        store = InMemoryCredentialStore()
        connector = HevyConnector(store)
        with pytest.raises(AuthRequired, match="hevy_api_key"):
            connector.connect({"hevy_api_key": ""})

    def test_fetch_raises_auth_required_when_no_stored_creds(self):
        store = InMemoryCredentialStore()
        connector = HevyConnector(store)
        account = make_account()
        with pytest.raises(AuthRequired, match="connect"):
            list(connector.fetch(account))

    def test_connect_raises_auth_required_on_401(self):
        store = InMemoryCredentialStore()
        connector = HevyConnector(store)
        with patch("openvitals.connectors.hevy.connector._hevy_get") as mock_get:
            mock_get.side_effect = AuthRequired("401")
            with pytest.raises(AuthRequired):
                connector.connect({"hevy_api_key": "bad-key"})

    def test_connect_stores_credential_after_valid_key(self):
        store = InMemoryCredentialStore()
        connector = HevyConnector(store)
        with patch("openvitals.connectors.hevy.connector._hevy_get") as mock_get:
            mock_get.return_value = {"data": {"id": "00000000-0000-0000-0000-000000000001", "username": "x"}}
            account = connector.connect({"hevy_api_key": "valid-key-12345678"})
        # Key should be stored
        creds = store.get("hevy", account.account_ref)
        assert creds is not None
        assert creds["hevy_api_key"] == "valid-key-12345678"

    def test_connect_account_ref_from_user_id(self):
        store = InMemoryCredentialStore()
        connector = HevyConnector(store)
        with patch("openvitals.connectors.hevy.connector._hevy_get") as mock_get:
            mock_get.return_value = {"data": {"id": "00000000-0000-0000-0000-000000000001"}}
            account = connector.connect({"hevy_api_key": "valid-key-12345678"})
        assert mock_get.call_args[0][0] == "/v1/user/info"
        assert account.account_ref == "hevy:00000000-0000-0000-0000-000000000001"

    def test_connect_missing_user_id_raises(self):
        connector = HevyConnector(InMemoryCredentialStore())
        with patch("openvitals.connectors.hevy.connector._hevy_get", return_value={"data": {}}):
            with pytest.raises(ProviderPayloadError):
                connector.connect({"hevy_api_key": "valid-key-12345678"})

    def test_api_key_not_in_account_metadata_full(self):
        """Full key must never appear in account.metadata."""
        store = InMemoryCredentialStore()
        connector = HevyConnector(store)
        test_key = "supersecret-api-key-full-value"
        with patch("openvitals.connectors.hevy.connector._hevy_get") as mock_get:
            mock_get.return_value = {"data": {"id": "00000000-0000-0000-0000-000000000001", "username": "x"}}
            account = connector.connect({"hevy_api_key": test_key})
        # The full key must not be in metadata values
        for v in account.metadata.values():
            assert test_key not in str(v), "Full API key must never appear in account metadata"


# ---------------------------------------------------------------------------
# Pagination test
# ---------------------------------------------------------------------------


class TestPagination:
    def test_paginate_all_drains_multiple_pages(self):
        """_paginate_all collects items from all pages."""
        page1 = {"page_count": 2, "events": [{"id": "a"}, {"id": "b"}]}
        page2 = {"page_count": 2, "events": [{"id": "c"}]}

        call_count = 0
        def mock_get(path, api_key, params=None):
            nonlocal call_count
            call_count += 1
            if (params or {}).get("page") == 1:
                return page1
            return page2

        with patch("openvitals.connectors.hevy.connector._hevy_get", side_effect=mock_get):
            with patch("openvitals.connectors.hevy.connector.time.sleep"):  # skip throttle
                results = _paginate_all("/v1/workouts/events", "key", "events")

        assert results == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        assert call_count == 2

    def test_paginate_raises_on_missing_items_key(self):
        """If the expected items key is absent, raise ProviderPayloadError."""
        with patch("openvitals.connectors.hevy.connector._hevy_get") as mock_get:
            mock_get.return_value = {"page_count": 1, "wrong_key": []}
            with pytest.raises(ProviderPayloadError, match="events"):
                _paginate_all("/v1/workouts/events", "key", "events")


# ---------------------------------------------------------------------------
# Deleted events test
# ---------------------------------------------------------------------------


class TestDeletedEvents:
    def test_deleted_event_yields_record_with_deleted_flag(self):
        """Deleted workout events → Record with source_extras.deleted = True."""
        store = make_store_with_key()
        connector = HevyConnector(store)
        account = make_account()

        deleted_event = {"type": "deleted", "id": "dead-workout-uuid"}
        updated_event = {
            "type": "updated",
            "workout": load_fixture("workout_event.json")["workout"],
        }

        events_response = {
            "page_count": 1,
            "events": [deleted_event, updated_event],  # newest first
        }
        body_response = {"page_count": 1, "body_measurements": []}

        def mock_paginate(path, api_key, items_key, extra_params=None):
            if "events" in path:
                return events_response["events"]
            if "body_measurements" in path:
                return []
            return []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            records = list(connector.fetch(account))

        deleted_records = [r for r in records if r.source_extras and r.source_extras.get("deleted")]
        assert len(deleted_records) == 1
        assert deleted_records[0].source_record_id == "dead-workout-uuid"

    def test_deleted_event_uses_deleted_at(self):
        connector = HevyConnector(make_store_with_key())
        ev = {"type": "deleted", "id": "dead-workout-uuid", "deleted_at": "2026-06-07T12:30:00Z"}

        def mock_paginate(path, api_key, items_key, extra_params=None):
            return [ev] if "events" in path else []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            (rec,) = list(connector.fetch(make_account()))
        assert rec.occurred_at == datetime(2026, 6, 7, 12, 30, tzinfo=timezone.utc)
        assert rec.local_date == date(2026, 6, 7)

    def test_deleted_event_missing_id_raises(self):
        """Deleted event with no 'id' field should raise ProviderPayloadError."""
        store = make_store_with_key()
        connector = HevyConnector(store)
        account = make_account()

        bad_event = {"type": "deleted"}  # no 'id'

        def mock_paginate(path, api_key, items_key, extra_params=None):
            if "events" in path:
                return [bad_event]
            return []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            with pytest.raises(ProviderPayloadError, match="id"):
                list(connector.fetch(account))


# ---------------------------------------------------------------------------
# Unknown event types
# ---------------------------------------------------------------------------


class TestUnknownEventTypes:
    """Unknown event types are skipped with a warning — not an error (forward compatibility)."""

    def test_unknown_event_type_is_skipped(self):
        """An unrecognised event type produces no record (no crash)."""
        store = make_store_with_key()
        connector = HevyConnector(store)
        account = make_account()

        unknown_event = {"type": "rest_period", "id": "some-id"}  # hypothetical future type

        def mock_paginate(path, api_key, items_key, extra_params=None):
            if "events" in path:
                return [unknown_event]
            return []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            records = list(connector.fetch(account))

        assert records == []

    def test_unknown_event_type_emits_warning(self, caplog):
        """An unrecognised event type logs a warning so it's visible for future support."""
        import logging
        store = make_store_with_key()
        connector = HevyConnector(store)
        account = make_account()

        unknown_event = {"type": "rest_period", "id": "some-id"}

        def mock_paginate(path, api_key, items_key, extra_params=None):
            if "events" in path:
                return [unknown_event]
            return []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            with caplog.at_level(logging.WARNING, logger="openvitals.connectors.hevy.connector"):
                list(connector.fetch(account))

        assert any("rest_period" in m for m in caplog.messages), (
            "Expected a warning mentioning the unknown event type"
        )

    def test_unknown_event_alongside_known_yields_known_only(self):
        """Unknown event is skipped; adjacent known events still yield records normally."""
        store = make_store_with_key()
        connector = HevyConnector(store)
        account = make_account()

        updated_event = {
            "type": "updated",
            "workout": load_fixture("workout_event.json")["workout"],
        }
        unknown_event = {"type": "rest_period", "id": "some-id"}

        def mock_paginate(path, api_key, items_key, extra_params=None):
            if "events" in path:
                return [unknown_event, updated_event]  # newest first
            return []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            records = list(connector.fetch(account))

        assert len(records) == 1
        assert records[0].record_type.value == "workout"


# ---------------------------------------------------------------------------
# Full fetch integration (mocked network)
# ---------------------------------------------------------------------------


class TestFetchIntegration:
    """End-to-end fetch() with all network calls mocked."""

    def _run_fetch(self, events: list[dict], body_measurements: list[dict]) -> list[Record]:
        store = make_store_with_key()
        connector = HevyConnector(store)
        account = make_account()

        def mock_paginate(path, api_key, items_key, extra_params=None):
            if "events" in path:
                return events
            if "body_measurements" in path:
                return body_measurements
            return []

        with patch("openvitals.connectors.hevy.connector._paginate_all", side_effect=mock_paginate):
            return list(connector.fetch(account))

    def test_fetch_emits_workout_and_body_records(self):
        workout_payload = load_fixture("workout_event.json")["workout"]
        body_measurements = load_fixture("body_measurements_page.json")["body_measurements"]

        events = [{"type": "updated", "workout": workout_payload}]
        records = self._run_fetch(events, body_measurements)

        workout_records = [r for r in records if r.record_type == RecordType.WORKOUT and not (r.source_extras or {}).get("deleted")]
        body_records = [r for r in records if r.record_type == RecordType.BODY_MEASUREMENT]

        assert len(workout_records) == 1
        assert len(body_records) == 2

    def test_fetch_workout_record_correct_id(self):
        workout_payload = load_fixture("workout_event.json")["workout"]
        events = [{"type": "updated", "workout": workout_payload}]
        records = self._run_fetch(events, [])
        workout = next(r for r in records if r.record_type == RecordType.WORKOUT)
        assert workout.source_record_id == "b459cba5-1111-2222-3333-000000000001"

    def test_fetch_body_measurement_identity(self):
        body_measurements = load_fixture("body_measurements_page.json")["body_measurements"]
        records = self._run_fetch([], body_measurements)
        body_records = sorted(records, key=lambda r: r.source_record_id)
        assert body_records[0].source_record_id == f"{ACCOUNT_REF}:2026-05-30:body_measurement"
        assert body_records[1].source_record_id == f"{ACCOUNT_REF}:2026-06-06:body_measurement"

    def test_fetch_all_records_validate_as_record(self):
        workout_payload = load_fixture("workout_event.json")["workout"]
        body_measurements = load_fixture("body_measurements_page.json")["body_measurements"]
        events = [{"type": "updated", "workout": workout_payload}]
        records = self._run_fetch(events, body_measurements)
        for r in records:
            assert isinstance(r, Record), f"Expected Record, got {type(r)}"

    def test_fetch_empty_returns_no_records(self):
        records = self._run_fetch([], [])
        assert records == []

    def test_fetch_idempotent_same_ids(self):
        """Re-fetching same events yields same source_record_ids (idempotency)."""
        workout_payload = load_fixture("workout_event.json")["workout"]
        events = [{"type": "updated", "workout": workout_payload}]
        records1 = self._run_fetch(events, [])
        records2 = self._run_fetch(events, [])
        ids1 = {r.source_record_id for r in records1}
        ids2 = {r.source_record_id for r in records2}
        assert ids1 == ids2
