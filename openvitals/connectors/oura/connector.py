"""
Oura connector for OpenVitals.

API: Oura Cloud API v2 — https://api.ouraring.com/v2

## Endpoints used
- GET /v2/usercollection/personal_info → account_ref (stable Oura user_id)
- GET /v2/usercollection/sleep         → sleep_session records
- GET /v2/usercollection/daily_readiness → daily_recovery records
- GET /v2/usercollection/workout       → workout records

## Identity rules (connector-contract.md §2)
- sleep_session:    source_record_id = raw["id"]          (Oura UUID — native)
- workout:          source_record_id = raw["id"]          (Oura UUID — native)
- daily_recovery:   source_record_id = f"{account_ref}:{day}:daily_recovery"
                    (daily aggregate — no stable provider ID; day from raw["day"])

## Scopes required
personal daily sleep workout

## Token rotation
Oura rotates the refresh token on every use.  ``connect()`` refreshes on expiry
(< 5 minutes remaining) and immediately persists the new refresh token.  Losing
this step orphans the token permanently — do not skip the persist step.

## Auth credentials dict shape (CredentialStore key = ("oura", account_ref))
{
    "access_token": str,
    "refresh_token": str,
    "expires_at": float,   # UTC epoch seconds
    "scopes": list[str],
}

The initial token bundle is obtained externally (OAuth code exchange) and stored
via CredentialStore.set() before connect() is called.

## Auth URLs
Authorization URL: https://cloud.ouraring.com/oauth/authorize
Token URL:         https://api.ouraring.com/oauth/token
Note: these are two different domains — cloud.ouraring.com for the auth redirect,
api.ouraring.com for token exchange.

## Pagination
All list endpoints accept start_date, end_date, next_token.
Responses return { data: [...], next_token: str | null }.
The connector drains all pages via next_token.

## Error semantics
- 401 → AuthRequired
- 403 → ProviderUnavailable (subscription/permission restriction)
- 429 → RateLimited
- 5xx / transport → ProviderUnavailable
- malformed payload → ProviderPayloadError
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from openvitals.connectors.base import Account, BaseConnector
from openvitals.errors import (
    AuthRequired,
    MissingScope,
    PartialFetchError,
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Authorization redirect uses cloud.ouraring.com; token exchange uses api.ouraring.com
OURA_AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_API_BASE = "https://api.ouraring.com"

REQUIRED_SCOPES = {"personal", "daily", "sleep", "workout"}

# Refresh the access token if it expires within 5 minutes
_EXPIRY_BUFFER_SECONDS = 300


# ---------------------------------------------------------------------------
# Activity type mapping
# ---------------------------------------------------------------------------

def _map_activity_type(activity: str | None) -> ActivityType:
    """Map an Oura activity string → ActivityType.

    Substring matching on the lowercased activity string.
    The original Oura label is always preserved in activity_label — never dropped.
    """
    if not activity:
        return ActivityType.OTHER
    low = activity.lower()
    if "run" in low:
        return ActivityType.RUN
    if "walk" in low:
        return ActivityType.WALK
    if "cycl" in low or "bike" in low:
        return ActivityType.RIDE
    if "swim" in low:
        return ActivityType.SWIM
    if "hik" in low:
        return ActivityType.HIKE
    if "strength" in low or "weights" in low or "lifting" in low:
        return ActivityType.STRENGTH
    return ActivityType.OTHER


# ---------------------------------------------------------------------------
# UTC helpers
# ---------------------------------------------------------------------------

def _parse_utc(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (may end in Z or +offset) → UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ProviderPayloadError(f"Cannot parse Oura timestamp {ts!r}: {exc}") from exc


# ---------------------------------------------------------------------------
# HTTP pagination helper
# ---------------------------------------------------------------------------

def _get_paginated(
    endpoint: str,
    access_token: str,
    params: dict[str, Any],
) -> list[dict]:
    """Drain a paginated Oura list endpoint. Returns all data items.

    Oura list responses use { data: [...], next_token: str | null }.
    Raises AuthRequired, RateLimited, ProviderUnavailable, or ProviderPayloadError.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    results: list[dict] = []
    page_params = dict(params)

    while True:
        try:
            resp = httpx.get(
                f"{OURA_API_BASE}{endpoint}",
                headers=headers,
                params=page_params,
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Oura API unreachable: {exc}") from exc

        if resp.status_code == 401:
            raise AuthRequired("Oura access token rejected (401). Call connect() to refresh.")
        if resp.status_code == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            retry_after: float | None = None
            if retry_after_raw:
                try:
                    retry_after = float(retry_after_raw)
                except (ValueError, TypeError):
                    pass
            raise RateLimited("Oura rate limit exceeded (429).", retry_after=retry_after)
        if resp.status_code == 403:
            raise ProviderUnavailable(
                f"Oura returned 403 for {endpoint}. "
                "Oura may be withholding access due to subscription or permission state."
            )
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"Oura API returned {resp.status_code}.")
        if resp.status_code >= 400:
            raise ProviderPayloadError(
                f"Oura API returned {resp.status_code} for {endpoint}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(f"Oura returned non-JSON response: {exc}") from exc

        items = data.get("data")
        if items is None:
            raise ProviderPayloadError(
                f"Oura response missing 'data' key for {endpoint}. "
                f"Keys present: {list(data.keys())}"
            )

        results.extend(items)

        next_token = data.get("next_token")
        if not next_token:
            break
        page_params["next_token"] = next_token

    return results


# ---------------------------------------------------------------------------
# OuraConnector
# ---------------------------------------------------------------------------


class OuraConnector(BaseConnector):
    """
    OpenVitals connector for the Oura Cloud API v2.

    Domains: sleep (sleep_session), recovery (daily_recovery), activity (workout).

    Usage::

        from openvitals.connectors.oura import OuraConnector
        from openvitals.storage.credentials import EncryptedFileStore

        store = EncryptedFileStore()
        connector = OuraConnector(credential_store=store)

        # credentials must already be stored (from an OAuth flow)
        account = connector.connect(credentials=store.get("oura", "<account_ref>"))
        records = list(connector.fetch(account, since=datetime(2026, 6, 1, tzinfo=timezone.utc)))
    """

    source = "oura"
    domains = ["sleep", "recovery", "activity"]

    _required_scopes = REQUIRED_SCOPES

    def __init__(
        self,
        credential_store: CredentialStore,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        """
        Parameters
        ----------
        credential_store:
            A CredentialStore implementation.  The connector uses it to persist
            rotated refresh tokens.  Never logs token values.
        client_id:
            Oura OAuth app client ID.  Falls back to OURA_CLIENT_ID env var.
        client_secret:
            Oura OAuth app client secret.  Falls back to OURA_CLIENT_SECRET env var.
        """
        self._store = credential_store
        self._client_id = client_id or os.environ.get("OURA_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("OURA_CLIENT_SECRET", "")

    # ------------------------------------------------------------------
    # connect()
    # ------------------------------------------------------------------

    def connect(self, credentials: dict[str, Any]) -> Account:
        """Validate credentials and return an Account.

        ``credentials`` must be a dict with keys:
            access_token, refresh_token, expires_at (UTC epoch float), scopes (list[str])

        If the access token is expired or within 5 minutes of expiry, this method
        refreshes it using the stored refresh token and persists the new token bundle.

        Raises AuthRequired if no valid credential can be established.
        Raises MissingScope if any required scope is absent.
        """
        if not credentials:
            raise AuthRequired(
                "No credentials provided. Run the Oura OAuth flow to obtain tokens "
                "and store them via CredentialStore.set()."
            )

        # --- Scope validation (loud failure, not silent empty result) ----------
        granted: set[str] = set(credentials.get("scopes", []))
        missing_scopes = self._required_scopes - granted
        if missing_scopes:
            raise MissingScope(
                f"Oura credentials are missing required scopes: {missing_scopes}. "
                f"Re-authenticate with all required scopes: {self._required_scopes}",
                required_scope=next(iter(missing_scopes)),
                granted_scopes=granted,
            )

        # --- Token refresh if near expiry -------------------------------------
        expires_at: float = credentials.get("expires_at", 0.0)
        access_token: str = credentials.get("access_token", "")

        if not access_token or time.time() > (expires_at - _EXPIRY_BUFFER_SECONDS):
            credentials = self._refresh_access_token(credentials)
            access_token = credentials["access_token"]
            # Persist early if we already have an account_ref cached
            if "_account_ref" in credentials:
                self._store.set(self.source, credentials["_account_ref"], credentials)

        # --- Fetch stable Oura user id as account_ref -----------------------
        try:
            resp = httpx.get(
                f"{OURA_API_BASE}/v2/usercollection/personal_info",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Oura personal_info endpoint unreachable: {exc}") from exc

        if resp.status_code == 401:
            raise AuthRequired("Oura access token rejected on personal_info fetch (401).")
        if resp.status_code >= 400:
            raise ProviderPayloadError(
                f"Oura personal_info fetch returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            info = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(f"Oura personal_info response is not JSON: {exc}") from exc

        oura_user_id = info.get("id")
        if oura_user_id is None:
            raise ProviderPayloadError(
                "Oura personal_info response missing 'id'. "
                f"Keys present: {list(info.keys())}"
            )

        account_ref = str(oura_user_id)

        # Embed account_ref so future re-connects can persist before the profile fetch
        credentials["_account_ref"] = account_ref
        # Persist (potentially refreshed) credentials keyed by stable account_ref
        self._store.set(self.source, account_ref, credentials)

        return Account(
            account_ref=account_ref,
            source=self.source,
            metadata={
                "scopes": list(granted),
                "token_expires_at": credentials.get("expires_at"),
                "email": info.get("email"),
            },
        )

    def _refresh_access_token(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Use the refresh token to obtain a new access token.

        Oura rotates the refresh token on every use — the new refresh token is stored
        back to the credential dict immediately.  Never log token values.

        Raises AuthRequired if the refresh fails (token has been revoked).
        """
        refresh_token = credentials.get("refresh_token", "")

        if not refresh_token:
            raise AuthRequired(
                "No refresh token in stored credentials. "
                "Re-run the Oura OAuth flow to obtain fresh tokens."
            )

        client_id = self._client_id or credentials.get("client_id", "")
        client_secret = self._client_secret or credentials.get("client_secret", "")

        if not client_id or not client_secret:
            raise AuthRequired(
                "OURA_CLIENT_ID and OURA_CLIENT_SECRET must be set (or passed at construction) "
                "to enable token refresh."
            )

        try:
            resp = httpx.post(
                OURA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"Oura token endpoint unreachable: {exc}") from exc

        if resp.status_code in (400, 401):
            raise AuthRequired(
                f"Oura refresh token rejected ({resp.status_code}). "
                "The token may have been revoked. Re-run the OAuth flow."
            )
        if resp.status_code >= 400:
            raise ProviderPayloadError(
                f"Oura token refresh returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            tokens = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(f"Oura token response is not JSON: {exc}") from exc

        new_access_token = tokens.get("access_token")
        new_refresh_token = tokens.get("refresh_token")

        if not new_access_token or not new_refresh_token:
            raise ProviderPayloadError(
                "Oura token response missing access_token or refresh_token. "
                f"Keys present: {list(tokens.keys())}"
            )

        # Build updated credential dict — carry forward client creds and scopes
        updated = dict(credentials)
        updated["access_token"] = new_access_token
        updated["refresh_token"] = new_refresh_token  # rotated — persist immediately
        updated["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        if "scope" in tokens:
            updated["scopes"] = tokens["scope"].split()

        return updated

    # ------------------------------------------------------------------
    # fetch()
    # ------------------------------------------------------------------

    def fetch(
        self,
        account: Account,
        since: datetime | None = None,
    ) -> Iterable[Record]:
        """Yield Records for all three Oura domains.

        Drains /v2/usercollection/sleep, /v2/usercollection/daily_readiness,
        and /v2/usercollection/workout.  All pages are fully consumed before
        returning.

        ``since`` is treated as the start of the fetch window (inclusive).
        If None, fetches all available history.
        """
        credentials = self._store.get(self.source, account.account_ref)
        if not credentials:
            raise AuthRequired(
                f"No stored credentials for oura/{account.account_ref}. "
                "Call connect() first."
            )

        access_token = credentials.get("access_token", "")
        expires_at: float = credentials.get("expires_at", 0.0)
        if not access_token or time.time() > (expires_at - _EXPIRY_BUFFER_SECONDS):
            credentials = self._refresh_access_token(credentials)
            self._store.set(self.source, account.account_ref, credentials)
            access_token = credentials["access_token"]

        ingested_at = datetime.now(timezone.utc)

        # Build date-window params
        date_params: dict[str, Any] = {}
        if since is not None:
            date_params["start_date"] = since.date().isoformat()
        date_params["end_date"] = datetime.now(timezone.utc).date().isoformat()

        collected: list[Record] = []

        # ------------------------------------------------------------------
        # 1. Sleep sessions
        # ------------------------------------------------------------------
        try:
            sleep_items = _get_paginated(
                "/v2/usercollection/sleep", access_token, dict(date_params)
            )
            for raw_sleep in sleep_items:
                record = self._map_sleep(raw_sleep, account.account_ref, ingested_at)
                collected.append(record)
        except (RateLimited, ProviderUnavailable, ProviderPayloadError, AuthRequired):
            raise
        except Exception as exc:
            if collected:
                raise PartialFetchError(
                    f"Sleep fetch failed mid-stream: {exc}",
                    records=list(collected),
                    cause=exc,
                ) from exc
            raise

        # ------------------------------------------------------------------
        # 2. Daily readiness
        # ------------------------------------------------------------------
        try:
            readiness_items = _get_paginated(
                "/v2/usercollection/daily_readiness", access_token, dict(date_params)
            )
            for raw_readiness in readiness_items:
                record = self._map_readiness(raw_readiness, account.account_ref, ingested_at)
                collected.append(record)
        except (RateLimited, ProviderUnavailable, ProviderPayloadError, AuthRequired):
            if collected:
                raise PartialFetchError(
                    "Readiness fetch failed after sleep records were collected.",
                    records=list(collected),
                )
            raise
        except Exception as exc:
            raise PartialFetchError(
                f"Readiness fetch failed mid-stream: {exc}",
                records=list(collected),
                cause=exc,
            ) from exc

        # ------------------------------------------------------------------
        # 3. Workouts
        # ------------------------------------------------------------------
        try:
            workout_items = _get_paginated(
                "/v2/usercollection/workout", access_token, dict(date_params)
            )
            for raw_workout in workout_items:
                record = self._map_workout(raw_workout, account.account_ref, ingested_at)
                collected.append(record)
        except (RateLimited, ProviderUnavailable, ProviderPayloadError, AuthRequired):
            if collected:
                raise PartialFetchError(
                    "Workout fetch failed after earlier domains collected records.",
                    records=list(collected),
                )
            raise
        except Exception as exc:
            raise PartialFetchError(
                f"Workout fetch failed mid-stream: {exc}",
                records=list(collected),
                cause=exc,
            ) from exc

        return collected

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------

    def _map_sleep(
        self,
        raw: dict[str, Any],
        account_ref: str,
        ingested_at: datetime,
    ) -> Record:
        """Map an Oura /v2/usercollection/sleep record → sleep_session Record.

        Identity: native Oura id → source_record_id = raw["id"].
        """
        sleep_id = raw.get("id")
        if not sleep_id:
            raise ProviderPayloadError(
                f"Oura sleep record missing 'id'. Keys: {list(raw.keys())}"
            )

        occurred_at = _parse_utc(raw.get("bedtime_start"))
        if occurred_at is None:
            raise ProviderPayloadError(
                f"Oura sleep {sleep_id!r} missing 'bedtime_start' timestamp."
            )
        occurred_end = _parse_utc(raw.get("bedtime_end"))

        # Oura provides a local attribution day directly
        local_date_str = raw.get("day")
        local_date = None
        if local_date_str:
            try:
                from datetime import date as _date
                local_date = _date.fromisoformat(local_date_str)
            except (ValueError, TypeError):
                pass

        # Sleep stage durations: Oura returns seconds → convert to minutes
        total_sleep_dur = raw.get("total_sleep_duration")
        time_in_bed_dur = raw.get("time_in_bed")
        rem_dur = raw.get("rem_sleep_duration")
        deep_dur = raw.get("deep_sleep_duration")
        light_dur = raw.get("light_sleep_duration")
        awake_dur = raw.get("awake_time")
        latency = raw.get("latency")

        common = SleepSessionCommon(
            total_sleep_minutes=total_sleep_dur // 60 if total_sleep_dur is not None else None,
            time_in_bed_minutes=time_in_bed_dur // 60 if time_in_bed_dur is not None else None,
            sleep_efficiency_pct=raw.get("efficiency"),
            sleep_latency_minutes=latency // 60 if latency is not None else None,
            stage_rem_minutes=rem_dur // 60 if rem_dur is not None else None,
            stage_deep_minutes=deep_dur // 60 if deep_dur is not None else None,
            stage_light_minutes=light_dur // 60 if light_dur is not None else None,
            stage_awake_minutes=awake_dur // 60 if awake_dur is not None else None,
            avg_heart_rate_bpm=raw.get("average_heart_rate"),
            avg_hrv_ms=raw.get("average_hrv"),
            respiratory_rate_bpm=raw.get("average_breath"),
            avg_spo2_pct=None,       # not in Oura sleep record directly
            awakenings_count=None,   # not exposed in this schema
            provider_sleep_score=raw.get("score"),  # top-level sleep score
            score_state=None,
        )

        # Readiness score from embedded readiness block → source_extras
        readiness = raw.get("readiness") or {}
        source_extras: dict[str, Any] = {
            "readiness_score": readiness.get("score"),
            "readiness_score_delta": raw.get("readiness_score_delta"),
            "sleep_score_delta": raw.get("sleep_score_delta"),
            "sleep_type": raw.get("type"),
            "period": raw.get("period"),
            "low_battery_alert": raw.get("low_battery_alert"),
        }

        return Record(
            source=self.source,
            domain="sleep",
            record_type=RecordType.SLEEP_SESSION,
            source_record_id=str(sleep_id),
            account_ref=account_ref,
            occurred_at=occurred_at,
            occurred_end=occurred_end,
            ingested_at=ingested_at,
            timezone=None,   # Oura does not return IANA timezone names in sleep records
            local_date=local_date,
            common=common,
            raw=raw,
            source_extras=source_extras,
        )

    def _map_readiness(
        self,
        raw: dict[str, Any],
        account_ref: str,
        ingested_at: datetime,
    ) -> Record:
        """Map an Oura /v2/usercollection/daily_readiness record → daily_recovery Record.

        Identity: daily aggregate → source_record_id = f"{account_ref}:{day}:daily_recovery".
        """
        day = raw.get("day")
        if not day:
            raise ProviderPayloadError(
                f"Oura daily_readiness record missing 'day'. Keys: {list(raw.keys())}"
            )

        source_record_id = f"{account_ref}:{day}:daily_recovery"

        occurred_at = _parse_utc(raw.get("timestamp"))
        if occurred_at is None:
            # Fall back to midnight UTC of the attribution day
            from datetime import date as _date
            try:
                d = _date.fromisoformat(day)
                occurred_at = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            except (ValueError, TypeError) as exc:
                raise ProviderPayloadError(
                    f"Oura daily_readiness has unparseable day: {day!r}"
                ) from exc

        local_date = None
        try:
            from datetime import date as _date
            local_date = _date.fromisoformat(day)
        except (ValueError, TypeError):
            pass

        contributors = raw.get("contributors") or {}

        common = DailyRecoveryCommon(
            hrv_ms=None,                         # not exposed directly in daily_readiness
            resting_heart_rate_bpm=None,         # not exposed directly in daily_readiness
            provider_recovery_score=raw.get("score"),
            avg_spo2_pct=None,                   # not in daily_readiness schema
            skin_temp_celsius=raw.get("temperature_deviation"),
            respiratory_rate_bpm=None,           # not in daily_readiness schema
            score_state=None,
        )

        source_extras: dict[str, Any] = {
            "contributors": contributors,
            "temperature_trend_deviation": raw.get("temperature_trend_deviation"),
        }

        return Record(
            source=self.source,
            domain="recovery",
            record_type=RecordType.DAILY_RECOVERY,
            source_record_id=source_record_id,
            account_ref=account_ref,
            occurred_at=occurred_at,
            occurred_end=None,
            ingested_at=ingested_at,
            timezone=None,
            local_date=local_date,
            common=common,
            raw=raw,
            source_extras=source_extras,
        )

    def _map_workout(
        self,
        raw: dict[str, Any],
        account_ref: str,
        ingested_at: datetime,
    ) -> Record:
        """Map an Oura /v2/usercollection/workout record → workout Record.

        Identity: native Oura id → source_record_id = raw["id"].
        """
        workout_id = raw.get("id")
        if not workout_id:
            raise ProviderPayloadError(
                f"Oura workout record missing 'id'. Keys: {list(raw.keys())}"
            )

        occurred_at = _parse_utc(raw.get("start_datetime"))
        if occurred_at is None:
            raise ProviderPayloadError(
                f"Oura workout {workout_id!r} missing 'start_datetime' timestamp."
            )
        occurred_end = _parse_utc(raw.get("end_datetime"))

        # Duration: derived from start/end timestamps
        duration_minutes: float | None = None
        if occurred_at and occurred_end:
            duration_seconds = (occurred_end - occurred_at).total_seconds()
            duration_minutes = round(duration_seconds / 60, 1)

        local_date_str = raw.get("day")
        local_date = None
        if local_date_str:
            try:
                from datetime import date as _date
                local_date = _date.fromisoformat(local_date_str)
            except (ValueError, TypeError):
                pass

        activity = raw.get("activity")
        activity_type = _map_activity_type(activity)

        distance = raw.get("distance")
        calories = raw.get("calories")

        common = WorkoutCommon(
            activity_type=activity_type,
            activity_label=activity,
            duration_minutes=duration_minutes,
            elapsed_minutes=None,
            distance_meters=float(distance) if distance is not None else None,
            elevation_gain_meters=None,
            avg_heart_rate_bpm=None,
            max_heart_rate_bpm=None,
            energy_kcal=float(calories) if calories is not None else None,
            avg_speed_mps=None,
            avg_power_watts=None,
            total_volume_kg=None,
            set_count=None,
            exercises=[],
            provider_strain=None,
            score_state=None,
        )

        source_extras: dict[str, Any] = {
            "intensity": raw.get("intensity"),
            "source": raw.get("source"),
            "label": raw.get("label"),
        }

        return Record(
            source=self.source,
            domain="activity",
            record_type=RecordType.WORKOUT,
            source_record_id=str(workout_id),
            account_ref=account_ref,
            occurred_at=occurred_at,
            occurred_end=occurred_end,
            ingested_at=ingested_at,
            timezone=None,
            local_date=local_date,
            common=common,
            raw=raw,
            source_extras=source_extras,
        )
