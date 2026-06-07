"""
Tests for the CronometerConnector.

Coverage (all offline — no live API calls):
- Fixtures → valid food_entry / daily_summary / food_definition Records
- food_id → food_name resolution (opaque IDs resolved via get_food)
- nutrient_id → canonical Micronutrients key mapping (spot-checks ≥ 6 keys)
- raw byte-faithfulness (food_entry raw == diary entry; food_definition raw includes full payload)
- Record identity rules (food_entry = servingId; food_definition = food_id; daily_summary = compound)
- local_date set on all emitted records
- Session expiry: re-login path and AuthRequired on double failure
- Opt-in gate: connector refuses to instantiate without enabled=True
- Meal group decoding
- food_category preserved as string

Design:
- All HTTP calls are monkeypatched — no network required.
- _CronometerSession and the login helper are patched at the connector module level.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openvitals.connectors.cronometer.connector import (
    NUTRIENT_ID_TO_CANONICAL,
    CronometerConnector,
    _build_micronutrients,
    _build_full_nutrient_list,
    _get_reference_grams,
    _map_source,
    _scale_nutrient,
)
from openvitals.errors import AuthRequired
from openvitals.schema.domains import Meal, RecordType
from openvitals.schema.records import Micronutrients, Record

UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures" / "cronometer"


# ---------------------------------------------------------------------------
# Load fixtures
# ---------------------------------------------------------------------------


def load_fixture(name: str) -> Any:
    with (FIXTURES / name).open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def nutrient_dict_raw():
    """Raw fixture: {str(id) → {...}} — as stored in JSON."""
    return load_fixture("nutrient_dict.json")


@pytest.fixture(scope="module")
def nutrient_dict(nutrient_dict_raw):
    """Int-keyed nutrient dict matching login() output shape."""
    return {int(k): v for k, v in nutrient_dict_raw.items()}


@pytest.fixture(scope="module")
def diary_response():
    return load_fixture("get_diary_response.json")


@pytest.fixture(scope="module")
def food_response():
    return load_fixture("get_food_response.json")


# ---------------------------------------------------------------------------
# Fake credential store
# ---------------------------------------------------------------------------


class FakeStore:
    """In-memory CredentialStore stub."""

    def __init__(self, initial: dict | None = None):
        self._data: dict[str, dict] = {}
        if initial:
            self._data.update(initial)

    def get(self, source: str, account_ref: str) -> dict | None:
        return self._data.get(f"{source}:{account_ref}")

    def set(self, source: str, account_ref: str, credential: dict) -> None:
        self._data[f"{source}:{account_ref}"] = credential

    def delete(self, source: str, account_ref: str) -> None:
        self._data.pop(f"{source}:{account_ref}", None)


# ---------------------------------------------------------------------------
# Helper: run a full fetch with mocked HTTP
# ---------------------------------------------------------------------------


def _make_connector(store: FakeStore) -> CronometerConnector:
    return CronometerConnector(credential_store=store, enabled=True)


def _fake_login_result(user_id: int = 42, nutrient_dict: dict | None = None):
    """Return a (user_id, token, nutrient_dict) triple matching _do_login output."""
    return user_id, "fake-session-token", nutrient_dict or {}


def collect_records(
    connector: CronometerConnector,
    account_ref: str,
    diary: dict,
    food: dict,
    nutrient_dict: dict,
    fetch_date: date = date(2026, 6, 7),
) -> list[Record]:
    """Run fetch for a single day using patched session + login."""
    from openvitals.connectors.base import Account

    account = Account(
        account_ref=account_ref,
        source="cronometer",
        metadata={"cronometer_user_id": int(account_ref), "timezone": "UTC"},
    )

    # Patch _do_login and _CronometerSession at module level
    with (
        patch(
            "openvitals.connectors.cronometer.connector.CronometerConnector._do_login",
            return_value=_fake_login_result(int(account_ref), nutrient_dict),
        ),
        patch("openvitals.connectors.cronometer.connector._CronometerSession") as MockSession,
        patch("openvitals.connectors.cronometer.connector.time.sleep"),
    ):
        session_instance = MagicMock()
        session_instance.nutrient_dict = nutrient_dict
        session_instance.get_diary.return_value = diary
        session_instance.get_food.return_value = food
        MockSession.return_value = session_instance

        since_dt = datetime(fetch_date.year, fetch_date.month, fetch_date.day, 0, 0, 0, tzinfo=UTC)
        return list(connector.fetch(account, since=since_dt))


# ---------------------------------------------------------------------------
# Tests — opt-in gate
# ---------------------------------------------------------------------------


class TestOptInGate:
    def test_refuses_without_enabled(self):
        with pytest.raises(RuntimeError, match="opt-in"):
            CronometerConnector(enabled=False)

    def test_accepts_with_enabled(self):
        connector = CronometerConnector(enabled=True)
        assert connector.source == "cronometer"


# ---------------------------------------------------------------------------
# Tests — record emission from fixtures
# ---------------------------------------------------------------------------


class TestRecordEmission:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)
        self.food_entries = [r for r in self.records if r.record_type == RecordType.FOOD_ENTRY]
        self.daily_summaries = [r for r in self.records if r.record_type == RecordType.DAILY_SUMMARY]
        self.food_defs = [r for r in self.records if r.record_type == RecordType.FOOD_DEFINITION]

    def test_emits_food_entries(self):
        # 2 Serving entries in fixture (1 Activity entry is skipped)
        assert len(self.food_entries) == 2

    def test_emits_daily_summary(self):
        assert len(self.daily_summaries) == 1

    def test_emits_food_definition(self):
        # Only 1 distinct food_id in fixture → 1 food_definition
        assert len(self.food_defs) == 1

    def test_records_are_valid_schema(self):
        """Every emitted Record validates against the Pydantic schema."""
        for r in self.records:
            assert isinstance(r, Record)
            # Verify round-trip
            r2 = Record.model_validate(r.model_dump())
            assert r2.record_type == r.record_type

    def test_all_records_have_local_date(self):
        for r in self.records:
            assert r.local_date is not None
            assert r.local_date == date(2026, 6, 7)

    def test_all_records_have_occurred_at_utc(self):
        for r in self.records:
            assert r.occurred_at.tzinfo is not None

    def test_food_entry_source(self):
        for r in self.food_entries:
            assert r.source == "cronometer"
            assert r.domain == "nutrition"


# ---------------------------------------------------------------------------
# Tests — food_id → food_name resolution
# ---------------------------------------------------------------------------


class TestFoodResolution:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)
        self.food_entries = [r for r in self.records if r.record_type == RecordType.FOOD_ENTRY]

    def test_food_name_resolved(self):
        """Opaque food_id must be resolved to a human-readable name."""
        for r in self.food_entries:
            assert r.common.food_name == "Synthetic Test Grain", (
                f"Expected 'Synthetic Test Grain', got {r.common.food_name!r}. "
                "food_id resolution failed."
            )

    def test_food_ref_id_is_food_id_string(self):
        for r in self.food_entries:
            assert r.common.food_ref_id == "100001"

    def test_food_tags_resolved(self):
        for r in self.food_entries:
            assert "Whole" in r.common.food_tags
            assert "Vegan" in r.common.food_tags

    def test_food_source_resolved(self):
        for r in self.food_entries:
            # 'NCCDB:99999' → prefix 'NCCDB'
            assert r.common.food_source == "NCCDB"

    def test_food_definition_name_matches(self):
        """food_definition must carry the same resolved name."""
        defs = [r for r in self.records if r.record_type == RecordType.FOOD_DEFINITION]
        assert defs[0].common.food_name == "Synthetic Test Grain"


# ---------------------------------------------------------------------------
# Tests — nutrient ID → canonical micronutrient mapping
# ---------------------------------------------------------------------------


class TestNutrientMapping:
    """Verify the canonical micronutrient mapping using _build_micronutrients directly."""

    @pytest.fixture(autouse=True)
    def setup(self, food_response, nutrient_dict):
        self.nutrients = food_response["nutrients"]
        self.nutrient_dict = nutrient_dict
        self.micros = _build_micronutrients(self.nutrients, nutrient_dict)

    def test_iron_mg_mapped(self):
        """Nutrient ID 303 → iron_mg."""
        assert self.micros.iron_mg == pytest.approx(4.2)

    def test_magnesium_mg_mapped(self):
        """Nutrient ID 304 → magnesium_mg."""
        assert self.micros.magnesium_mg == pytest.approx(115.0)

    def test_selenium_mcg_mapped(self):
        """Nutrient ID 317 → selenium_mcg."""
        assert self.micros.selenium_mcg == pytest.approx(28.5)

    def test_vitamin_d_mcg_mapped(self):
        """Nutrient ID 328 → vitamin_d_mcg."""
        assert self.micros.vitamin_d_mcg == pytest.approx(0.0)

    def test_potassium_mg_mapped(self):
        """Nutrient ID 306 → potassium_mg."""
        assert self.micros.potassium_mg == pytest.approx(320.0)

    def test_sodium_mg_mapped(self):
        """Nutrient ID 307 → sodium_mg."""
        assert self.micros.sodium_mg == pytest.approx(5.0)

    def test_unmapped_id_ignored(self):
        """Nutrient ID 605 (trans fat, not in NUTRIENT_ID_TO_CANONICAL) → ignored safely."""
        # ID 605 is in the fixture but not in NUTRIENT_ID_TO_CANONICAL.
        # Verify no error and no canonical key is set for an unknown ID.
        nutrients_with_unknown = [{"id": 605, "amount": 99.9}]
        micros = _build_micronutrients(nutrients_with_unknown, self.nutrient_dict)
        # All canonical fields should be None — the unknown ID doesn't poison anything
        assert micros.iron_mg is None
        assert micros.vitamin_c_mg is None

    def test_micronutrients_instance_valid_schema(self):
        assert isinstance(self.micros, Micronutrients)

    def test_canonical_mapping_coverage(self):
        """NUTRIENT_ID_TO_CANONICAL must cover all canonical keys except iodine_mcg."""
        all_canonical = set(Micronutrients.model_fields.keys())
        mapped_canonical = {v[0] for v in NUTRIENT_ID_TO_CANONICAL.values()}
        # iodine_mcg is documented as unmapped
        expected_unmapped = {"iodine_mcg"}
        actually_unmapped = all_canonical - mapped_canonical
        assert actually_unmapped == expected_unmapped, (
            f"Unexpected unmapped canonical keys: {actually_unmapped - expected_unmapped}. "
            f"Newly unmapped (regression): {expected_unmapped - actually_unmapped}."
        )


# ---------------------------------------------------------------------------
# Tests — raw byte-faithfulness
# ---------------------------------------------------------------------------


class TestRawFaithfulness:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)
        self.diary_fixture = diary_response
        self.food_fixture = food_response

    def test_food_entry_raw_is_diary_entry(self):
        """food_entry raw must equal the verbatim diary entry dict."""
        food_entries = [r for r in self.records if r.record_type == RecordType.FOOD_ENTRY]
        serving_ids_in_fixture = {"srv-abc001", "srv-abc002"}
        for r in food_entries:
            assert r.raw.get("servingId") in serving_ids_in_fixture
            assert r.raw.get("type") == "Serving"
            # raw must not be decoded or restructured
            assert "foodId" in r.raw  # original key, not renamed

    def test_daily_summary_raw_is_full_diary_response(self):
        """daily_summary raw must equal the full diary response."""
        summaries = [r for r in self.records if r.record_type == RecordType.DAILY_SUMMARY]
        assert len(summaries) == 1
        assert summaries[0].raw == self.diary_fixture

    def test_food_definition_raw_includes_original_payload(self):
        """food_definition raw must include the original get_food payload keys."""
        defs = [r for r in self.records if r.record_type == RecordType.FOOD_DEFINITION]
        assert len(defs) == 1
        raw = defs[0].raw
        # Original keys from get_food response must be preserved
        assert raw.get("id") == self.food_fixture["id"]
        assert raw.get("name") == self.food_fixture["name"]
        assert raw.get("nutrients") == self.food_fixture["nutrients"]
        # _normalized_nutrients is appended (internal plumbing, not a replacement)
        assert "_normalized_nutrients" in raw


# ---------------------------------------------------------------------------
# Tests — Record identity rules
# ---------------------------------------------------------------------------


class TestIdentityRules:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)

    def test_food_entry_id_is_serving_id(self):
        food_entries = [r for r in self.records if r.record_type == RecordType.FOOD_ENTRY]
        ids = {r.source_record_id for r in food_entries}
        assert "srv-abc001" in ids
        assert "srv-abc002" in ids

    def test_food_definition_id_is_food_id(self):
        defs = [r for r in self.records if r.record_type == RecordType.FOOD_DEFINITION]
        assert defs[0].source_record_id == "100001"

    def test_daily_summary_id_is_compound(self):
        summaries = [r for r in self.records if r.record_type == RecordType.DAILY_SUMMARY]
        assert summaries[0].source_record_id == "42:2026-06-07:daily_summary"

    def test_food_entry_account_ref(self):
        food_entries = [r for r in self.records if r.record_type == RecordType.FOOD_ENTRY]
        for r in food_entries:
            assert r.account_ref == "42"


# ---------------------------------------------------------------------------
# Tests — meal group decoding
# ---------------------------------------------------------------------------


class TestMealDecoding:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)
        self.food_entries = [r for r in self.records if r.record_type == RecordType.FOOD_ENTRY]

    def test_breakfast_entry_meal(self):
        """order=65536 → meal_group=1 → Breakfast."""
        # serving srv-abc001 has order=65536 → (65536 >> 16) & 0xFF = 1 → Breakfast
        entry = next(r for r in self.food_entries if r.source_record_id == "srv-abc001")
        assert entry.common.meal == Meal.BREAKFAST

    def test_lunch_entry_meal(self):
        """order=131072 → meal_group=2 → Lunch."""
        entry = next(r for r in self.food_entries if r.source_record_id == "srv-abc002")
        assert entry.common.meal == Meal.LUNCH


# ---------------------------------------------------------------------------
# Tests — daily_summary fields
# ---------------------------------------------------------------------------


class TestDailySummary:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)
        self.summary = next(r for r in self.records if r.record_type == RecordType.DAILY_SUMMARY)

    def test_energy_kcal(self):
        assert self.summary.common.energy_kcal == pytest.approx(525.0)

    def test_protein_g(self):
        assert self.summary.common.protein_g == pytest.approx(18.75)

    def test_carbs_g(self):
        assert self.summary.common.carbs_g == pytest.approx(108.0)

    def test_fat_g(self):
        assert self.summary.common.fat_g == pytest.approx(4.2)

    def test_energy_target_kcal(self):
        assert self.summary.common.energy_target_kcal == pytest.approx(2000.0)

    def test_micronutrients_present(self):
        assert isinstance(self.summary.common.micronutrients, Micronutrients)


# ---------------------------------------------------------------------------
# Tests — food_definition fields
# ---------------------------------------------------------------------------


class TestFoodDefinition:
    @pytest.fixture(autouse=True)
    def setup(self, diary_response, food_response, nutrient_dict):
        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        self.records = collect_records(connector, "42", diary_response, food_response, nutrient_dict)
        self.fd = next(r for r in self.records if r.record_type == RecordType.FOOD_DEFINITION)

    def test_food_ref_id(self):
        assert self.fd.common.food_ref_id == "100001"

    def test_food_name(self):
        assert self.fd.common.food_name == "Synthetic Test Grain"

    def test_food_source(self):
        assert self.fd.common.food_source == "NCCDB"

    def test_food_tags(self):
        assert "Whole" in self.fd.common.food_tags

    def test_reference_serving(self):
        assert self.fd.common.reference_serving_amount == pytest.approx(100.0)
        assert self.fd.common.reference_serving_unit == "g"

    def test_energy_kcal(self):
        assert self.fd.common.energy_kcal == pytest.approx(350.0)

    def test_micronutrients_iron(self):
        assert self.fd.common.micronutrients is not None
        assert self.fd.common.micronutrients.iron_mg == pytest.approx(4.2)


# ---------------------------------------------------------------------------
# Tests — session expiry handling
# ---------------------------------------------------------------------------


class TestSessionExpiry:
    def test_relogin_on_session_expiry(self, diary_response, food_response, nutrient_dict):
        """If get_diary returns None (session expired), connector re-logins once and retries."""
        from openvitals.connectors.base import Account

        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        account = Account(account_ref="42", source="cronometer", metadata={"cronometer_user_id": 42, "timezone": "UTC"})

        call_count = [0]

        def fake_get_diary(day):
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # first call: simulate session expiry
            return diary_response  # second call: success after re-login

        with (
            patch(
                "openvitals.connectors.cronometer.connector.CronometerConnector._do_login",
                return_value=_fake_login_result(42, nutrient_dict),
            ),
            patch("openvitals.connectors.cronometer.connector._CronometerSession") as MockSession,
            patch("openvitals.connectors.cronometer.connector.time.sleep"),
        ):
            session_instance = MagicMock()
            session_instance.nutrient_dict = nutrient_dict
            session_instance.get_diary.side_effect = fake_get_diary
            session_instance.get_food.return_value = food_response
            MockSession.return_value = session_instance

            since_dt = datetime(2026, 6, 7, 0, 0, 0, tzinfo=UTC)
            records = list(connector.fetch(account, since=since_dt))

        # After re-login the fetch should complete normally
        assert any(r.record_type == RecordType.DAILY_SUMMARY for r in records)

    def test_auth_required_on_double_failure(self, nutrient_dict):
        """If re-login also fails, raises AuthRequired."""
        from openvitals.connectors.base import Account

        store = FakeStore({"cronometer:42": {"username": "u", "password": "p", "token": "t", "timezone": "UTC"}})
        connector = _make_connector(store)
        account = Account(account_ref="42", source="cronometer", metadata={"cronometer_user_id": 42, "timezone": "UTC"})

        with (
            patch(
                "openvitals.connectors.cronometer.connector.CronometerConnector._do_login",
                side_effect=[
                    _fake_login_result(42, nutrient_dict),  # initial login succeeds
                    AuthRequired("Re-login failed"),          # re-login fails
                ],
            ),
            patch("openvitals.connectors.cronometer.connector._CronometerSession") as MockSession,
            patch("openvitals.connectors.cronometer.connector.time.sleep"),
        ):
            session_instance = MagicMock()
            session_instance.nutrient_dict = nutrient_dict
            session_instance.get_diary.return_value = None  # always expired
            MockSession.return_value = session_instance

            since_dt = datetime(2026, 6, 7, 0, 0, 0, tzinfo=UTC)
            with pytest.raises(AuthRequired):
                list(connector.fetch(account, since=since_dt))


# ---------------------------------------------------------------------------
# Tests — private helpers (unit)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_map_source_extracts_prefix(self):
        assert _map_source("NCCDB:14953") == "NCCDB"
        assert _map_source("USDA:999") == "USDA"
        assert _map_source("Custom") == "Custom"
        assert _map_source(None) is None

    def test_get_reference_grams_first_measure(self, food_response):
        grams = _get_reference_grams(food_response)
        assert grams == pytest.approx(100.0)

    def test_get_reference_grams_fallback(self):
        """Food with no measures → fall back to 100g."""
        assert _get_reference_grams({"id": 1, "name": "X", "measures": []}) == pytest.approx(100.0)

    def test_get_reference_grams_none_input(self):
        assert _get_reference_grams(None) is None

    def test_scale_nutrient(self, food_response):
        """Iron (id=303, amount=4.2 per 100g) at 50g serving → 2.1."""
        nutrients = food_response["nutrients"]
        result = _scale_nutrient(nutrients, 303, 50.0, 100.0)
        assert result == pytest.approx(2.1)

    def test_build_full_nutrient_list(self, food_response, nutrient_dict):
        result = _build_full_nutrient_list(food_response["nutrients"], nutrient_dict)
        assert len(result) == len(food_response["nutrients"])
        iron = next(n for n in result if n["id"] == 303)
        assert iron["name"] == "Iron, Fe"
        assert iron["unit"] == "mg"
        assert iron["amount"] == pytest.approx(4.2)

    def test_build_full_nutrient_list_unknown_id(self, nutrient_dict):
        """Unknown nutrient ID → name/unit/category are None (no KeyError)."""
        result = _build_full_nutrient_list([{"id": 9999, "amount": 1.0}], nutrient_dict)
        assert result[0]["name"] is None
        assert result[0]["unit"] is None
