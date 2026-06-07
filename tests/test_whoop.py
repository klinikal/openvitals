"""
Tests for the WHOOP connector.

Coverage (per connector-contract.md §6):
- Unit: fixture → Record mapping, every common field.
- Contract: every emitted Record validates against the schema.
- raw faithfulness: golden fixture asserts raw == provider payload.
- Auth: missing credentials → AuthRequired; missing scope → MissingScope;
  refresh logic and rotated-token persistence.
- score_state passthrough: SCORED, PENDING, UNSCORED all emit records.
- Sauna/wellness: activity_type=sauna, never dropped.
- Identity rules: sleep/workout use native ID; daily_recovery uses compound key.
- local_date: set correctly on all three record types.

All tests use synthetic, redacted golden fixtures (tests/fixtures/whoop/).
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

from openvitals.connectors.base import Account
from openvitals.connectors.whoop.connector import (
    WhoopConnector,
    _map_activity_type,
    _map_score_state,
    _millis_to_minutes,
    _kj_to_kcal,
)
from openvitals.errors import AuthRequired, MissingScope, ProviderPayloadError
from openvitals.schema.domains import ActivityType, RecordType, ScoreState
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

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "whoop"


def load_fixture(filename: str) -> dict:
    """Load a JSON fixture file by name and return the parsed dict."""
    path = FIXTURES_DIR / filename
    with path.open() as f:
        return json.load(f)


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
        # NEVER log credential contents
        self._data[f"{source}:{account_ref}"] = dict(credential)

    def delete(self, source: str, account_ref: str) -> None:
        self._data.pop(f"{source}:{account_ref}", None)

    def was_written(self, source: str, account_ref: str) -> bool:
        """Test helper: confirm a credential was persisted."""
        return f"{source}:{account_ref}" in self._data


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

ACCOUNT_REF = "whoop_user_12345"

VALID_CREDENTIALS: dict[str, Any] = {
    "access_token": "test_access_token",
    "refresh_token": "test_refresh_token",
    "expires_at": time.time() + 7200,  # 2 hours from now — not near expiry
    "client_id": "test_client_id",
    "client_secret": "test_client_secret",
    "scopes": [
        "read:recovery",
        "read:cycles",
        "read:sleep",
        "read:workout",
        "read:profile",
        "offline",
    ],
}

ALL_REQUIRED_SCOPES = [
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "offline",
]


def make_account(account_ref: str = ACCOUNT_REF) -> Account:
    return Account(account_ref=account_ref, source="whoop")


def make_connector(store: CredentialStore | None = None) -> WhoopConnector:
    if store is None:
        store = _InMemoryStore(
            initial={(("whoop", ACCOUNT_REF)): dict(VALID_CREDENTIALS)}
        )
    return WhoopConnector(credential_store=store)


# ---------------------------------------------------------------------------
# Unit tests — utility functions
# ---------------------------------------------------------------------------


class TestUtilityFunctions:
    def test_map_score_state_scored(self):
        assert _map_score_state("SCORED") == ScoreState.SCORED

    def test_map_score_state_pending(self):
        assert _map_score_state("PENDING_SCORE") == ScoreState.PENDING

    def test_map_score_state_unscored(self):
        assert _map_score_state("UNSCORABLE") == ScoreState.UNSCORED

    def test_map_score_state_none(self):
        assert _map_score_state(None) is None

    def test_map_score_state_unknown_raises(self):
        """An unrecognised WHOOP score_state must raise ProviderPayloadError — never silently pass."""
        with pytest.raises(ProviderPayloadError, match="Unrecognised WHOOP score_state"):
            _map_score_state("NEW_STATE_WE_DIDNT_KNOW_ABOUT")

    def test_map_activity_type_run(self):
        assert _map_activity_type("running") == ActivityType.RUN

    def test_map_activity_type_sauna(self):
        assert _map_activity_type("sauna") == ActivityType.SAUNA

    def test_map_activity_type_unknown_falls_back_to_other(self):
        assert _map_activity_type("pickleball") == ActivityType.OTHER

    def test_map_activity_type_none_falls_back_to_other(self):
        assert _map_activity_type(None) == ActivityType.OTHER

    def test_millis_to_minutes(self):
        assert _millis_to_minutes(3_600_000) == 60.0

    def test_millis_to_minutes_none(self):
        assert _millis_to_minutes(None) is None

    def test_kj_to_kcal(self):
        # 2800 kJ * 0.239006 ≈ 669.2 kcal
        result = _kj_to_kcal(2800.0)
        assert result is not None
        assert abs(result - 669.2) < 0.5

    def test_kj_to_kcal_none(self):
        assert _kj_to_kcal(None) is None


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAuth:
    def test_connect_raises_auth_required_when_no_credentials(self):
        store = _InMemoryStore()
        connector = WhoopConnector(credential_store=store)
        with pytest.raises(AuthRequired):
            connector.connect(credentials={})

    def test_connect_raises_missing_scope_when_read_cycles_absent(self):
        """read:cycles being missing must fail loudly (classic WHOOP bug in HealthOS)."""
        bad_creds = dict(VALID_CREDENTIALS)
        bad_creds["scopes"] = [s for s in ALL_REQUIRED_SCOPES if s != "read:cycles"]
        connector = make_connector()
        with pytest.raises(MissingScope) as exc_info:
            connector.connect(credentials=bad_creds)
        assert exc_info.value.required_scope == "read:cycles"

    def test_connect_raises_missing_scope_for_any_required_scope(self):
        bad_creds = dict(VALID_CREDENTIALS)
        bad_creds["scopes"] = ["read:sleep"]  # many missing
        connector = make_connector()
        with pytest.raises(MissingScope):
            connector.connect(credentials=bad_creds)

    def test_fetch_raises_auth_required_when_no_stored_credentials(self):
        store = _InMemoryStore()  # empty — no credentials stored
        connector = WhoopConnector(credential_store=store)
        account = make_account()
        with pytest.raises(AuthRequired):
            list(connector.fetch(account))

    def test_refresh_token_persisted_to_store(self):
        """Rotated refresh token must be persisted back immediately — never discarded."""
        store = _InMemoryStore()
        connector = WhoopConnector(credential_store=store)

        old_creds = dict(VALID_CREDENTIALS)
        old_creds["expires_at"] = time.time() - 1  # already expired → will trigger refresh

        new_token_response = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
            "scope": " ".join(ALL_REQUIRED_SCOPES),
        }

        profile_response = {
            "user_id": int(ACCOUNT_REF.replace("whoop_user_", "")),
            "first_name": "Test",
            "last_name": "User",
            "email": "test@redacted.example",
        }

        with patch("openvitals.connectors.whoop.connector.httpx.post") as mock_post, \
             patch("openvitals.connectors.whoop.connector.httpx.get") as mock_get:

            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 200
            mock_post_resp.json.return_value = new_token_response
            mock_post.return_value = mock_post_resp

            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = profile_response
            mock_get.return_value = mock_get_resp

            account = connector.connect(credentials=old_creds)

        # The new refresh token must be persisted into the store
        persisted = store.get("whoop", account.account_ref)
        assert persisted is not None
        # NEVER assert the actual token value in logs — just confirm it was updated
        assert "refresh_token" in persisted
        assert persisted["refresh_token"] != old_creds["refresh_token"]

    def test_tokens_not_present_in_account_metadata(self):
        """Tokens must never appear in Account.metadata — that field is not a junk drawer."""
        store = _InMemoryStore()
        connector = WhoopConnector(credential_store=store)

        creds = dict(VALID_CREDENTIALS)
        profile_response = {
            "user_id": 12345,
            "first_name": "Test",
            "last_name": "User",
            "email": "test@redacted.example",
        }

        with patch("openvitals.connectors.whoop.connector.httpx.post") as mock_post, \
             patch("openvitals.connectors.whoop.connector.httpx.get") as mock_get:

            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 200
            mock_post_resp.json.return_value = {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": " ".join(ALL_REQUIRED_SCOPES),
            }

            # First call to httpx.get is the profile fetch
            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = profile_response
            mock_get.return_value = mock_get_resp

            # Token is near expiry so refresh triggers
            creds["expires_at"] = time.time() - 1
            mock_post.return_value = mock_post_resp
            account = connector.connect(credentials=creds)

        # No secret values should leak into metadata
        metadata_str = str(account.metadata)
        assert "access_token" not in metadata_str
        assert "refresh_token" not in metadata_str


# ---------------------------------------------------------------------------
# sleep_session mapping tests
# ---------------------------------------------------------------------------


class TestSleepMapping:
    def _make_connector_and_account(self) -> tuple[WhoopConnector, Account]:
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()
        return connector, account

    def test_scored_sleep_produces_valid_record(self):
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        ingested_at = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)

        record = connector._map_sleep(raw, account.account_ref, ingested_at)

        assert record is not None
        assert isinstance(record, Record)
        assert record.record_type == RecordType.SLEEP_SESSION
        assert record.domain == "sleep"
        assert record.source == "whoop"

    def test_sleep_source_record_id_is_native_uuid(self):
        """Identity rule: sleep uses WHOOP's native UUID as source_record_id."""
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.source_record_id == "sleep-uuid-001"

    def test_sleep_common_fields_populated(self):
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        c = record.common
        assert isinstance(c, SleepSessionCommon)
        assert c.total_sleep_minutes is not None
        assert c.time_in_bed_minutes is not None
        assert c.sleep_efficiency_pct == pytest.approx(87.3)
        assert c.stage_rem_minutes is not None
        assert c.stage_deep_minutes is not None
        assert c.stage_light_minutes is not None
        assert c.stage_awake_minutes is not None
        assert c.awakenings_count == 3
        assert c.avg_hrv_ms == pytest.approx(68.4)
        assert c.respiratory_rate_bpm == pytest.approx(15.1)
        assert c.provider_sleep_score == 84
        assert c.score_state == ScoreState.SCORED

    def test_sleep_total_sleep_calculation(self):
        """total_sleep = in_bed - awake (HealthOS reference formula)."""
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        # in_bed=30300000ms, awake=3840000ms → diff=26460000ms → 441 minutes
        assert record.common.total_sleep_minutes == 441

    def test_sleep_time_in_bed_calculation(self):
        """time_in_bed_minutes = total_in_bed_time_milli → minutes."""
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        # 30300000ms / 60000 = 505 minutes
        assert record.common.time_in_bed_minutes == 505

    def test_sleep_occurred_at_is_utc(self):
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.occurred_at.tzinfo is not None
        assert record.occurred_at.tzinfo == UTC or record.occurred_at.utcoffset().total_seconds() == 0

    def test_sleep_local_date(self):
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        # start = 2026-06-06T22:45:00Z, timezone_offset = "+01:00"
        # local time = 23:45 on 2026-06-06 → local_date = 2026-06-06
        assert record.local_date == date(2026, 6, 6)

    def test_sleep_local_date_cross_midnight(self):
        """WHOOP timezone_offset must be applied before taking the local date.

        A sleep starting at 23:30 UTC with +01:00 offset is 00:30 local time on
        the NEXT calendar day — not the UTC date. This is the cross-midnight edge case.
        """
        raw = {
            "id": "sleep-cross-midnight-001",
            "start": "2026-06-06T23:30:00Z",  # UTC 23:30 on June 6
            "end": "2026-06-07T07:00:00Z",
            "nap": False,
            "timezone_offset": "+01:00",       # local time = 00:30 on June 7
            "score_state": "SCORED",
            "score": {
                "sleep_performance_percentage": 75,
                "sleep_efficiency_percentage": 82.0,
                "respiratory_rate": 14.8,
                "hrv_rmssd_milli": 55.0,
                "stage_summary": {
                    "total_in_bed_time_milli": 27000000,
                    "total_awake_time_milli": 1800000,
                    "total_light_sleep_time_milli": 10800000,
                    "total_slow_wave_sleep_time_milli": 5400000,
                    "total_rem_sleep_time_milli": 9000000,
                    "disturbance_count": 2,
                },
            },
        }
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        # 23:30 UTC + 01:00 = 00:30 local on 2026-06-07 → local_date = 2026-06-07
        assert record.local_date == date(2026, 6, 7), (
            f"Expected local_date=2026-06-07 for 23:30Z+01:00 cross-midnight session, "
            f"got {record.local_date}"
        )

    def test_sleep_raw_is_verbatim_fixture(self):
        """raw must equal the provider payload exactly — no decoding, renaming, or dropping."""
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.raw == raw

    def test_unscored_sleep_is_emitted(self):
        """UNSCORABLE sleep must still produce a Record — never filtered."""
        raw = load_fixture("sleep_unscored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record is not None
        assert record.common.score_state == ScoreState.UNSCORED
        # Score fields should be None (score block is null)
        assert record.common.total_sleep_minutes is None
        assert record.common.provider_sleep_score is None

    def test_unscored_sleep_source_record_id(self):
        raw = load_fixture("sleep_unscored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        assert record.source_record_id == "sleep-uuid-002"

    def test_sleep_missing_id_raises_payload_error(self):
        raw = {"start": "2026-06-06T22:00:00Z", "end": "2026-06-07T07:00:00Z", "nap": False}
        connector, account = self._make_connector_and_account()
        with pytest.raises(ProviderPayloadError, match="missing 'id'"):
            connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

    def test_sleep_hrv_in_ms_not_converted(self):
        """HRV must be passed through as milliseconds — WHOOP reports rMSSD in ms already."""
        raw = load_fixture("sleep_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_sleep(raw, account.account_ref, datetime.now(UTC))

        # Fixture has hrv_rmssd_milli=68.4 — should arrive as 68.4 ms in common
        assert record.common.avg_hrv_ms == pytest.approx(68.4)


# ---------------------------------------------------------------------------
# daily_recovery mapping tests
# ---------------------------------------------------------------------------


class TestRecoveryMapping:
    def _make_connector_and_account(self) -> tuple[WhoopConnector, Account]:
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        return WhoopConnector(credential_store=store), make_account()

    def _cycles_by_id(self, *filenames: str) -> dict:
        result = {}
        for fn in filenames:
            cycle = load_fixture(fn)
            result[cycle["id"]] = cycle
        return result

    def test_scored_recovery_produces_valid_record(self):
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()

        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        assert record is not None
        assert record.record_type == RecordType.DAILY_RECOVERY
        assert record.domain == "recovery"

    def test_recovery_identity_uses_compound_key(self):
        """Identity rule: daily_recovery source_record_id = account_ref:local_date:daily_recovery."""
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()

        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        # cycle_001 starts 2026-06-06T06:00:00Z → local_date = 2026-06-06
        expected_id = f"{ACCOUNT_REF}:2026-06-06:daily_recovery"
        assert record.source_record_id == expected_id

    def test_recovery_common_fields_populated(self):
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        c = record.common
        assert isinstance(c, DailyRecoveryCommon)
        assert c.hrv_ms == pytest.approx(68.4)
        assert c.resting_heart_rate_bpm == 52
        assert c.provider_recovery_score == 77
        assert c.avg_spo2_pct == pytest.approx(97.2)
        assert c.skin_temp_celsius == pytest.approx(36.9)
        assert c.score_state == ScoreState.SCORED

    def test_recovery_hrv_in_ms_not_converted(self):
        """HRV must arrive as milliseconds — same field meaning as sleep HRV."""
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        # hrv_rmssd_milli=68.4 → hrv_ms=68.4
        assert record.common.hrv_ms == pytest.approx(68.4)

    def test_recovery_local_date(self):
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        assert record.local_date == date(2026, 6, 6)

    def test_recovery_raw_is_verbatim_fixture(self):
        """raw must be the recovery record payload verbatim — no mutation."""
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        assert record.raw == raw

    def test_pending_recovery_is_emitted(self):
        """PENDING_SCORE recovery must still emit a record — never filtered."""
        raw = load_fixture("recovery_pending.json")
        cycles = self._cycles_by_id("cycle_002.json")
        connector, account = self._make_connector_and_account()

        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        assert record is not None
        assert record.common.score_state == ScoreState.PENDING
        # All score fields should be None
        assert record.common.hrv_ms is None
        assert record.common.provider_recovery_score is None

    def test_recovery_source_extras_contains_cycle_and_sleep_ids(self):
        raw = load_fixture("recovery_scored.json")
        cycles = self._cycles_by_id("cycle_001.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_recovery(raw, cycles, account.account_ref, datetime.now(UTC))

        assert record.source_extras is not None
        assert record.source_extras["cycle_id"] == 9900001
        assert record.source_extras["sleep_id"] == "sleep-uuid-001"


# ---------------------------------------------------------------------------
# workout mapping tests
# ---------------------------------------------------------------------------


class TestWorkoutMapping:
    def _make_connector_and_account(self) -> tuple[WhoopConnector, Account]:
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        return WhoopConnector(credential_store=store), make_account()

    def test_scored_run_produces_valid_record(self):
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record is not None
        assert record.record_type == RecordType.WORKOUT
        assert record.domain == "activity"

    def test_workout_source_record_id_is_native_uuid(self):
        """Identity rule: workout uses WHOOP's native UUID as source_record_id."""
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record.source_record_id == "workout-uuid-run-001"

    def test_workout_run_common_fields(self):
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        c = record.common
        assert isinstance(c, WorkoutCommon)
        assert c.activity_type == ActivityType.RUN
        assert c.activity_label == "running"
        assert c.avg_heart_rate_bpm == 155
        assert c.max_heart_rate_bpm == 178
        assert c.distance_meters == pytest.approx(10450.0)
        assert c.elevation_gain_meters == pytest.approx(95.0)
        assert c.provider_strain == pytest.approx(13.8)
        assert c.score_state == ScoreState.SCORED

    def test_workout_duration_derived_from_timestamps(self):
        """Duration is derived from start/end (HealthOS reference pattern)."""
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        # start=07:00:00Z, end=08:05:00Z → 65.0 minutes
        assert record.common.duration_minutes == pytest.approx(65.0)

    def test_workout_energy_converted_from_kj(self):
        """Energy must be in kcal (converted from WHOOP's kilojoule field)."""
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        # 2800 kJ * 0.239006 ≈ 669.2 kcal
        assert record.common.energy_kcal is not None
        assert abs(record.common.energy_kcal - 669.2) < 1.0

    def test_workout_local_date(self):
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        # start = 2026-06-06T07:00:00Z, timezone_offset = "+01:00"
        # local time = 08:00 on 2026-06-06 → local_date = 2026-06-06
        assert record.local_date == date(2026, 6, 6)

    def test_workout_local_date_cross_midnight(self):
        """WHOOP timezone_offset must be applied to workouts too.

        A workout starting at 23:30 UTC with +01:00 offset is 00:30 local time on
        the NEXT calendar day.
        """
        raw = {
            "id": "workout-cross-midnight-001",
            "start": "2026-06-06T23:30:00Z",   # UTC 23:30 on June 6
            "end": "2026-06-07T00:30:00Z",
            "timezone_offset": "+01:00",          # local time = 00:30 on June 7
            "sport_name": "sauna",
            "sport_id": 63,
            "score_state": "SCORED",
            "score": {
                "strain": 2.0,
                "average_heart_rate": 75,
                "max_heart_rate": 90,
                "kilojoule": 100.0,
                "percent_recorded": 100.0,
            },
        }
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        # 23:30 UTC + 01:00 = 00:30 local on 2026-06-07 → local_date = 2026-06-07
        assert record.local_date == date(2026, 6, 7), (
            f"Expected local_date=2026-06-07 for 23:30Z+01:00 cross-midnight workout, "
            f"got {record.local_date}"
        )

    def test_workout_raw_is_verbatim_fixture(self):
        """raw must equal the provider payload exactly."""
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record.raw == raw

    def test_sauna_workout_emitted_with_sauna_activity_type(self):
        """Sauna must emit as ActivityType.SAUNA — never dropped (contract rule)."""
        raw = load_fixture("workout_sauna_unscored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record is not None
        assert record.common.activity_type == ActivityType.SAUNA
        assert record.common.activity_label == "sauna"
        assert record.common.score_state == ScoreState.UNSCORED

    def test_sauna_raw_preserved(self):
        raw = load_fixture("workout_sauna_unscored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record.raw == raw

    def test_pending_workout_emitted(self):
        """PENDING_SCORE workout must emit — never filtered."""
        raw = load_fixture("workout_pending.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record is not None
        assert record.common.score_state == ScoreState.PENDING
        assert record.common.activity_type == ActivityType.STRENGTH
        assert record.common.avg_heart_rate_bpm is None
        assert record.common.provider_strain is None

    def test_workout_source_extras_contains_zone_durations(self):
        raw = load_fixture("workout_run_scored.json")
        connector, account = self._make_connector_and_account()
        record = connector._map_workout(raw, account.account_ref, datetime.now(UTC))

        assert record.source_extras is not None
        assert "zone_durations" in record.source_extras
        assert record.source_extras["zone_durations"] is not None

    def test_workout_missing_id_raises_payload_error(self):
        raw = {"start": "2026-06-06T07:00:00Z", "sport_name": "running"}
        connector, account = self._make_connector_and_account()
        with pytest.raises(ProviderPayloadError, match="missing 'id'"):
            connector._map_workout(raw, account.account_ref, datetime.now(UTC))


# ---------------------------------------------------------------------------
# fetch() integration tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetch:
    """Tests for the full fetch() method, mocking httpx calls."""

    def _make_paginated_response(self, records: list[dict]) -> MagicMock:
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"records": records, "next_token": None}
        return mock

    def test_fetch_yields_all_three_record_types(self):
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()

        sleep_data = [load_fixture("sleep_scored.json")]
        recovery_data = [load_fixture("recovery_scored.json")]
        cycle_data = [load_fixture("cycle_001.json")]
        workout_data = [load_fixture("workout_run_scored.json")]

        responses = [
            self._make_paginated_response(sleep_data),     # /activity/sleep
            self._make_paginated_response(recovery_data),   # /recovery
            self._make_paginated_response(cycle_data),      # /cycle
            self._make_paginated_response(workout_data),    # /activity/workout
        ]

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=responses):
            records = list(connector.fetch(account))

        record_types = {r.record_type for r in records}
        assert RecordType.SLEEP_SESSION in record_types
        assert RecordType.DAILY_RECOVERY in record_types
        assert RecordType.WORKOUT in record_types

    def test_fetch_multiple_records_of_each_type(self):
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()

        sleep_data = [load_fixture("sleep_scored.json"), load_fixture("sleep_unscored.json")]
        recovery_data = [load_fixture("recovery_scored.json"), load_fixture("recovery_pending.json")]
        cycle_data = [load_fixture("cycle_001.json"), load_fixture("cycle_002.json")]
        workout_data = [
            load_fixture("workout_run_scored.json"),
            load_fixture("workout_sauna_unscored.json"),
            load_fixture("workout_pending.json"),
        ]

        responses = [
            self._make_paginated_response(sleep_data),
            self._make_paginated_response(recovery_data),
            self._make_paginated_response(cycle_data),
            self._make_paginated_response(workout_data),
        ]

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=responses):
            records = list(connector.fetch(account))

        sleep_records = [r for r in records if r.record_type == RecordType.SLEEP_SESSION]
        recovery_records = [r for r in records if r.record_type == RecordType.DAILY_RECOVERY]
        workout_records = [r for r in records if r.record_type == RecordType.WORKOUT]

        assert len(sleep_records) == 2
        assert len(recovery_records) == 2
        assert len(workout_records) == 3

    def test_fetch_unscored_records_emitted(self):
        """Contract rule: unscored/pending records are never filtered — consumer decides."""
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()

        responses = [
            self._make_paginated_response([load_fixture("sleep_unscored.json")]),
            self._make_paginated_response([load_fixture("recovery_pending.json")]),
            self._make_paginated_response([load_fixture("cycle_002.json")]),
            self._make_paginated_response([load_fixture("workout_sauna_unscored.json")]),
        ]

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=responses):
            records = list(connector.fetch(account))

        assert len(records) == 3  # 1 sleep + 1 recovery + 1 workout
        score_states = {r.common.score_state for r in records}
        assert ScoreState.UNSCORED in score_states or ScoreState.PENDING in score_states

    def test_fetch_all_records_validate_against_schema(self):
        """Contract: every Record emitted by fetch() validates against the schema envelope."""
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()

        responses = [
            self._make_paginated_response([load_fixture("sleep_scored.json")]),
            self._make_paginated_response([load_fixture("recovery_scored.json")]),
            self._make_paginated_response([load_fixture("cycle_001.json")]),
            self._make_paginated_response([load_fixture("workout_run_scored.json")]),
        ]

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=responses):
            records = list(connector.fetch(account))

        for record in records:
            # Serialise and re-validate to confirm full schema round-trip
            round_tripped = Record.model_validate(record.model_dump())
            assert round_tripped.record_type == record.record_type
            assert round_tripped.source_record_id == record.source_record_id

    def test_fetch_pagination_drains_all_pages(self):
        """Pagination: multi-page fetch must drain fully — no partial pages."""
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()

        sleep_1 = load_fixture("sleep_scored.json")
        sleep_2 = load_fixture("sleep_unscored.json")

        # First call returns page 1 with next_token; second call returns page 2
        page1_mock = MagicMock()
        page1_mock.status_code = 200
        page1_mock.json.return_value = {"records": [sleep_1], "next_token": "cursor_abc"}

        page2_mock = MagicMock()
        page2_mock.status_code = 200
        page2_mock.json.return_value = {"records": [sleep_2], "next_token": None}

        empty = MagicMock()
        empty.status_code = 200
        empty.json.return_value = {"records": [], "next_token": None}

        # httpx.get call order: sleep page1, sleep page2, recovery, cycle, workout
        responses = [page1_mock, page2_mock, empty, empty, empty]

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=responses):
            records = list(connector.fetch(account))

        sleep_records = [r for r in records if r.record_type == RecordType.SLEEP_SESSION]
        assert len(sleep_records) == 2

    def test_fetch_idempotent_same_source_record_ids(self):
        """Idempotency: re-fetching the same range produces the same source_record_ids."""
        store = _InMemoryStore(initial={("whoop", ACCOUNT_REF): dict(VALID_CREDENTIALS)})
        connector = WhoopConnector(credential_store=store)
        account = make_account()

        data = [load_fixture("workout_run_scored.json")]

        def make_responses():
            return [
                self._make_paginated_response([load_fixture("sleep_scored.json")]),
                self._make_paginated_response([load_fixture("recovery_scored.json")]),
                self._make_paginated_response([load_fixture("cycle_001.json")]),
                self._make_paginated_response(data),
            ]

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=make_responses()):
            records_1 = list(connector.fetch(account))

        with patch("openvitals.connectors.whoop.connector.httpx.get", side_effect=make_responses()):
            records_2 = list(connector.fetch(account))

        ids_1 = {r.source_record_id for r in records_1}
        ids_2 = {r.source_record_id for r in records_2}
        assert ids_1 == ids_2


# ---------------------------------------------------------------------------
# WhoopConnector class-level contract
# ---------------------------------------------------------------------------


class TestConnectorContract:
    def test_source_is_whoop(self):
        connector = make_connector()
        assert connector.source == "whoop"

    def test_domains(self):
        connector = make_connector()
        assert set(connector.domains) == {"sleep", "recovery", "activity"}

    def test_is_base_connector_subclass(self):
        from openvitals.connectors.base import BaseConnector
        assert issubclass(WhoopConnector, BaseConnector)
