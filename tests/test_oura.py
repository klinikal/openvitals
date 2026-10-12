"""
Tests for the Oura connector.

Coverage (per connector-contract.md §6 and the oura-connector-implementation-plan.md §13.2):
1.  connect() returns an Account with source="oura" and account_ref from personal_info.id
2.  missing required scopes raises MissingScope
3.  expired token triggers refresh and persists rotated tokens
4.  sleep record maps to valid SleepSessionCommon
5.  sleep identity uses native Oura id
6.  readiness identity uses compound account_ref:local_date:daily_recovery
7.  readiness mapping preserves contributors in source_extras
8.  workout record maps to valid WorkoutCommon
9.  workout identity uses native Oura id
10. pagination drains multiple sleep pages
11. 401 on a list endpoint raises AuthRequired
12. 429 raises RateLimited
13. transport error raises ProviderUnavailable
14. malformed payload raises ProviderPayloadError
15. fetch() emits records from all three supported Oura endpoints

All tests use synthetic, redacted golden fixtures (tests/fixtures/oura/).
No live API calls are made.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import httpx

from openvitals.connectors.base import Account
from openvitals.connectors.oura.connector import (
    OuraConnector,
    _map_activity_type,
    _parse_utc,
)
from openvitals.errors import (
    AuthRequired,
    MissingScope,
    ProviderPayloadError,
    ProviderUnavailable,
    RateLimited,
)
from openvitals.schema.domains import ActivityType, RecordType
from openvitals.schema.records import (
    DailyRecoveryCommon,
    Record,
    SleepSessionCommon,
    WorkoutCommon,
)
from openvitals.storage.credentials import CredentialStore

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "oura"


def load_fixture(filename: str) -> dict:
    """Load a JSON fixture file and return parsed dict."""
    path = FIXTURES_DIR / filename
    with path.open() as f:
        return json.load(f)


def load_sleep_item(fixture_name: str = "sleep_page1.json") -> dict:
    """Return the first item from a sleep page fixture."""
    return load_fixture(fixture_name)["data"][0]


def load_readiness_item(fixture_name: str = "daily_readiness_page.json") -> dict:
    """Return the first item from a daily_readiness page fixture."""
    return load_fixture(fixture_name)["data"][0]


def load_workout_item(fixture_name: str = "workout_page.json") -> dict:
    """Return the first item from a workout page fixture."""
    return load_fixture(fixture_name)["data"][0]


# ---------------------------------------------------------------------------
# In-memory CredentialStore for tests
# ---------------------------------------------------------------------------


class _InMemoryStore(CredentialStore):
    """Simple in-memory CredentialStore for unit tests."""

    def __init__(self, initial: dict[tuple, dict] | None = None) -> None:
        self._data: dict[str, dict] = {}
        if initial:
            for (source, account_ref), cred in initial.items():
                self.set(source, account_ref, cred)

    def get(self, source: str, account_ref: str) -> dict | None:
        return self._data.get(f"{source}:{account_ref}")

    def set(self, source: str, account_ref: str, credential: dict) -> None:
        self._data[f"{source}:{account_ref}"] = dict(credential)

    def delete(self, source: str, account_ref: str) -> None:
        self._data.pop(f"{source}:{account_ref}", None)

    def was_written(self, source: str, account_ref: str) -> bool:
        """Test helper: confirm a credential was persisted."""
        return f"{source}:{account_ref}" in self._data


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ACCOUNT_REF = "oura-user-abc123"

ALL_REQUIRED_SCOPES = ["personal", "daily", "workout"]

VALID_CREDENTIALS: dict[str, Any] = {
    "access_token": "test_oura_access_token",
    "refresh_token": "test_oura_refresh_token",
    "expires_at": time.time() + 7200,   # 2 hours from now — not near expiry
    "client_id": "test_client_id",
    "client_secret": "test_client_secret",
    "scopes": ALL_REQUIRED_SCOPES,
}


def make_account(account_ref: str = ACCOUNT_REF) -> Account:
    return Account(account_ref=account_ref, source="oura")


def make_connector(store: CredentialStore | None = None) -> OuraConnector:
    if store is None:
        store = _InMemoryStore(
            initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)}
        )
    return OuraConnector(
        credential_store=store,
        client_id="test_client_id",
        client_secret="test_client_secret",
    )


def _mock_resp(status_code: int, payload: dict | None = None, text: str = "") -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    if payload is not None:
        m.json.return_value = payload
    m.text = text
    m.headers = {}
    return m


def _personal_info_resp() -> MagicMock:
    return _mock_resp(200, load_fixture("personal_info.json"))


def _empty_list_resp() -> MagicMock:
    return _mock_resp(200, {"data": [], "next_token": None})


# ---------------------------------------------------------------------------
# Test 1 — connect() happy path
# ---------------------------------------------------------------------------


class TestConnect:
    def test_connect_returns_account_with_oura_source(self):
        """connect() must return Account with source='oura' and account_ref from personal_info.id."""
        store = _InMemoryStore()
        connector = make_connector(store)
        creds = dict(VALID_CREDENTIALS)

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.return_value = _personal_info_resp()
            account = connector.connect(credentials=creds)

        assert isinstance(account, Account)
        assert account.source == "oura"
        assert account.account_ref == ACCOUNT_REF

    def test_connect_account_ref_from_personal_info_id(self):
        """account_ref must be taken from personal_info.id, not from any token field."""
        store = _InMemoryStore()
        connector = make_connector(store)
        creds = dict(VALID_CREDENTIALS)

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.return_value = _personal_info_resp()
            account = connector.connect(credentials=creds)

        assert account.account_ref == "oura-user-abc123"


# ---------------------------------------------------------------------------
# Test 2 — missing scopes
# ---------------------------------------------------------------------------


class TestMissingScopes:
    def test_missing_any_required_scope_raises_missing_scope(self):
        """Missing any required scope must raise MissingScope loudly — not a silent empty result."""
        connector = make_connector()
        bad_creds = dict(VALID_CREDENTIALS)
        bad_creds["scopes"] = ["personal", "daily"]  # missing "workout"

        with pytest.raises(MissingScope):
            connector.connect(credentials=bad_creds)

    def test_missing_multiple_scopes_raises_missing_scope(self):
        connector = make_connector()
        bad_creds = dict(VALID_CREDENTIALS)
        bad_creds["scopes"] = ["personal"]

        with pytest.raises(MissingScope):
            connector.connect(credentials=bad_creds)

    def test_empty_credentials_raises_auth_required(self):
        connector = make_connector()
        with pytest.raises(AuthRequired):
            connector.connect(credentials={})


# ---------------------------------------------------------------------------
# Test 3 — token refresh and persistence
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    def test_expired_token_triggers_refresh_and_persists(self):
        """Expired token must trigger refresh and persist the new rotated token immediately."""
        store = _InMemoryStore()
        connector = make_connector(store)

        old_creds = dict(VALID_CREDENTIALS)
        old_creds["expires_at"] = time.time() - 1  # already expired

        refresh_payload = load_fixture("token_refresh.json")
        personal_info = load_fixture("personal_info.json")

        with patch("openvitals.connectors.oura.connector.httpx.post") as mock_post, \
             patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_post.return_value = _mock_resp(200, refresh_payload)
            mock_get.return_value = _mock_resp(200, personal_info)

            account = connector.connect(credentials=old_creds)

        # New credentials must be persisted to the store
        persisted = store.get("oura", account.account_ref)
        assert persisted is not None
        assert "refresh_token" in persisted
        # Rotated token must differ from the old one
        assert persisted["refresh_token"] != old_creds["refresh_token"]

    def test_refresh_fails_raises_auth_required(self):
        """A 401 on the token refresh endpoint must raise AuthRequired."""
        store = _InMemoryStore()
        connector = make_connector(store)

        old_creds = dict(VALID_CREDENTIALS)
        old_creds["expires_at"] = time.time() - 1

        with patch("openvitals.connectors.oura.connector.httpx.post") as mock_post:
            mock_post.return_value = _mock_resp(401, text="Unauthorized")
            with pytest.raises(AuthRequired):
                connector.connect(credentials=old_creds)

    def test_tokens_not_in_account_metadata(self):
        """Token values must never appear in Account.metadata."""
        store = _InMemoryStore()
        connector = make_connector(store)
        creds = dict(VALID_CREDENTIALS)

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.return_value = _personal_info_resp()
            account = connector.connect(credentials=creds)

        assert "access_token" not in account.metadata
        assert "refresh_token" not in account.metadata


# ---------------------------------------------------------------------------
# Test 4 — sleep mapping → valid SleepSessionCommon
# ---------------------------------------------------------------------------


class TestSleepMapping:
    def _make_connector_and_account(self) -> tuple[OuraConnector, Account]:
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        return make_connector(store), make_account()

    def test_sleep_maps_to_sleep_session_record(self):
        """Sleep fixture must produce a valid Record with record_type=SLEEP_SESSION."""
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()
        ingested_at = datetime.now(UTC)

        record = connector._map_sleep(raw, account.account_ref, ingested_at)

        assert isinstance(record, Record)
        assert record.record_type == RecordType.SLEEP_SESSION
        assert record.domain == "sleep"
        assert record.source == "oura"

    def test_sleep_common_is_sleep_session_common(self):
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert isinstance(record.common, SleepSessionCommon)

    def test_sleep_common_fields_populated(self):
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_sleep(
            raw, account.account_ref, datetime.now(UTC), {"2026-06-06": 82}
        )
        c = record.common

        # sleep_page1 has total_sleep_duration=28800s → 480 min
        assert c.total_sleep_minutes == 480
        # time_in_bed=30600s → 510 min
        assert c.time_in_bed_minutes == 510
        # efficiency=94
        assert c.sleep_efficiency_pct == 94
        # latency=600s → 10 min
        assert c.sleep_latency_minutes == 10
        # rem=9000s → 150 min
        assert c.stage_rem_minutes == 150
        # deep=5400s → 90 min
        assert c.stage_deep_minutes == 90
        # light=12600s → 210 min
        assert c.stage_light_minutes == 210
        # awake=1800s → 30 min
        assert c.stage_awake_minutes == 30
        # avg_heart_rate_bpm=56
        assert c.avg_heart_rate_bpm == 56
        # avg_hrv_ms=65.2
        assert c.avg_hrv_ms == pytest.approx(65.2)
        # respiratory_rate_bpm=14.8
        assert c.respiratory_rate_bpm == pytest.approx(14.8)
        # score comes from daily_sleep, joined by day
        assert c.provider_sleep_score == 82

    def test_sleep_score_none_without_daily_sleep_match(self):
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))
        assert record.common.provider_sleep_score is None

    def test_sleep_raw_is_verbatim(self):
        """raw must equal the provider payload — no decoding, renaming, or dropping."""
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.raw == raw

    def test_sleep_occurred_at_is_utc(self):
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.occurred_at.tzinfo is not None
        assert record.occurred_at.utcoffset().total_seconds() == 0

    def test_sleep_local_date_from_day_field(self):
        raw = load_sleep_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        # sleep_page1 has day="2026-06-06"
        assert record.local_date == date(2026, 6, 6)


# ---------------------------------------------------------------------------
# Test 5 — sleep identity uses native Oura id
# ---------------------------------------------------------------------------


class TestSleepIdentity:
    def test_sleep_source_record_id_is_native_oura_id(self):
        """Identity rule: sleep source_record_id must be the native Oura UUID."""
        raw = load_sleep_item()
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.source_record_id == "oura-sleep-001"


# ---------------------------------------------------------------------------
# Test 6 — readiness identity uses compound key
# ---------------------------------------------------------------------------


class TestReadinessIdentity:
    def test_readiness_source_record_id_is_compound_key(self):
        """Identity rule: daily_recovery source_record_id = account_ref:day:daily_recovery."""
        raw = load_readiness_item()
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        record = connector._map_readiness(raw, account.account_ref, datetime.now(UTC))

        expected_id = f"{ACCOUNT_REF}:2026-06-07:daily_recovery"
        assert record.source_record_id == expected_id


# ---------------------------------------------------------------------------
# Test 7 — readiness contributors in source_extras
# ---------------------------------------------------------------------------


class TestReadinessMapping:
    def _make_connector_and_account(self) -> tuple[OuraConnector, Account]:
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        return make_connector(store), make_account()

    def test_readiness_contributors_in_source_extras(self):
        """contributors dict must be preserved in source_extras."""
        raw = load_readiness_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_readiness(raw, account.account_ref, datetime.now(UTC))

        assert record.source_extras is not None
        assert "contributors" in record.source_extras
        assert isinstance(record.source_extras["contributors"], dict)
        # Fixture has hrv_balance=72
        assert record.source_extras["contributors"].get("hrv_balance") == 72

    def test_readiness_common_fields_populated(self):
        raw = load_readiness_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_readiness(raw, account.account_ref, datetime.now(UTC))
        c = record.common

        assert isinstance(c, DailyRecoveryCommon)
        assert c.provider_recovery_score == 76
        assert c.skin_temp_celsius == pytest.approx(0.12)

    def test_readiness_record_type_is_daily_recovery(self):
        raw = load_readiness_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_readiness(raw, account.account_ref, datetime.now(UTC))

        assert record.record_type == RecordType.DAILY_RECOVERY
        assert record.domain == "recovery"

    def test_readiness_raw_is_verbatim(self):
        raw = load_readiness_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_readiness(raw, account.account_ref, datetime.now(UTC))

        assert record.raw == raw


# ---------------------------------------------------------------------------
# Test 8 — workout record maps to valid WorkoutCommon
# ---------------------------------------------------------------------------


class TestWorkoutMapping:
    def _make_connector_and_account(self) -> tuple[OuraConnector, Account]:
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        return make_connector(store), make_account()

    def test_workout_maps_to_workout_record(self):
        """Workout fixture must produce a valid Record with record_type=WORKOUT."""
        raw = load_workout_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert isinstance(record, Record)
        assert record.record_type == RecordType.WORKOUT
        assert record.domain == "activity"
        assert record.source == "oura"

    def test_workout_common_is_workout_common(self):
        raw = load_workout_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert isinstance(record.common, WorkoutCommon)

    def test_workout_common_fields_populated(self):
        raw = load_workout_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))
        c = record.common

        # activity=running → ActivityType.RUN
        assert c.activity_type == ActivityType.RUN
        # activity_label preserved
        assert c.activity_label == "running"
        # duration: 07:00 to 08:05 = 65 min
        assert c.duration_minutes == pytest.approx(65.0)
        # distance=8500.0
        assert c.distance_meters == pytest.approx(8500.0)
        # calories=520
        assert c.energy_kcal == pytest.approx(520.0)

    def test_workout_raw_is_verbatim(self):
        raw = load_workout_item()
        connector, account = self._make_connector_and_account()

        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record.raw == raw

    def test_workout_activity_type_mapping(self):
        """Activity type helper must map Oura strings correctly."""
        assert _map_activity_type("running") == ActivityType.RUN
        assert _map_activity_type("walking") == ActivityType.WALK
        assert _map_activity_type("cycling") == ActivityType.RIDE
        assert _map_activity_type("swimming") == ActivityType.SWIM
        assert _map_activity_type("hiking") == ActivityType.HIKE
        assert _map_activity_type("strength_training") == ActivityType.STRENGTH
        assert _map_activity_type("yoga") == ActivityType.OTHER
        assert _map_activity_type(None) == ActivityType.OTHER


# ---------------------------------------------------------------------------
# Test 9 — workout identity uses native Oura id
# ---------------------------------------------------------------------------


class TestWorkoutIdentity:
    def test_workout_source_record_id_is_native_oura_id(self):
        """Identity rule: workout source_record_id must be the native Oura UUID."""
        raw = load_workout_item()
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record.source_record_id == "oura-workout-001"


# ---------------------------------------------------------------------------
# Test 10 — pagination drains multiple sleep pages
# ---------------------------------------------------------------------------


class TestPagination:
    def test_pagination_drains_all_sleep_pages(self):
        """Multi-page fetch must drain both pages fully, producing 2 sleep records."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        page1 = load_fixture("sleep_page1.json")
        page2 = load_fixture("sleep_page2.json")

        # Two sleep pages, then empty readiness and workout pages
        page1_resp = _mock_resp(200, page1)
        page2_resp = _mock_resp(200, page2)

        def side_effect(*args, **kwargs):
            params = kwargs.get("params", {})
            if "next_token" in params:
                return page2_resp
            return page1_resp

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            # sleep page1 (returns next_token), sleep page2 (no next_token),
            # readiness (empty), workout (empty)
            mock_get.side_effect = [
                page1_resp,
                page2_resp,
                _empty_list_resp(),  # daily_sleep
                _empty_list_resp(),
                _empty_list_resp(),
            ]
            records = list(connector.fetch(account))

        sleep_records = [r for r in records if r.record_type == RecordType.SLEEP_SESSION]
        assert len(sleep_records) == 2
        ids = {r.source_record_id for r in sleep_records}
        assert "oura-sleep-001" in ids
        assert "oura-sleep-002" in ids


# ---------------------------------------------------------------------------
# Test 11 — 401 on list endpoint raises AuthRequired
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_on_list_raises_auth_required(self):
        """A 401 response on a list endpoint must raise AuthRequired."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.return_value = _mock_resp(401)
            with pytest.raises(AuthRequired):
                list(connector.fetch(account))

    # Test 12 — 429 raises RateLimited
    def test_429_raises_rate_limited(self):
        """A 429 response must raise RateLimited."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        rate_limit_resp = MagicMock()
        rate_limit_resp.status_code = 429
        rate_limit_resp.headers = {"Retry-After": "60"}

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.return_value = rate_limit_resp
            with pytest.raises(RateLimited):
                list(connector.fetch(account))

    # Test 13 — transport error raises ProviderUnavailable
    def test_transport_error_raises_provider_unavailable(self):
        """A transport-level error must raise ProviderUnavailable."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.side_effect = httpx.TransportError("Connection refused")
            with pytest.raises(ProviderUnavailable):
                list(connector.fetch(account))

    # Test 14 — malformed payload raises ProviderPayloadError
    def test_malformed_payload_raises_provider_payload_error(self):
        """A response missing the 'data' key must raise ProviderPayloadError."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        # Return a response that looks like a 200 but has unexpected structure
        bad_resp = _mock_resp(200, {"unexpected_key": "oops"})

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.return_value = bad_resp
            with pytest.raises(ProviderPayloadError):
                list(connector.fetch(account))


# ---------------------------------------------------------------------------
# Test 15 — fetch() emits records from all three endpoints
# ---------------------------------------------------------------------------


class TestFetchAllDomains:
    def test_fetch_joins_sleep_score_from_daily_sleep(self):
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.side_effect = [
                _mock_resp(200, load_fixture("sleep_page2.json")),  # day 2026-06-07
                _mock_resp(200, {"data": [{"day": "2026-06-07", "score": 77}], "next_token": None}),
                _empty_list_resp(),
                _empty_list_resp(),
            ]
            records = list(connector.fetch(make_account()))
        assert records[0].common.provider_sleep_score == 77

    def test_fetch_emits_records_from_all_three_endpoints(self):
        """fetch() must emit records from sleep, readiness, and workout endpoints."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        sleep_page = load_fixture("sleep_page2.json")  # single page, no next_token
        readiness_page = load_fixture("daily_readiness_page.json")
        workout_page = load_fixture("workout_page.json")

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.side_effect = [
                _mock_resp(200, sleep_page),
                _empty_list_resp(),  # daily_sleep
                _mock_resp(200, readiness_page),
                _mock_resp(200, workout_page),
            ]
            records = list(connector.fetch(account))

        record_types = {r.record_type for r in records}
        assert RecordType.SLEEP_SESSION in record_types
        assert RecordType.DAILY_RECOVERY in record_types
        assert RecordType.WORKOUT in record_types
        assert len(records) == 3

    def test_fetch_all_records_validate_against_schema(self):
        """Contract: every Record from fetch() must validate against the schema."""
        store = _InMemoryStore(initial={("oura", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = make_connector(store)
        account = make_account()

        with patch("openvitals.connectors.oura.connector.httpx.get") as mock_get:
            mock_get.side_effect = [
                _mock_resp(200, load_fixture("sleep_page2.json")),
                _empty_list_resp(),  # daily_sleep
                _mock_resp(200, load_fixture("daily_readiness_page.json")),
                _mock_resp(200, load_fixture("workout_page.json")),
            ]
            records = list(connector.fetch(account))

        for record in records:
            round_tripped = Record.model_validate(record.model_dump())
            assert round_tripped.record_type == record.record_type
            assert round_tripped.source_record_id == record.source_record_id

    def test_fetch_raises_auth_required_with_no_stored_credentials(self):
        """fetch() with no stored credentials must raise AuthRequired."""
        store = _InMemoryStore()   # empty
        connector = make_connector(store)
        account = make_account()

        with pytest.raises(AuthRequired):
            list(connector.fetch(account))


# ---------------------------------------------------------------------------
# Connector class-level contract
# ---------------------------------------------------------------------------


class TestConnectorContract:
    def test_source_is_oura(self):
        connector = make_connector()
        assert connector.source == "oura"

    def test_domains(self):
        connector = make_connector()
        assert set(connector.domains) == {"sleep", "recovery", "activity"}

    def test_is_base_connector_subclass(self):
        from openvitals.connectors.base import BaseConnector
        assert issubclass(OuraConnector, BaseConnector)
