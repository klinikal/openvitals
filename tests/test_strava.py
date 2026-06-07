"""
Tests for StravaConnector.

Coverage:
- Fixture → valid workout Records (all key common fields).
- Activity-type normalization for every category.
- raw byte-faithfulness (verbatim payload, no mutations).
- Identity: source_record_id == str(strava_id), account_ref, source.
- Timestamps: occurred_at UTC, occurred_end derived, timezone IANA.
- Energy conversion: kJ → kcal.
- auth: refresh-token rotation (rotated token persisted before use).
- auth: missing required scope raises MissingScope (loud failure).
- auth: missing credentials raises AuthRequired.
- pagination: multi-page fetch drains fully.
- idempotency: re-fetching same range yields same logical records.
- no real network calls — all httpx interactions are mocked.

No live API is ever called. Run with:
    cd /Users/james/Documents/Projects/OpenVitals/code
    VIRTUAL_ENV="" uv run pytest tests/test_strava.py -q
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openvitals.connectors.strava.connector import (
    StravaConnector,
    _kj_to_kcal,
    _normalize_sport_type,
    REQUIRED_SCOPE,
)
from openvitals.errors import AuthRequired, MissingScope, PartialFetchError, RateLimited
from openvitals.schema.domains import ActivityType, Domain, RecordType
from openvitals.schema.records import Record, WorkoutCommon
from openvitals.storage.credentials import CredentialStore

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "strava"
UTC = timezone.utc
ATHLETE_ID = 12345678
ACCOUNT_REF = str(ATHLETE_ID)


def load_fixture(name: str) -> Any:
    """Load a JSON fixture by filename."""
    return json.loads((FIXTURES_DIR / name).read_text())


class InMemoryStore(CredentialStore):
    """Simple in-memory CredentialStore for tests — no encryption needed."""

    def __init__(self, initial: dict[str, dict] | None = None) -> None:
        self._data: dict[str, dict] = dict(initial or {})

    def _key(self, source: str, account_ref: str) -> str:
        return f"{source}:{account_ref}"

    def get(self, source: str, account_ref: str) -> dict | None:
        return self._data.get(self._key(source, account_ref))

    def set(self, source: str, account_ref: str, credential: dict) -> None:
        self._data[self._key(source, account_ref)] = dict(credential)

    def delete(self, source: str, account_ref: str) -> None:
        self._data.pop(self._key(source, account_ref), None)


def make_credential(*, expires_at: int | None = None, scope: str = "read,activity:read_all") -> dict:
    """Build a synthetic credential dict. Defaults to a non-expired token."""
    if expires_at is None:
        # 2 hours from now — well within valid window
        expires_at = int(time.time()) + 7200
    return {
        "access_token": "fake_access_token",
        "refresh_token": "fake_refresh_token",
        "expires_at": expires_at,
        "athlete_id": ATHLETE_ID,
        "scope": scope,
    }


def make_connector(
    store: InMemoryStore | None = None,
    http_client=None,
) -> StravaConnector:
    if store is None:
        store = InMemoryStore()
    return StravaConnector(
        client_id="fake_client_id",
        client_secret="fake_client_secret",
        credential_store=store,
        http_client=http_client,
    )


def make_mock_http(pages: list[list[dict]]) -> MagicMock:
    """
    Build a mock httpx.Client that returns successive pages on .get() calls.
    After ``pages`` are exhausted, returns an empty list (pagination terminator).
    """
    import httpx

    responses = []
    for page in pages:
        r = MagicMock(spec=httpx.Response)
        r.status_code = 200
        r.json.return_value = page
        r.raise_for_status.return_value = None
        responses.append(r)

    # Final terminator: empty list
    empty = MagicMock(spec=httpx.Response)
    empty.status_code = 200
    empty.json.return_value = []
    empty.raise_for_status.return_value = None
    responses.append(empty)

    mock_http = MagicMock()
    mock_http.get.side_effect = responses
    return mock_http


# ---------------------------------------------------------------------------
# Unit: sport_type normalization
# ---------------------------------------------------------------------------


class TestSportTypeNormalization:
    def test_run(self):
        assert _normalize_sport_type("Run") == ActivityType.RUN

    def test_trail_run(self):
        assert _normalize_sport_type("TrailRun") == ActivityType.RUN

    def test_virtual_run(self):
        assert _normalize_sport_type("VirtualRun") == ActivityType.RUN

    def test_ride(self):
        assert _normalize_sport_type("Ride") == ActivityType.RIDE

    def test_gravel_ride(self):
        assert _normalize_sport_type("GravelRide") == ActivityType.RIDE

    def test_mountain_bike_ride(self):
        assert _normalize_sport_type("MountainBikeRide") == ActivityType.RIDE

    def test_swim(self):
        assert _normalize_sport_type("Swim") == ActivityType.SWIM

    def test_walk(self):
        assert _normalize_sport_type("Walk") == ActivityType.WALK

    def test_hike(self):
        assert _normalize_sport_type("Hike") == ActivityType.HIKE

    def test_weight_training_maps_to_strength(self):
        # ASSUMPTION: WeightTraining is the closest to strength; Strava has no sets data
        assert _normalize_sport_type("WeightTraining") == ActivityType.STRENGTH

    def test_sauna(self):
        assert _normalize_sport_type("Sauna") == ActivityType.SAUNA

    def test_yoga_maps_to_cardio(self):
        # ASSUMPTION: mindful activities → cardio
        assert _normalize_sport_type("Yoga") == ActivityType.CARDIO

    def test_workout_maps_to_cardio(self):
        assert _normalize_sport_type("Workout") == ActivityType.CARDIO

    def test_surfing_maps_to_cardio(self):
        assert _normalize_sport_type("Surfing") == ActivityType.CARDIO

    def test_unknown_sport_maps_to_other(self):
        assert _normalize_sport_type("FutureUnsupportedSport") == ActivityType.OTHER

    def test_none_maps_to_other(self):
        assert _normalize_sport_type(None) == ActivityType.OTHER

    def test_empty_string_maps_to_other(self):
        assert _normalize_sport_type("") == ActivityType.OTHER


# ---------------------------------------------------------------------------
# Unit: kJ → kcal conversion
# ---------------------------------------------------------------------------


class TestEnergyConversion:
    def test_known_value(self):
        # 418.4 kJ → 100 kcal
        result = _kj_to_kcal(418.4)
        assert result is not None
        assert abs(result - 100.0) < 0.01

    def test_none_input(self):
        assert _kj_to_kcal(None) is None

    def test_zero(self):
        assert _kj_to_kcal(0) == 0.0

    def test_fixture_run_activity(self):
        # Fixture Run: kilojoules=987.4 → kcal = 987.4 / 4.184 ≈ 236.0
        result = _kj_to_kcal(987.4)
        assert result is not None
        assert abs(result - 236.0) < 1.0


# ---------------------------------------------------------------------------
# Unit: auth — connect()
# ---------------------------------------------------------------------------


class TestConnect:
    def test_no_credentials_raises_auth_required(self):
        store = InMemoryStore()
        connector = make_connector(store)
        with pytest.raises(AuthRequired):
            connector.connect({})

    def test_missing_athlete_id_raises_auth_required(self):
        connector = make_connector()
        with pytest.raises(AuthRequired):
            connector.connect({"access_token": "x", "refresh_token": "y", "expires_at": 99999999999})

    def test_missing_scope_raises_missing_scope(self):
        connector = make_connector()
        cred = make_credential(scope="read")  # missing activity:read_all
        with pytest.raises(MissingScope) as exc_info:
            connector.connect(cred)
        assert REQUIRED_SCOPE in exc_info.value.required_scope

    def test_no_scope_field_raises_missing_scope(self):
        connector = make_connector()
        cred = make_credential()
        del cred["scope"]
        with pytest.raises(MissingScope):
            connector.connect(cred)

    def test_valid_credential_returns_account(self):
        store = InMemoryStore()
        connector = make_connector(store)
        cred = make_credential()
        account = connector.connect(cred)
        assert account.source == "strava"
        assert account.account_ref == ACCOUNT_REF

    def test_account_ref_is_stable_str_of_athlete_id(self):
        connector = make_connector()
        cred = make_credential()
        account = connector.connect(cred)
        assert account.account_ref == str(ATHLETE_ID)

    def test_stale_token_triggers_refresh_and_persists(self):
        """When the access token is near-expired, connect() refreshes and persists."""
        import httpx

        store = InMemoryStore()
        # Token expired 1 minute ago
        stale_cred = make_credential(expires_at=int(time.time()) - 60)
        store.set("strava", ACCOUNT_REF, stale_cred)

        new_expires = int(time.time()) + 21600  # 6 hours from now
        refresh_response = MagicMock(spec=httpx.Response)
        refresh_response.status_code = 200
        refresh_response.json.return_value = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_at": new_expires,
            "scope": "read,activity:read_all",
        }
        refresh_response.raise_for_status.return_value = None

        mock_http = MagicMock()
        mock_http.post.return_value = refresh_response

        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(stale_cred)

        # Token was refreshed
        mock_http.post.assert_called_once()

        # Rotated token persisted to store BEFORE returning
        persisted = store.get("strava", ACCOUNT_REF)
        assert persisted is not None
        assert persisted["access_token"] == "new_access_token"
        assert persisted["refresh_token"] == "new_refresh_token"

        # Never in metadata (no raw tokens in Account metadata)
        assert "access_token" not in account.metadata
        assert "refresh_token" not in account.metadata

    def test_tokens_never_in_account_metadata(self):
        """Raw tokens must never appear in Account.metadata."""
        store = InMemoryStore()
        connector = make_connector(store)
        cred = make_credential()
        account = connector.connect(cred)
        metadata_str = str(account.metadata)
        assert "fake_access_token" not in metadata_str
        assert "fake_refresh_token" not in metadata_str


# ---------------------------------------------------------------------------
# Fixture → Record mapping
# ---------------------------------------------------------------------------


class TestActivityMapping:
    """Map each fixture activity → Record and assert key fields."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        cred = make_credential()
        store.set("strava", ACCOUNT_REF, cred)
        mock_http = make_mock_http([self.activities])
        self.connector = make_connector(store, http_client=mock_http)
        cred_copy = make_credential()
        self.account = self.connector.connect(cred_copy)
        # Re-create connector (mock http reused) for fetch
        mock_http2 = make_mock_http([self.activities])
        store2 = InMemoryStore()
        store2.set("strava", ACCOUNT_REF, make_credential())
        self.connector2 = make_connector(store2, http_client=mock_http2)
        cred2 = make_credential()
        self.account2 = self.connector2.connect(cred2)
        self.records: list[Record] = list(self.connector2.fetch(self.account2))

    def test_correct_number_of_records(self):
        assert len(self.records) == len(self.activities)

    def test_all_records_are_workout_type(self):
        for r in self.records:
            assert r.record_type == RecordType.WORKOUT

    def test_domain_is_activity(self):
        for r in self.records:
            assert r.domain == Domain.ACTIVITY.value

    def test_source_is_strava(self):
        for r in self.records:
            assert r.source == "strava"

    def test_source_record_id_is_native_strava_id(self):
        """source_record_id must be the Strava activity id as a string."""
        expected_ids = [str(a["id"]) for a in self.activities]
        actual_ids = [r.source_record_id for r in self.records]
        assert actual_ids == expected_ids

    def test_account_ref_matches(self):
        for r in self.records:
            assert r.account_ref == ACCOUNT_REF

    def test_occurred_at_is_utc(self):
        for r in self.records:
            assert r.occurred_at.tzinfo is not None
            assert r.occurred_at.tzinfo == timezone.utc or str(r.occurred_at.tzinfo) in ("+00:00", "UTC")

    def test_run_activity_type(self):
        run = self.records[0]  # first fixture is a Run
        assert run.common.activity_type == ActivityType.RUN
        assert run.common.activity_label == "Run"

    def test_ride_activity_type(self):
        ride = self.records[1]  # GravelRide
        assert ride.common.activity_type == ActivityType.RIDE
        assert ride.common.activity_label == "GravelRide"

    def test_swim_activity_type(self):
        swim = self.records[2]
        assert swim.common.activity_type == ActivityType.SWIM

    def test_hike_activity_type(self):
        hike = self.records[3]
        assert hike.common.activity_type == ActivityType.HIKE

    def test_weight_training_activity_type(self):
        strength = self.records[4]
        assert strength.common.activity_type == ActivityType.STRENGTH

    def test_walk_activity_type(self):
        walk = self.records[5]
        assert walk.common.activity_type == ActivityType.WALK

    def test_surfing_maps_to_cardio(self):
        surfing = self.records[6]
        assert surfing.common.activity_type == ActivityType.CARDIO

    def test_future_unknown_sport_maps_to_other(self):
        unknown = self.records[7]
        assert unknown.common.activity_type == ActivityType.OTHER

    def test_run_duration_minutes(self):
        """moving_time=3180s → 53.0 minutes"""
        run = self.records[0]
        assert run.common.duration_minutes is not None
        assert abs(run.common.duration_minutes - 53.0) < 0.01

    def test_run_elapsed_minutes(self):
        """elapsed_time=3360s → 56.0 minutes"""
        run = self.records[0]
        assert run.common.elapsed_minutes is not None
        assert abs(run.common.elapsed_minutes - 56.0) < 0.01

    def test_run_distance_meters(self):
        run = self.records[0]
        assert run.common.distance_meters == 9540.0

    def test_run_elevation_gain(self):
        run = self.records[0]
        assert run.common.elevation_gain_meters == 62.3

    def test_run_avg_heart_rate(self):
        """148.6 bpm rounds to int 149"""
        run = self.records[0]
        assert run.common.avg_heart_rate_bpm == 149

    def test_run_max_heart_rate(self):
        run = self.records[0]
        assert run.common.max_heart_rate_bpm == 172

    def test_run_energy_kcal_from_kj(self):
        """987.4 kJ ÷ 4.184 ≈ 236.0 kcal"""
        run = self.records[0]
        assert run.common.energy_kcal is not None
        assert abs(run.common.energy_kcal - 236.0) < 1.0

    def test_ride_avg_power_watts(self):
        ride = self.records[1]
        assert ride.common.avg_power_watts == 185.0

    def test_run_avg_speed_mps(self):
        run = self.records[0]
        assert run.common.avg_speed_mps == 3.0

    def test_no_energy_when_kj_is_null(self):
        """WeightTraining has kilojoules=null → energy_kcal should be None"""
        strength = self.records[4]
        assert strength.common.energy_kcal is None

    def test_no_hr_when_has_heartrate_false(self):
        strength = self.records[4]
        assert strength.common.avg_heart_rate_bpm is None
        assert strength.common.max_heart_rate_bpm is None

    def test_no_strength_fields(self):
        """Strava is cardio — exercises, total_volume_kg, set_count must be empty/None."""
        for r in self.records:
            assert r.common.total_volume_kg is None
            assert r.common.set_count is None
            assert r.common.exercises == []

    def test_timezone_iana_extracted(self):
        """'(GMT+01:00) Europe/London' → 'Europe/London'"""
        run = self.records[0]
        assert run.timezone == "Europe/London"

    def test_occurred_end_derived_from_elapsed_time(self):
        """occurred_end = occurred_at + elapsed_time seconds"""
        run = self.records[0]
        assert run.occurred_end is not None
        delta = run.occurred_end - run.occurred_at
        assert int(delta.total_seconds()) == 3360  # elapsed_time from fixture

    def test_schema_version(self):
        for r in self.records:
            assert r.schema_version == "0.1"


# ---------------------------------------------------------------------------
# raw byte-faithfulness
# ---------------------------------------------------------------------------


class TestRawFaithfulness:
    def test_raw_equals_verbatim_provider_payload(self):
        """raw must be the verbatim Strava dict — no decoding, renaming, or dropping."""
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        for record, raw_activity in zip(records, activities):
            assert record.raw == raw_activity, (
                f"raw mismatch for activity id={raw_activity['id']}: "
                f"expected {raw_activity}, got {record.raw}"
            )

    def test_raw_round_trips(self):
        """Serialise and deserialise a Record — raw must survive unchanged."""
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        for record in records:
            dumped = record.model_dump()
            restored = Record.model_validate(dumped)
            assert restored.raw == record.raw

    def test_lap_splits_remain_in_raw(self):
        """Lap splits from fixture (if present as null) stay in raw untouched."""
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        # Fixture has "laps": null in raw — must be preserved
        for record, raw_act in zip(records, activities):
            if "laps" in raw_act:
                assert "laps" in record.raw
                assert record.raw["laps"] == raw_act["laps"]

    def test_common_does_not_contain_kj(self):
        """kilojoules lives in raw/source_extras, not in common (which has energy_kcal)."""
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        run = records[0]
        # common only has energy_kcal (decoded), not kilojoules
        assert not hasattr(run.common, "kilojoules")
        # kilojoules stays in raw
        assert "kilojoules" in run.raw
        # and also surfaced in source_extras
        assert run.source_extras is not None
        assert "kilojoules" in run.source_extras


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


class TestPagination:
    def test_multi_page_fetch_drains_fully(self):
        """Connector must drain all pages; total = page1 + page2."""
        activities = load_fixture("activities_page1.json")
        # Split into two pages of 4 each
        page1 = activities[:4]
        page2 = activities[4:]

        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([page1, page2])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        assert len(records) == len(activities)
        # Confirm both pages were requested (get called 3 times: page1, page2, empty)
        assert mock_http.get.call_count == 3

    def test_single_page_terminates_correctly(self):
        """Single-page result: get called twice (page1, empty terminator)."""
        activities = load_fixture("activities_page1.json")[:3]
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        assert len(records) == 3
        assert mock_http.get.call_count == 2  # page1 + empty


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_logical_records_on_re_fetch(self):
        """Re-fetching the same range produces the same source_record_ids."""
        activities = load_fixture("activities_page1.json")

        def make_fresh() -> list[Record]:
            store = InMemoryStore()
            store.set("strava", ACCOUNT_REF, make_credential())
            mock_http = make_mock_http([activities])
            connector = make_connector(store, http_client=mock_http)
            account = connector.connect(make_credential())
            return list(connector.fetch(account))

        records1 = make_fresh()
        records2 = make_fresh()

        ids1 = [r.source_record_id for r in records1]
        ids2 = [r.source_record_id for r in records2]
        assert ids1 == ids2

    def test_no_duplicate_logical_ids_in_single_fetch(self):
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        ids = [r.source_record_id for r in records]
        assert len(ids) == len(set(ids)), "Duplicate source_record_ids within a single fetch"


# ---------------------------------------------------------------------------
# Auth edge cases in fetch
# ---------------------------------------------------------------------------


class TestFetchAuth:
    def test_no_credential_in_store_raises_auth_required(self):
        """fetch() raises AuthRequired if the store has no credential for this account."""
        import httpx

        store = InMemoryStore()
        # Do NOT store any credential
        mock_http = MagicMock()
        connector = make_connector(store, http_client=mock_http)

        # Bypass connect() to inject a fake account directly
        from openvitals.connectors.base import Account
        account = Account(account_ref=ACCOUNT_REF, source="strava")

        with pytest.raises(AuthRequired):
            list(connector.fetch(account))

    def test_scope_comma_delimited_parsing(self):
        """Strava returns 'read,activity:read_all' — must parse correctly."""
        connector = make_connector()
        # Should not raise
        connector._check_scopes("read,activity:read_all")

    def test_scope_with_spaces_after_comma(self):
        """Handle 'read, activity:read_all' with a space after the comma."""
        connector = make_connector()
        # Should not raise — strip() handles extra whitespace
        connector._check_scopes("read, activity:read_all")

    def test_scope_missing_raises_missing_scope(self):
        connector = make_connector()
        with pytest.raises(MissingScope) as exc_info:
            connector._check_scopes("read,profile:read_all")
        assert exc_info.value.required_scope == REQUIRED_SCOPE
        assert "activity:read_all" not in exc_info.value.granted_scopes


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    def test_rate_limit_on_first_page_raises_rate_limited(self):
        """429 on the first page (no records yet) → RateLimited."""
        import httpx

        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())

        rate_limited_resp = MagicMock(spec=httpx.Response)
        rate_limited_resp.status_code = 429
        rate_limited_resp.headers = {"Retry-After": "60"}

        mock_http = MagicMock()
        mock_http.get.return_value = rate_limited_resp

        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())

        with pytest.raises(RateLimited):
            list(connector.fetch(account))

    def test_rate_limit_mid_stream_raises_partial_fetch_error(self):
        """429 after some records → PartialFetchError with records_fetched > 0."""
        import httpx

        activities = load_fixture("activities_page1.json")
        page1 = activities[:3]

        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())

        ok_resp = MagicMock(spec=httpx.Response)
        ok_resp.status_code = 200
        ok_resp.json.return_value = page1
        ok_resp.raise_for_status.return_value = None

        rate_resp = MagicMock(spec=httpx.Response)
        rate_resp.status_code = 429
        rate_resp.headers = {}

        mock_http = MagicMock()
        mock_http.get.side_effect = [ok_resp, rate_resp]

        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())

        with pytest.raises(PartialFetchError) as exc_info:
            list(connector.fetch(account))

        assert exc_info.value.records_fetched == 3


# ---------------------------------------------------------------------------
# Pydantic schema contract validation
# ---------------------------------------------------------------------------


class TestSchemaContract:
    def test_all_records_validate_against_schema(self):
        """Every emitted Record must be a valid Pydantic model (no ValidationError)."""
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        for record in records:
            # Serialise → deserialise as contract check
            dumped = record.model_dump()
            reloaded = Record.model_validate(dumped)
            assert reloaded.record_type == RecordType.WORKOUT
            assert isinstance(reloaded.common, WorkoutCommon)

    def test_common_type_matches_record_type(self):
        """Record.common must be WorkoutCommon for every emitted record."""
        activities = load_fixture("activities_page1.json")
        store = InMemoryStore()
        store.set("strava", ACCOUNT_REF, make_credential())
        mock_http = make_mock_http([activities])
        connector = make_connector(store, http_client=mock_http)
        account = connector.connect(make_credential())
        records = list(connector.fetch(account))

        for r in records:
            assert isinstance(r.common, WorkoutCommon)
