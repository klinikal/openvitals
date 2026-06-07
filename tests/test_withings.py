"""
Tests for the Withings connector.

Coverage:
- Fixture → valid Records for all three record types.
- Decode correctness: encoded raw values → correct decoded common values.
- raw faithfulness: raw retains the original verbatim fixture data.
- Identity rules: source_record_id follows the contract.
- Auth: MissingScope, AuthRequired on missing/invalid credentials.
- Token refresh persists rotated tokens before returning.
- No token values in any log/exception message.
"""

from __future__ import annotations

import json
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from openvitals.connectors.withings.connector import (
    WithingsConnector,
    decode_value,
    BODY_COMP_TYPES,
    CARDIO_TYPES,
    SEG_FAT_TYPE,
    SEG_MUSCLE_TYPE,
    SEGMENT_POSITIONS,
    ECG_RESULT_MAP,
)
from openvitals.errors import AuthRequired, MissingScope, ProviderPayloadError
from openvitals.schema.domains import (
    BodySegment,
    CardiovascularMarkerType,
    RecordType,
)
from openvitals.schema.records import (
    BodyMeasurementCommon,
    CardiovascularMarkerCommon,
    Record,
    SegmentalCompositionCommon,
)
from openvitals.storage.credentials import CredentialStore

UTC = timezone.utc

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "withings"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_fixture(name: str = "measuregrps.json") -> list[dict]:
    with (FIXTURES_DIR / name).open() as f:
        return json.load(f)


def make_account(ref: str = "12345678") -> Any:
    from openvitals.connectors.base import Account
    return Account(account_ref=ref, source="withings")


class InMemoryStore(CredentialStore):
    """Simple in-memory CredentialStore for tests (no encryption needed)."""

    def __init__(self, initial: dict | None = None):
        self._data: dict[str, dict] = {}
        if initial:
            for (source, ref), creds in initial.items():
                self._data[f"{source}:{ref}"] = creds

    def get(self, source: str, account_ref: str) -> dict | None:
        return self._data.get(f"{source}:{account_ref}")

    def set(self, source: str, account_ref: str, credential: dict) -> None:
        self._data[f"{source}:{account_ref}"] = credential

    def delete(self, source: str, account_ref: str) -> None:
        self._data.pop(f"{source}:{account_ref}", None)


def make_valid_creds(account_ref: str = "12345678") -> dict:
    from datetime import timedelta
    return {
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "expires_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
        "withings_user_id": account_ref,
        "scope_granted": "user.info,user.metrics",
    }


def make_connector(account_ref: str = "12345678", creds: dict | None = None) -> tuple[WithingsConnector, InMemoryStore]:
    """Return (connector, store) with valid credentials loaded."""
    c = creds or make_valid_creds(account_ref)
    store = InMemoryStore({("withings", account_ref): c})
    connector = WithingsConnector(
        credential_store=store,
        client_id="test_client_id",
        client_secret="test_client_secret",
    )
    return connector, store


def build_records_from_fixture(account_ref: str = "12345678") -> list[Record]:
    """Run the connector's internal builders over the fixture data (no HTTP)."""
    grps = load_fixture()
    connector, store = make_connector(account_ref)
    account = make_account(account_ref)
    ingested_at = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)

    records = []
    for grp in grps:
        grp_types = {m.get("type") for m in grp.get("measures", [])}
        grpid = str(grp["grpid"])
        occurred_at = datetime.fromtimestamp(grp["date"], tz=UTC)
        local_date = occurred_at.date()

        from openvitals.connectors.withings.connector import HYDRATION_TYPE
        body_types = set(BODY_COMP_TYPES.keys()) | {HYDRATION_TYPE}
        if grp_types & body_types:
            r = connector._build_body_measurement(grp, grpid, occurred_at, local_date, ingested_at, account)
            if r:
                records.append(r)

        if grp_types & set(CARDIO_TYPES.keys()):
            records.extend(
                connector._build_cardiovascular_markers(grp, grpid, occurred_at, local_date, ingested_at, account)
            )

        from openvitals.connectors.withings.connector import MEASTYPES_SEGMENTAL
        if grp_types & MEASTYPES_SEGMENTAL:
            r = connector._build_segmental_composition(grp, grpid, occurred_at, local_date, ingested_at, account)
            if r:
                records.append(r)

    return records


# ---------------------------------------------------------------------------
# Decode unit test
# ---------------------------------------------------------------------------


class TestDecodeValue:
    def test_positive_unit(self):
        assert decode_value(1, 2) == 100.0

    def test_negative_unit(self):
        # 8200 × 10^-2 = 82.0 (weight fixture)
        assert decode_value(8200, -2) == pytest.approx(82.0)

    def test_zero_unit(self):
        # 38 × 10^0 = 38 (vascular age)
        assert decode_value(38, 0) == pytest.approx(38.0)

    def test_negative_value(self):
        # Withings can return negative values for unit exponents
        assert decode_value(-500, -2) == pytest.approx(-5.0)

    def test_visceral_fat_fixture(self):
        # 7 × 10^0 = 7 (integer visceral fat index)
        assert decode_value(7, 0) == 7.0


# ---------------------------------------------------------------------------
# Fixture → Records (all three types)
# ---------------------------------------------------------------------------


class TestFixtureToRecords:
    def setup_method(self):
        self.records = build_records_from_fixture()

    def _by_type(self, rt: RecordType) -> list[Record]:
        return [r for r in self.records if r.record_type == rt]

    def test_body_measurement_emitted(self):
        bm = self._by_type(RecordType.BODY_MEASUREMENT)
        assert len(bm) == 1, f"Expected 1 body_measurement, got {len(bm)}"

    def test_cardiovascular_markers_emitted(self):
        cm = self._by_type(RecordType.CARDIOVASCULAR_MARKER)
        # Fixture has 4 cardio measures (types 91, 130, 155, 167) → 4 records
        assert len(cm) == 4, f"Expected 4 cardiovascular_marker records, got {len(cm)}"

    def test_segmental_composition_emitted(self):
        sc = self._by_type(RecordType.SEGMENTAL_COMPOSITION)
        assert len(sc) == 1, f"Expected 1 segmental_composition, got {len(sc)}"

    def test_all_records_validate_as_record(self):
        """Every emitted object must be a valid Record (envelope + domain model)."""
        for r in self.records:
            assert isinstance(r, Record)
            # Round-trip through model_validate to confirm serialisation
            r2 = Record.model_validate(r.model_dump())
            assert r2.source_record_id == r.source_record_id

    def test_all_records_have_correct_source(self):
        for r in self.records:
            assert r.source == "withings"
            assert r.domain == "body"

    def test_all_records_have_local_date(self):
        for r in self.records:
            assert r.local_date is not None
            assert isinstance(r.local_date, date)

    def test_all_records_have_occurred_at(self):
        for r in self.records:
            assert r.occurred_at is not None
            assert r.occurred_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Decode correctness — body_measurement
# ---------------------------------------------------------------------------


class TestBodyMeasurementDecode:
    def setup_method(self):
        records = build_records_from_fixture()
        self.bm = next(r for r in records if r.record_type == RecordType.BODY_MEASUREMENT)

    def test_weight_kg_decoded(self):
        # fixture: type 1, value=8200, unit=-2 → 8200 × 10^-2 = 82.0 kg
        assert self.bm.common.weight_kg == pytest.approx(82.0)

    def test_body_fat_pct_decoded(self):
        # fixture: type 6, value=185, unit=-1 → 185 × 10^-1 = 18.5 %
        assert self.bm.common.body_fat_pct == pytest.approx(18.5)

    def test_fat_mass_kg_decoded(self):
        # fixture: type 8, value=1518, unit=-2 → 1518 × 10^-2 = 15.18 kg
        assert self.bm.common.fat_mass_kg == pytest.approx(15.18)

    def test_lean_mass_kg_decoded(self):
        # fixture: type 5, value=6710, unit=-2 → 67.10 kg
        assert self.bm.common.lean_mass_kg == pytest.approx(67.10)

    def test_muscle_mass_kg_decoded(self):
        # fixture: type 76, value=6200, unit=-2 → 62.0 kg
        assert self.bm.common.muscle_mass_kg == pytest.approx(62.0)

    def test_bone_mass_kg_decoded(self):
        # fixture: type 88, value=340, unit=-2 → 3.40 kg
        assert self.bm.common.bone_mass_kg == pytest.approx(3.40)

    def test_visceral_fat_index_decoded(self):
        # fixture: type 170, value=7, unit=0 → int(7.0) = 7
        assert self.bm.common.visceral_fat_index == 7

    def test_hydration_in_source_extras_not_common(self):
        # body_water_pct in common should be None (no % available from Withings directly)
        assert self.bm.common.body_water_pct is None
        # hydration_kg should land in source_extras
        assert self.bm.source_extras is not None
        assert "hydration_kg" in self.bm.source_extras
        # fixture: type 77, value=4820, unit=-2 → 48.20 kg
        assert self.bm.source_extras["hydration_kg"] == pytest.approx(48.20)

    def test_raw_retains_original_encoded_values(self):
        """raw must be byte-faithful — encoded values, not decoded."""
        raw_measures = self.bm.raw["measures"]
        weight_measure = next(m for m in raw_measures if m["type"] == 1)
        # Original: value=8200, unit=-2 (NOT 82.0)
        assert weight_measure["value"] == 8200
        assert weight_measure["unit"] == -2

    def test_raw_equals_fixture_grp_verbatim(self):
        """raw must be the exact fixture grp dict — no structuring or dropping."""
        fixture_grp = load_fixture()[0]
        assert self.bm.raw == fixture_grp


# ---------------------------------------------------------------------------
# Decode correctness — cardiovascular_marker
# ---------------------------------------------------------------------------


class TestCardiovascularMarkerDecode:
    def setup_method(self):
        records = build_records_from_fixture()
        self.cm_records = [r for r in records if r.record_type == RecordType.CARDIOVASCULAR_MARKER]
        self.by_marker = {r.common.marker_type: r for r in self.cm_records}

    def test_vascular_age_decoded(self):
        # fixture: type 155, value=38, unit=0 → int(38.0) = 38
        r = self.by_marker[CardiovascularMarkerType.VASCULAR_AGE]
        assert r.common.value == 38
        assert r.common.unit == "years"

    def test_pulse_wave_velocity_decoded(self):
        # fixture: type 91, value=842, unit=-2 → 8.42 m/s
        r = self.by_marker[CardiovascularMarkerType.PULSE_WAVE_VELOCITY]
        assert r.common.value == pytest.approx(8.42)
        assert r.common.unit == "mps"

    def test_ecg_result_categorical(self):
        # fixture: type 130, value=0, unit=0 → "normal_sinus_rhythm"
        r = self.by_marker[CardiovascularMarkerType.ECG_RESULT]
        assert r.common.value == "normal_sinus_rhythm"
        assert r.common.unit is None

    def test_nerve_health_score_decoded(self):
        # fixture: type 167, value=920, unit=-1 → 92.0
        r = self.by_marker[CardiovascularMarkerType.NERVE_HEALTH_SCORE]
        assert r.common.value == pytest.approx(92.0)

    def test_raw_retains_original_encoded_values(self):
        """raw must keep encoded values for all cardio measures."""
        pwv_record = self.by_marker[CardiovascularMarkerType.PULSE_WAVE_VELOCITY]
        raw_measures = pwv_record.raw["measures"]
        pwv_measure = next(m for m in raw_measures if m["type"] == 91)
        # Original: value=842, unit=-2 (NOT 8.42)
        assert pwv_measure["value"] == 842
        assert pwv_measure["unit"] == -2

    def test_raw_equals_fixture_grp_verbatim(self):
        """All cardio records share the same fixture grp (grpid 111002)."""
        fixture_grp = load_fixture()[1]
        for r in self.cm_records:
            assert r.raw == fixture_grp


# ---------------------------------------------------------------------------
# Identity rules
# ---------------------------------------------------------------------------


class TestIdentityRules:
    def setup_method(self):
        self.records = build_records_from_fixture()

    def _by_type(self, rt: RecordType) -> list[Record]:
        return [r for r in self.records if r.record_type == rt]

    def test_body_measurement_id_is_native_grpid(self):
        bm = self._by_type(RecordType.BODY_MEASUREMENT)[0]
        # Fixture grpid = 111001
        assert bm.source_record_id == "111001"

    def test_cardiovascular_marker_id_format(self):
        # Should be f"{grpid}:{marker_type}"
        cm_records = self._by_type(RecordType.CARDIOVASCULAR_MARKER)
        for r in cm_records:
            marker_type_str = r.common.marker_type.value
            expected = f"111002:{marker_type_str}"
            assert r.source_record_id == expected, (
                f"Expected '{expected}', got '{r.source_record_id}'"
            )

    def test_segmental_composition_id_format(self):
        sc = self._by_type(RecordType.SEGMENTAL_COMPOSITION)[0]
        assert sc.source_record_id == "111003:segmental_composition"

    def test_source_record_ids_are_stable(self):
        """Re-building from same fixture must produce identical IDs."""
        records2 = build_records_from_fixture()
        ids1 = sorted(r.source_record_id for r in self.records)
        ids2 = sorted(r.source_record_id for r in records2)
        assert ids1 == ids2


# ---------------------------------------------------------------------------
# Segmental composition
# ---------------------------------------------------------------------------


class TestSegmentalComposition:
    def setup_method(self):
        records = build_records_from_fixture()
        self.sc = next(r for r in records if r.record_type == RecordType.SEGMENTAL_COMPOSITION)

    def test_five_segments_present(self):
        assert len(self.sc.common.segments) == 5

    def test_segment_names(self):
        names = {s.segment for s in self.sc.common.segments}
        assert names == {
            BodySegment.LEFT_ARM,
            BodySegment.RIGHT_ARM,
            BodySegment.TRUNK,
            BodySegment.LEFT_LEG,
            BodySegment.RIGHT_LEG,
        }

    def test_left_arm_fat_pct_decoded(self):
        # fixture left_arm: fat_mass = 320 × 10^-2 = 3.20 kg, muscle = 3200 × 10^-2 = 32.0 kg
        # fat_pct = 3.20 / (3.20 + 32.0) * 100 = 9.09 %
        left_arm = next(s for s in self.sc.common.segments if s.segment == BodySegment.LEFT_ARM)
        assert left_arm.fat_pct == pytest.approx(9.09, abs=0.01)

    def test_trunk_muscle_mass_kg_decoded(self):
        # fixture trunk: muscle = 2800 × 10^-2 = 28.0 kg
        trunk = next(s for s in self.sc.common.segments if s.segment == BodySegment.TRUNK)
        assert trunk.muscle_mass_kg == pytest.approx(28.0)

    def test_raw_retains_original_encoded_segmental_values(self):
        raw_measures = self.sc.raw["measures"]
        left_arm_fat = next(
            m for m in raw_measures if m["type"] == SEG_FAT_TYPE and m.get("fm") == 1
        )
        # Original: value=320, unit=-2 (NOT 3.20)
        assert left_arm_fat["value"] == 320
        assert left_arm_fat["unit"] == -2

    def test_raw_equals_fixture_grp_verbatim(self):
        fixture_grp = load_fixture()[2]
        assert self.sc.raw == fixture_grp

    def test_segments_sorted_canonical_order(self):
        expected_order = [
            BodySegment.LEFT_ARM,
            BodySegment.RIGHT_ARM,
            BodySegment.TRUNK,
            BodySegment.LEFT_LEG,
            BodySegment.RIGHT_LEG,
        ]
        actual_order = [s.segment for s in self.sc.common.segments]
        assert actual_order == expected_order


# ---------------------------------------------------------------------------
# Auth — connect()
# ---------------------------------------------------------------------------


class TestConnectAuth:
    def test_empty_credentials_raises_auth_required(self):
        connector, _ = make_connector()
        with pytest.raises(AuthRequired):
            connector.connect({})

    def test_none_credentials_raises_auth_required(self):
        connector, _ = make_connector()
        with pytest.raises(AuthRequired):
            connector.connect(None)

    def test_missing_key_raises_auth_required(self):
        connector, _ = make_connector()
        creds = make_valid_creds()
        del creds["refresh_token"]
        with pytest.raises(AuthRequired):
            connector.connect(creds)

    def test_missing_scope_raises_missing_scope(self):
        connector, _ = make_connector()
        creds = make_valid_creds()
        creds["scope_granted"] = "user.info"  # missing user.metrics
        with pytest.raises(MissingScope) as exc_info:
            connector.connect(creds)
        assert "user.metrics" in str(exc_info.value)

    def test_valid_credentials_returns_account(self):
        from datetime import timedelta
        connector, _ = make_connector()
        creds = make_valid_creds()
        account = connector.connect(creds)
        assert account.source == "withings"
        assert account.account_ref == "12345678"

    def test_token_never_logged_in_missing_scope_error(self):
        """Token values must not appear in exception messages."""
        connector, _ = make_connector()
        creds = make_valid_creds()
        creds["scope_granted"] = "user.info"
        try:
            connector.connect(creds)
        except MissingScope as e:
            assert "test_access_token" not in str(e)
            assert "test_refresh_token" not in str(e)

    def test_expired_token_triggers_refresh(self):
        """An expired access token must trigger _refresh_token, not AuthRequired."""
        from datetime import timedelta

        creds = make_valid_creds()
        creds["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()  # expired
        store = InMemoryStore({("withings", "12345678"): creds})
        connector = WithingsConnector(
            credential_store=store,
            client_id="test_client_id",
            client_secret="test_client_secret",
        )

        new_expires = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": 0,
            "body": {
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 10800,
            },
        }

        with patch("httpx.post", return_value=mock_response) as mock_post:
            account = connector.connect(creds)

        assert account.account_ref == "12345678"
        mock_post.assert_called_once()

    def test_refresh_persists_new_tokens(self):
        """Rotated tokens must be persisted to the store before returning."""
        from datetime import timedelta

        creds = make_valid_creds()
        creds["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        store = InMemoryStore({("withings", "12345678"): creds})
        connector = WithingsConnector(
            credential_store=store,
            client_id="test_client_id",
            client_secret="test_client_secret",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": 0,
            "body": {
                "access_token": "rotated_access",
                "refresh_token": "rotated_refresh",
                "expires_in": 10800,
            },
        }

        with patch("httpx.post", return_value=mock_response):
            connector.connect(creds)

        persisted = store.get("withings", "12345678")
        assert persisted is not None
        assert persisted["access_token"] == "rotated_access"
        assert persisted["refresh_token"] == "rotated_refresh"

    def test_refresh_token_never_logged_in_auth_required(self):
        """If refresh fails, the error message must not contain token values."""
        from datetime import timedelta

        creds = make_valid_creds()
        creds["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        store = InMemoryStore({("withings", "12345678"): creds})
        connector = WithingsConnector(
            credential_store=store,
            client_id="test_client_id",
            client_secret="test_client_secret",
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": 101}  # invalid token

        with patch("httpx.post", return_value=mock_response):
            try:
                connector.connect(creds)
            except AuthRequired as e:
                assert "test_refresh_token" not in str(e)
                assert "test_access_token" not in str(e)
            else:
                pytest.fail("Expected AuthRequired to be raised")


# ---------------------------------------------------------------------------
# fetch() with mocked HTTP
# ---------------------------------------------------------------------------


class TestFetch:
    def _make_getmeas_response(self, grps: list[dict]) -> MagicMock:
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {
            "status": 0,
            "body": {"measuregrps": grps},
        }
        return mock

    def test_fetch_yields_records_from_fixture(self):
        account_ref = "12345678"
        grps = load_fixture()
        connector, store = make_connector(account_ref)
        account = make_account(account_ref)

        with patch("httpx.post", return_value=self._make_getmeas_response(grps)):
            records = list(connector.fetch(account, since=datetime(2026, 1, 1, tzinfo=UTC)))

        assert len(records) > 0
        types = {r.record_type for r in records}
        assert RecordType.BODY_MEASUREMENT in types
        assert RecordType.CARDIOVASCULAR_MARKER in types
        assert RecordType.SEGMENTAL_COMPOSITION in types

    def test_fetch_no_data_returns_empty(self):
        account_ref = "12345678"
        connector, store = make_connector(account_ref)
        account = make_account(account_ref)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": 181}  # no data

        with patch("httpx.post", return_value=mock_resp):
            records = list(connector.fetch(account))

        assert records == []

    def test_fetch_raises_auth_required_on_no_stored_creds(self):
        store = InMemoryStore()
        connector = WithingsConnector(
            credential_store=store,
            client_id="test_client_id",
            client_secret="test_client_secret",
        )
        account = make_account("no_creds_user")
        with pytest.raises(AuthRequired):
            list(connector.fetch(account))

    def test_fetch_idempotent_same_records_same_ids(self):
        account_ref = "12345678"
        grps = load_fixture()
        connector, store = make_connector(account_ref)
        account = make_account(account_ref)

        with patch("httpx.post", return_value=self._make_getmeas_response(grps)):
            records_1 = list(connector.fetch(account))
        with patch("httpx.post", return_value=self._make_getmeas_response(grps)):
            records_2 = list(connector.fetch(account))

        ids_1 = sorted(r.source_record_id for r in records_1)
        ids_2 = sorted(r.source_record_id for r in records_2)
        assert ids_1 == ids_2


# ---------------------------------------------------------------------------
# Connector class attributes
# ---------------------------------------------------------------------------


class TestConnectorAttributes:
    def test_source_slug(self):
        store = InMemoryStore()
        c = WithingsConnector(credential_store=store)
        assert c.source == "withings"

    def test_domains(self):
        store = InMemoryStore()
        c = WithingsConnector(credential_store=store)
        assert c.domains == ["body"]
