"""
CronometerConnector — OpenVitals connector for Cronometer nutrition data.

⚠️  UNOFFICIAL API — personal use only (see package __init__.py warning).

Emits three record types per sync:
- ``food_entry``       (one per diary serving, with resolved food name + micronutrients)
- ``daily_summary``    (one per day, macro + micro totals)
- ``food_definition``  (one per distinct food seen; reference-style)

Auth model:
- username/password → session token from login().
- Session can expire mid-fetch; the connector re-logins once automatically.
  If re-login also fails, raises ``AuthRequired`` so the caller can present a
  fresh login prompt.
- Credentials are stored via ``CredentialStore`` (never logged).

Identity (connector-contract.md §2):
- ``food_entry``      → ``source_record_id = str(serving_id)``   (provider-native)
- ``food_definition`` → ``source_record_id = str(food_id)``      (provider-native)
- ``daily_summary``   → ``source_record_id = "{account_ref}:{local_date}:daily_summary"``

Sync semantics:
- ``fetch(account, since)`` iterates day-by-day from ``since`` (or ``since_date``
  derived from it) to today.  Day granularity only — Cronometer's API is
  per-day.  There is no updated-time cursor; re-fetching a date is idempotent.
- The connector persists no watermark; the caller owns the cursor.

Food resolution:
- The diary returns opaque ``food_id`` integers.  The connector calls
  ``get_food`` for each unseen food and joins the ``login["nutrients"]``
  dictionary (nutrient_id → name/unit/category) to produce human-readable
  names.  Resolved foods are cached in-memory for the lifetime of a fetch call.
- Cronometer nutrient IDs follow USDA SR numbering for primary nutrients plus
  Cronometer-specific IDs for others.  The canonical mapping below covers all
  24 canonical Micronutrients keys; unmapped keys remain None in the emitted
  record (the full list is always preserved in ``raw``).

Cronometer nutrient ID → canonical key mapping
(verified against the nutrient dictionary returned by the live API on 2026-06-07;
 IDs are stable USDA-style integers; Cronometer-specific IDs noted inline):
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from openvitals.connectors.base import Account, BaseConnector
from openvitals.errors import AuthRequired, ProviderPayloadError, ProviderUnavailable, RateLimited
from openvitals.schema.domains import Domain, Meal, RecordType
from openvitals.schema.records import (
    DailySummaryCommon,
    FoodDefinitionCommon,
    FoodEntryCommon,
    Micronutrients,
    Record,
)
from openvitals.storage.credentials import CredentialStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cronometer API constants (verified against live API)
# ---------------------------------------------------------------------------

_BASE = "https://mobile.cronometer.com"
_HEADERS = {
    "content-type": "text/plain; charset=utf-8",
    "user-agent": "Dart/3.9 (dart:io)",
    "accept-encoding": "gzip",
}
_APP_AUTH = {"api": 3, "os": "Android", "build": "2807", "flavour": "free"}

# ---------------------------------------------------------------------------
# Cronometer nutrient ID → canonical Micronutrients key mapping
#
# Primary source: USDA SR numbering used by Cronometer (verified against live
# login["nutrients"] response, 2026-06-07).  IDs marked [CRON] are
# Cronometer-specific (not USDA SR).
#
# Unmapped canonical keys (remain None unless source adds them):
#   - iodine_mcg   (Cronometer does not consistently expose iodine)
#
# ---------------------------------------------------------------------------

NUTRIENT_ID_TO_CANONICAL: dict[int, tuple[str, float]] = {
    # (canonical_field_name, unit_scale_factor_to_canonical_unit)
    # scale = 1.0 unless the source unit differs from the canonical unit
    #
    # Vitamins
    320:  ("vitamin_a_mcg",     1.0),   # Vitamin A, RAE (mcg)
    401:  ("vitamin_c_mg",      1.0),   # Vitamin C (mg)
    328:  ("vitamin_d_mcg",     1.0),   # Vitamin D (D2+D3, mcg)
    323:  ("vitamin_e_mg",      1.0),   # Vitamin E (alpha-tocopherol, mg)
    430:  ("vitamin_k_mcg",     1.0),   # Vitamin K (phylloquinone, mcg)
    404:  ("thiamin_b1_mg",     1.0),   # Thiamin (mg)
    405:  ("riboflavin_b2_mg",  1.0),   # Riboflavin (mg)
    406:  ("niacin_b3_mg",      1.0),   # Niacin (mg)
    415:  ("vitamin_b6_mg",     1.0),   # Vitamin B6 (mg)
    435:  ("folate_b9_mcg",     1.0),   # Folate, DFE (mcg)
    418:  ("vitamin_b12_mcg",   1.0),   # Vitamin B12 (mcg)
    # Minerals
    301:  ("calcium_mg",        1.0),   # Calcium (mg)
    303:  ("iron_mg",           1.0),   # Iron (mg)
    304:  ("magnesium_mg",      1.0),   # Magnesium (mg)
    309:  ("zinc_mg",           1.0),   # Zinc (mg)
    306:  ("potassium_mg",      1.0),   # Potassium (mg)
    307:  ("sodium_mg",         1.0),   # Sodium (mg)
    317:  ("selenium_mcg",      1.0),   # Selenium (mcg)
    305:  ("phosphorus_mg",     1.0),   # Phosphorus (mg)
    # iodine_mcg: Cronometer does not consistently expose iodine as a discrete
    # nutrient ID in the standard nutrient dictionary. Left unmapped (None).
    #
    # Fats of interest
    851:  ("omega3_g",          1.0),   # Omega-3 total (g) [CRON composite]
    675:  ("omega6_g",          1.0),   # Omega-6 total (g) [CRON composite]
    606:  ("saturated_fat_g",   1.0),   # Fatty acids, saturated (g)
    601:  ("cholesterol_mg",    1.0),   # Cholesterol (mg)
}

# ---------------------------------------------------------------------------
# Meal group mapping (diary entry order >> 16 & 0xFF → Cronometer slot)
# Verified against live API.
# ---------------------------------------------------------------------------

_MEAL_MAP: dict[int, Meal] = {
    1: Meal.BREAKFAST,
    2: Meal.LUNCH,
    3: Meal.DINNER,
    4: Meal.SNACK,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode(payload: dict) -> bytes:
    """Encode payload as JSON bytes (Cronometer expects text/plain; charset=utf-8)."""
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _fmt_day(d: date) -> str:
    """Return Cronometer's non-zero-padded date format: '2026-6-1' not '2026-06-01'."""
    return f"{d.year}-{d.month}-{d.day}"


def _day_to_utc_midnight(d: date, tz: str | None) -> datetime:
    """Return UTC midnight for a local date. Uses UTC as fallback if no tz given."""
    # We don't have a real tz conversion without pytz/zoneinfo for arbitrary zones,
    # but we store local_date separately so occurred_at UTC midnight is a safe proxy.
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)


def _build_micronutrients(
    nutrients: list[dict],
    nutrient_dict: dict[int, dict],
) -> Micronutrients:
    """Map a list of {id, amount} nutrient records to the canonical Micronutrients model.

    nutrients: the ``nutrients`` list from a get_food or diary summary payload
               (each item has at least ``id`` and ``amount``).
    nutrient_dict: the login["nutrients"] dictionary {id → {name, unit, ...}}.

    Unknown nutrient IDs are silently ignored (they remain in raw).
    Canonical keys with no source data remain None.
    """
    kwargs: dict[str, float] = {}
    for n in nutrients:
        nid = n.get("id")
        amount = n.get("amount")
        if nid is None or amount is None:
            continue
        mapping = NUTRIENT_ID_TO_CANONICAL.get(nid)
        if mapping is None:
            continue
        canonical_key, scale = mapping
        # Only set if not already set (take first occurrence)
        if canonical_key not in kwargs:
            kwargs[canonical_key] = amount * scale
    return Micronutrients(**kwargs)


def _sum_micronutrients(micros_list: list[Micronutrients]) -> Micronutrients:
    """Sum a list of Micronutrients objects field-by-field.

    None stays None only if every entry for that field is None.
    If at least one entry has a non-None value, the sum is taken across
    all non-None values (missing entries contribute 0 to the sum).

    Used to accumulate per-entry micronutrients into a daily_summary total.
    """
    if not micros_list:
        return Micronutrients()

    # Collect all canonical field names from the schema
    fields = list(Micronutrients.model_fields.keys())
    totals: dict[str, float | None] = {}

    for field in fields:
        values = [getattr(m, field) for m in micros_list]
        non_none = [v for v in values if v is not None]
        if non_none:
            totals[field] = sum(non_none)
        else:
            totals[field] = None

    return Micronutrients(**{k: v for k, v in totals.items() if v is not None})


def _map_source(source_str: str | None) -> str | None:
    """Normalize Cronometer source strings to short labels.

    Cronometer encodes source as e.g. 'NCCDB:14953' or 'USDA:xxx' or 'Custom'.
    We extract the prefix as the canonical source name.
    """
    if not source_str:
        return None
    return source_str.split(":")[0] if ":" in source_str else source_str


# ---------------------------------------------------------------------------
# API client (thin, no retry logic beyond session expiry handling)
# ---------------------------------------------------------------------------


class _CronometerSession:
    """Thin stateful HTTP session for the Cronometer mobile API.

    Holds the user_id + session token obtained from login(). Never logs
    username, password, or token — only the user_id (opaque integer).
    """

    def __init__(
        self,
        cronometer_user_id: int,
        token: str,
        nutrient_dict: dict[int, dict],
        http_client: httpx.Client,
    ) -> None:
        self._uid = cronometer_user_id
        self._token = token
        self.nutrient_dict = nutrient_dict  # {id → {name, unit, category}}
        self._client = http_client

    @property
    def cronometer_user_id(self) -> int:
        return self._uid

    def _auth_block(self) -> dict:
        return {"userId": self._uid, "token": self._token, **_APP_AUTH}

    def _post(self, endpoint: str, payload: dict) -> dict:
        resp = self._client.post(
            f"{_BASE}{endpoint}",
            content=_encode(payload),
            headers=_HEADERS,
            timeout=30,
        )
        if resp.status_code == 429:
            raise RateLimited("Cronometer rate limit hit", retry_after=None)
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"Cronometer returned {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def get_diary(self, day: date) -> dict | None:
        """Fetch diary for one day.  Returns None if the session has expired."""
        payload = {
            "day": _fmt_day(day),
            "config": {"call_version": 1},
            "auth": self._auth_block(),
            "lastSeen": 0,
        }
        data = self._post("/api/v2/get_diary", payload)
        if data.get("result") == "FAIL":
            return None  # session expired or auth failure
        return data

    def get_food(self, food_id: int) -> dict | None:
        """Fetch a single food's reference data.  Returns None on failure."""
        payload = {
            "id": food_id,
            "config": {"call_version": 1},
            "auth": self._auth_block(),
        }
        try:
            data = self._post("/api/v2/get_food", payload)
            if data.get("result") == "FAIL":
                return None
            return data
        except (httpx.HTTPStatusError, ProviderUnavailable):
            return None


# ---------------------------------------------------------------------------
# Main connector
# ---------------------------------------------------------------------------


class CronometerConnector(BaseConnector):
    """
    OpenVitals connector for Cronometer nutrition data.

    Opt-in only — see package-level warning about unofficial API risk.

    Usage::

        connector = CronometerConnector(credential_store=store, enabled=True)
        account = connector.connect({"username": "...", "password": "..."})
        for record in connector.fetch(account, since=since_dt):
            ...
    """

    source = "cronometer"
    domains = [Domain.NUTRITION.value]

    def __init__(
        self,
        credential_store: CredentialStore | None = None,
        enabled: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        credential_store:
            Where to persist session credentials.  If None, credentials are
            not persisted between calls (safe for one-shot use).
        enabled:
            Explicit opt-in gate — must be True to use the connector.
            Raises RuntimeError if False (protects against accidental use).
        """
        if not enabled:
            raise RuntimeError(
                "CronometerConnector requires explicit opt-in: pass enabled=True. "
                "This connector uses an unofficial Cronometer API. Your password will be "
                "stored in the credential store (encrypted at rest, but decryptable in "
                "memory). Do not use on shared systems."
            )
        self._store = credential_store

    # --- Auth ---------------------------------------------------------------

    @staticmethod
    def _do_login(username: str, password: str, timezone_str: str = "UTC") -> tuple[int, str, dict[int, dict]]:
        """Call the Cronometer login endpoint.  Never logs username/password/token.

        Returns (cronometer_user_id, session_token, nutrient_dict).
        Raises AuthRequired on bad credentials, ProviderUnavailable on 5xx.
        """
        payload = {
            "email": username,
            "password": password,
            "timezone": timezone_str,
            "userCode": None,
            "build": "4.48.2 b2807-a",
            "device": "Android 14 (SDK 34), Google Pixel 6 Pro",
            "firebaseToken": "",
            "features": {
                "food_search_config": '{"newSearch": true, "newSpellcheck": true}',
                "use_gpt_autofill": "true",
            },
            "auth": {"userId": None, "token": None, **_APP_AUTH},
            "lastSeen": 0,
            "config": {"call_version": 2},
        }
        try:
            resp = httpx.post(
                f"{_BASE}/api/v2/login",
                content=_encode(payload),
                headers=_HEADERS,
                timeout=30,
            )
        except httpx.RequestError as exc:
            raise ProviderUnavailable(f"Cronometer login network error: {exc}") from exc

        # Cronometer rate-limits logins — check 429 before raise_for_status().
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            raise RateLimited(
                "Cronometer login rate-limited (HTTP 429)",
                retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"Cronometer login returned {resp.status_code}")
        resp.raise_for_status()

        data = resp.json()
        # The limiter sometimes arrives as an HTTP 200 with "Too Many Attempts" in the body.
        if data.get("error") == "Too Many Attempts" or data.get("result") == "Too Many Attempts":
            raise RateLimited("Cronometer login rate-limited (Too Many Attempts)")
        if data.get("result") == "FAIL":
            raise AuthRequired(
                "Cronometer login failed — check username/password. "
                f"(result={data.get('result')!r})"
            )
        # Guard against a malformed success response missing required fields.
        if "id" not in data or "sessionKey" not in data:
            raise AuthRequired(
                f"Cronometer login response missing required fields: {list(data.keys())}"
            )

        nutrient_dict = {n["id"]: n for n in data.get("nutrients", [])}
        return data["id"], data["sessionKey"], nutrient_dict

    def connect(self, credentials: dict[str, Any]) -> Account:
        """Authenticate with Cronometer and return an Account.

        ``credentials`` must contain ``username`` and ``password``.
        Optionally ``timezone`` (IANA string, default "UTC").

        The session token is persisted to ``credential_store`` keyed by the
        Cronometer user ID (stable integer, used as account_ref).

        Raises ``AuthRequired`` on bad credentials.
        Never logs username, password, or token.
        """
        username = credentials.get("username")
        password = credentials.get("password")
        tz = credentials.get("timezone", "UTC")

        if not username or not password:
            raise AuthRequired("Cronometer connector requires 'username' and 'password' in credentials.")

        cronometer_user_id, token, nutrient_dict = self._do_login(username, password, tz)

        account_ref = str(cronometer_user_id)

        # Persist session for later — store token + nutrient_dict so fetch() can
        # reuse the session without a full re-login on every call
        if self._store is not None:
            self._store.set(
                self.source,
                account_ref,
                {
                    "username": username,
                    "password": password,
                    "token": token,
                    "timezone": tz,
                    # JSON requires string keys; fetch() re-keys to int on load
                    "nutrient_dict": {str(k): v for k, v in nutrient_dict.items()},
                },
            )

        return Account(
            account_ref=account_ref,
            source=self.source,
            metadata={
                "cronometer_user_id": cronometer_user_id,
                "timezone": tz,
            },
        )

    # --- Fetch --------------------------------------------------------------

    def fetch(
        self,
        account: Account,
        since: datetime | None = None,
        _today: date | None = None,
    ) -> Iterable[Record]:
        """Yield ``food_entry``, ``daily_summary``, and ``food_definition`` Records.

        ``since`` — UTC datetime; the connector fetches from the corresponding
        local date (derived from account timezone where known) through today.
        If None, fetches the last 90 days.

        Session expiry handling: if get_diary returns a FAIL (session expired),
        the connector re-logins once using stored credentials. If re-login fails,
        raises ``AuthRequired``.

        Raises ``AuthRequired``, ``ProviderUnavailable``.
        """
        cronometer_user_id = int(account.account_ref)
        tz = account.metadata.get("timezone", "UTC")
        account_ref = account.account_ref

        # Retrieve stored credentials for potential re-login
        stored_creds = None
        if self._store is not None:
            stored_creds = self._store.get(self.source, account_ref)

        if stored_creds is None:
            raise AuthRequired(
                "No stored credentials for this Cronometer account. Call connect() first."
            )

        # Reuse the stored session token to avoid a full re-login on every fetch.
        # If the token has expired, the first get_diary() call returns None and the
        # loop re-logins automatically (same expiry path as before).
        stored_token = stored_creds.get("token")
        stored_nutrient_dict_raw = stored_creds.get("nutrient_dict", {})
        nutrient_dict: dict[int, dict] = {int(k): v for k, v in stored_nutrient_dict_raw.items()}

        if not stored_token:
            # No stored token — must login (e.g. store was cleared externally)
            uid, stored_token, nutrient_dict = self._do_login(
                stored_creds["username"], stored_creds["password"], tz
            )
            if self._store is not None:
                updated = dict(stored_creds)
                updated["token"] = stored_token
                updated["nutrient_dict"] = {str(k): v for k, v in nutrient_dict.items()}
                self._store.set(self.source, account_ref, updated)

        uid = cronometer_user_id
        session = _CronometerSession(uid, stored_token, nutrient_dict, httpx.Client())

        # Determine date range
        today = _today or date.today()
        if since is not None:
            start_date = since.date() if isinstance(since, datetime) else since
        else:
            start_date = today - timedelta(days=90)

        # Per-fetch in-memory food resolution cache: food_id → get_food payload
        food_cache: dict[int, dict | None] = {}

        # Track emitted food_definitions so we only emit each food once
        emitted_food_defs: set[int] = set()

        ingested_at = datetime.now(tz=timezone.utc)
        current = start_date

        while current <= today:
            data = session.get_diary(current)

            if data is None:
                # Session expired — re-login once
                log.info("Cronometer session expired; re-logging in.")
                try:
                    uid2, token2, nd2 = self._do_login(
                        stored_creds["username"], stored_creds["password"], tz
                    )
                    session = _CronometerSession(uid2, token2, nd2, httpx.Client())
                    # Update stored token + nutrient_dict
                    if self._store is not None:
                        updated = dict(stored_creds)
                        updated["token"] = token2
                        updated["nutrient_dict"] = {str(k): v for k, v in nd2.items()}
                        self._store.set(self.source, account_ref, updated)
                    data = session.get_diary(current)
                except AuthRequired:
                    raise AuthRequired(
                        "Cronometer session expired and re-login failed. "
                        "Call connect() with fresh credentials."
                    )

                if data is None:
                    raise AuthRequired(
                        "Cronometer session expired and re-login did not restore access."
                    )

            diary_entries = data.get("diary") or []
            summary_block = data.get("summary") or {}
            consumed = summary_block.get("consumed") or {}
            macros = summary_block.get("macros") or {}

            serving_entries = [e for e in diary_entries if e.get("type") == "Serving"]

            # Resolve food IDs for this day
            food_ids_today = {e["foodId"] for e in serving_entries if e.get("foodId") is not None}
            for food_id in food_ids_today:
                if food_id not in food_cache:
                    food_data = session.get_food(food_id)
                    food_cache[food_id] = food_data
                    time.sleep(0.2)  # conservative throttle

            # Accumulate per-entry micronutrients so daily_summary can sum them
            day_entry_micros: list[Micronutrients] = []
            # Track unresolved entries so daily_summary can flag incomplete micronutrient sum
            unresolved_food_count = 0

            # Emit food_entry records
            for entry in serving_entries:
                food_id = entry.get("foodId")
                serving_id = entry.get("servingId")

                if serving_id is None:
                    log.warning("Diary entry missing servingId — skipping: %r", entry.get("foodId"))
                    continue

                if food_id is None:
                    log.warning("Diary entry missing foodId (servingId=%r) — skipping", serving_id)
                    continue

                food_data = food_cache.get(food_id)

                # Skip entries where food resolution failed entirely — a record with
                # food_name=None is near-useless and silently deflates the micronutrient
                # sum for the day. Flag the count so daily_summary can expose it.
                if food_data is None:
                    log.warning(
                        "Food resolution failed for food_id=%r (servingId=%r) — "
                        "skipping food_entry; daily_summary micronutrient sum will be incomplete.",
                        food_id, serving_id,
                    )
                    unresolved_food_count += 1
                    continue

                # Meal group from packed order field
                order = entry.get("order", 0)
                meal_group_int = (order >> 16) & 0xFF
                meal = _MEAL_MAP.get(meal_group_int, Meal.UNCATEGORIZED)

                # food_data is guaranteed non-None here (None case continued above)
                food_nutrients_raw: list[dict] = food_data.get("nutrients") or []
                ref_grams = _get_reference_grams(food_data)
                entry_grams = entry.get("grams")

                entry_micros: Micronutrients | None = None
                if food_nutrients_raw and ref_grams and entry_grams:
                    scale = entry_grams / ref_grams
                    scaled_nutrients = [
                        {"id": n.get("id"), "amount": (n.get("amount") or 0) * scale}
                        for n in food_nutrients_raw
                    ]
                    entry_micros = _build_micronutrients(scaled_nutrients, session.nutrient_dict)
                elif food_nutrients_raw:
                    entry_micros = _build_micronutrients(food_nutrients_raw, session.nutrient_dict)

                food_name = food_data.get("name")
                food_source_raw = food_data.get("source")
                food_tags = food_data.get("foodTags") or []

                energy_kcal = _scale_nutrient(food_nutrients_raw, 1, entry_grams, ref_grams)
                protein_g = _scale_nutrient(food_nutrients_raw, 203, entry_grams, ref_grams)
                fat_g = _scale_nutrient(food_nutrients_raw, 204, entry_grams, ref_grams)
                carbs_g = _scale_nutrient(food_nutrients_raw, 205, entry_grams, ref_grams)
                fiber_g = _scale_nutrient(food_nutrients_raw, 291, entry_grams, ref_grams)
                sugar_g = _scale_nutrient(food_nutrients_raw, 269, entry_grams, ref_grams)
                alcohol_g = _scale_nutrient(food_nutrients_raw, 221, entry_grams, ref_grams)

                common = FoodEntryCommon(
                    food_name=food_name,
                    food_ref_id=str(food_id),
                    food_tags=food_tags,
                    food_category=str(food_data.get("category")) if food_data.get("category") is not None else None,
                    food_source=_map_source(food_source_raw),
                    brand=None,  # Cronometer doesn't expose brand separately
                    serving_amount=entry.get("grams"),
                    serving_unit="g",
                    meal=meal,
                    energy_kcal=energy_kcal,
                    protein_g=protein_g,
                    carbs_g=carbs_g,
                    fat_g=fat_g,
                    fiber_g=fiber_g,
                    sugar_g=sugar_g,
                    alcohol_g=alcohol_g,
                    micronutrients=entry_micros,
                )

                # Accumulate micronutrients for daily_summary (fix #2)
                if entry_micros is not None:
                    day_entry_micros.append(entry_micros)

                yield Record(
                    source=self.source,
                    domain=Domain.NUTRITION.value,
                    record_type=RecordType.FOOD_ENTRY,
                    source_record_id=str(serving_id),
                    account_ref=account_ref,
                    occurred_at=_day_to_utc_midnight(current, tz),
                    ingested_at=ingested_at,
                    timezone=tz,
                    local_date=current,
                    common=common,
                    raw=entry,
                )

                # Emit food_definition (once per food per fetch)
                if food_id not in emitted_food_defs:
                    emitted_food_defs.add(food_id)

                    # Full nutrient list with human names for raw
                    full_nutrients = _build_full_nutrient_list(
                        food_nutrients_raw, session.nutrient_dict
                    )
                    ref_grams = _get_reference_grams(food_data)

                    def_micros = _build_micronutrients(
                        food_nutrients_raw, session.nutrient_dict
                    )

                    def_common = FoodDefinitionCommon(
                        food_ref_id=str(food_id),
                        food_name=food_data.get("name"),
                        food_category=str(food_data.get("category")) if food_data.get("category") is not None else None,
                        food_source=_map_source(food_data.get("source")),
                        food_tags=food_data.get("foodTags") or [],
                        barcode=food_data.get("barcode"),
                        energy_kcal=_get_nutrient_amount(food_nutrients_raw, 1),
                        protein_g=_get_nutrient_amount(food_nutrients_raw, 203),
                        carbs_g=_get_nutrient_amount(food_nutrients_raw, 205),
                        fat_g=_get_nutrient_amount(food_nutrients_raw, 204),
                        reference_serving_amount=ref_grams,
                        reference_serving_unit="g",
                        micronutrients=def_micros,
                    )

                    yield Record(
                        source=self.source,
                        domain=Domain.NUTRITION.value,
                        record_type=RecordType.FOOD_DEFINITION,
                        source_record_id=str(food_id),
                        account_ref=account_ref,
                        occurred_at=_day_to_utc_midnight(current, tz),
                        ingested_at=ingested_at,
                        timezone=tz,
                        local_date=current,
                        common=def_common,
                        raw=food_data,  # verbatim — never mutated (spec: raw is byte-faithful)
                        source_extras={"normalized_nutrients": full_nutrients},
                    )

            # Emit daily_summary
            summary_id = f"{account_ref}:{current.isoformat()}:daily_summary"

            # Sum micronutrients across all food entries for this day
            summary_micros = _sum_micronutrients(day_entry_micros) if day_entry_micros else None

            summary_common = DailySummaryCommon(
                energy_kcal=consumed.get("total"),
                protein_g=consumed.get("protein_g"),
                carbs_g=consumed.get("carbs_g"),
                fat_g=consumed.get("fat_g"),
                fiber_g=consumed.get("fiber_g"),
                sugar_g=consumed.get("sugar_g"),
                alcohol_g=consumed.get("alcohol_g"),
                energy_target_kcal=macros.get("energy"),
                micronutrients=summary_micros,
            )

            summary_extras: dict | None = None
            if unresolved_food_count > 0:
                summary_extras = {
                    "micronutrient_sum_incomplete": True,
                    "unresolved_food_count": unresolved_food_count,
                }

            yield Record(
                source=self.source,
                domain=Domain.NUTRITION.value,
                record_type=RecordType.DAILY_SUMMARY,
                source_record_id=summary_id,
                account_ref=account_ref,
                occurred_at=_day_to_utc_midnight(current, tz),
                ingested_at=ingested_at,
                timezone=tz,
                local_date=current,
                common=summary_common,
                raw=data,
                source_extras=summary_extras,
            )

            current += timedelta(days=1)
            time.sleep(1)  # conservative rate limit — Cronometer limits unknown


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_nutrient_amount(nutrients: list[dict], nutrient_id: int) -> float | None:
    """Return the amount for a nutrient ID from a nutrients list, or None."""
    for n in nutrients:
        if n.get("id") == nutrient_id:
            return n.get("amount")
    return None


def _get_reference_grams(food_data: dict | None) -> float | None:
    """Extract the reference serving size in grams from a get_food response.

    Cronometer's get_food response includes a ``measures`` list. The first
    measure with unit 'g' (or the gram-equivalent) is the reference serving.
    If no gram measure exists, fall back to 100g (conventional NCCDB reference).
    """
    if food_data is None:
        return None
    measures = food_data.get("measures") or []
    for m in measures:
        # Cronometer measure has {id, name, grams, ...}
        grams = m.get("grams")
        if grams and grams > 0:
            return float(grams)
    return 100.0  # fallback: conventional 100g reference


def _scale_nutrient(
    nutrients: list[dict],
    nutrient_id: int,
    entry_grams: float | None,
    ref_grams: float | None,
) -> float | None:
    """Return a macro value scaled from reference serving to actual serving."""
    amount = _get_nutrient_amount(nutrients, nutrient_id)
    if amount is None:
        return None
    if entry_grams and ref_grams and ref_grams > 0:
        return amount * (entry_grams / ref_grams)
    return amount


def _build_full_nutrient_list(
    nutrients: list[dict],
    nutrient_dict: dict[int, dict],
) -> list[dict]:
    """Build a human-readable full nutrient list by joining with nutrient_dict.

    Returns [{id, name, unit, amount, category}] for every nutrient in the
    food's nutrient list. Stored in ``source_extras["normalized_nutrients"]``
    on food_definition records — useful provider-headline data but not in raw
    (raw is verbatim per spec) nor in common (not a canonical micronutrient field).
    """
    result = []
    for n in nutrients:
        nid = n.get("id")
        meta = nutrient_dict.get(nid, {}) if nid is not None else {}
        result.append({
            "id": nid,
            "name": meta.get("name"),
            "unit": meta.get("unit"),
            "amount": n.get("amount"),
            "category": meta.get("category"),
        })
    return result
