"""
WHOOP connector for OpenVitals.

API: WHOOP Developer API v2 — https://api.prod.whoop.com/developer/v2

## Endpoints used
- GET /v2/activity/sleep     → sleep_session records
- GET /v2/recovery           → daily_recovery records (linked to cycle for date)
- GET /v2/cycle              → used only for day attribution on daily_recovery
- GET /v2/activity/workout   → workout records
- GET /v2/user/profile/basic → account_ref (stable WHOOP user_id)

## Identity rules (connector-contract.md §2)
- sleep_session:    source_record_id = sleep["id"]          (WHOOP UUID — native)
- workout:          source_record_id = workout["id"]        (WHOOP UUID — native)
- daily_recovery:   source_record_id = f"{account_ref}:{local_date}:daily_recovery"
                    (daily aggregate — no stable provider ID; date from cycle.start[:10])

## Score state mapping
WHOOP "SCORED"        → ScoreState.SCORED
WHOOP "PENDING_SCORE" → ScoreState.PENDING
WHOOP "UNSCORABLE"    → ScoreState.UNSCORED
All three are always emitted — never filtered (contract §1).

## Scopes required
read:recovery read:cycles read:sleep read:workout read:profile offline

## Token rotation
WHOOP rotates the refresh token on every use.  ``connect()`` refreshes on expiry
(< 5 minutes remaining) and immediately persists the new refresh token.  Losing
this step orphans the token permanently — do not skip the persist step.

## Auth credentials dict shape (CredentialStore key = ("whoop", account_ref))
{
    "access_token": str,
    "refresh_token": str,
    "expires_at": float,   # UTC epoch seconds
    "scopes": list[str],
}

The initial token bundle is obtained externally (OAuth code exchange) and stored
via CredentialStore.set() before connect() is called.

## Pagination
All list endpoints use cursor pagination: params include ``limit=25`` and
``nextToken`` on continuation.  The connector drains all pages.

## Rate limits
100 req/min, 10 000 req/day.  429 → RateLimited with retry_after from header.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from openvitals.connectors.base import Account, BaseConnector
from openvitals.utils.units import kj_to_kcal as _kj_to_kcal
from openvitals.errors import (
    AuthRequired,
    MissingScope,
    PartialFetchError,
    ProviderPayloadError,
    ProviderUnavailable,
    RateLimited,
)
from openvitals.schema.domains import ActivityType, RecordType, ScoreState
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

WHOOP_API_BASE = "https://api.prod.whoop.com/developer/v2"
WHOOP_TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

REQUIRED_SCOPES = {
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "offline",
}

# Threshold in seconds — refresh the access token if it expires within 5 minutes
_EXPIRY_BUFFER_SECONDS = 300

# Page size for all paginated endpoints (WHOOP max is 25)
_PAGE_LIMIT = 25


# ---------------------------------------------------------------------------
# Score state mapping
# ---------------------------------------------------------------------------

_SCORE_STATE_MAP: dict[str, ScoreState] = {
    "SCORED": ScoreState.SCORED,
    "PENDING_SCORE": ScoreState.PENDING,
    "UNSCORABLE": ScoreState.UNSCORED,
}


def _map_score_state(whoop_state: str | None) -> ScoreState | None:
    """Map WHOOP score_state string → ScoreState enum.

    Raises ProviderPayloadError if the value is non-null but unrecognised,
    so we detect API changes loudly rather than silently dropping data.
    """
    if whoop_state is None:
        return None
    mapped = _SCORE_STATE_MAP.get(whoop_state)
    if mapped is None:
        raise ProviderPayloadError(
            f"Unrecognised WHOOP score_state: {whoop_state!r}. "
            "The WHOOP API may have changed — update _SCORE_STATE_MAP."
        )
    return mapped


# ---------------------------------------------------------------------------
# Activity type mapping
# ---------------------------------------------------------------------------

# WHOOP sport_name strings (lowercased) → ActivityType enum.
# The original sport_name is always preserved in activity_label.
_SPORT_NAME_MAP: dict[str, ActivityType] = {
    "running": ActivityType.RUN,
    "run": ActivityType.RUN,
    "trail run": ActivityType.RUN,
    "cycling": ActivityType.RIDE,
    "ride": ActivityType.RIDE,
    "biking": ActivityType.RIDE,
    "swimming": ActivityType.SWIM,
    "swim": ActivityType.SWIM,
    "walking": ActivityType.WALK,
    "walk": ActivityType.WALK,
    "hiking": ActivityType.HIKE,
    "hike": ActivityType.HIKE,
    "strength training": ActivityType.STRENGTH,
    "strength": ActivityType.STRENGTH,
    "weightlifting": ActivityType.STRENGTH,
    "functional fitness": ActivityType.STRENGTH,
    "cardio": ActivityType.CARDIO,
    "rowing": ActivityType.CARDIO,
    "elliptical": ActivityType.CARDIO,
    "sauna": ActivityType.SAUNA,
}


def _map_activity_type(sport_name: str | None) -> ActivityType:
    """Map WHOOP sport_name → ActivityType.

    Wellness activities like sauna map to ActivityType.SAUNA when the enum
    covers it; all others fall back to ActivityType.OTHER.  The raw sport_name
    is always preserved in activity_label — never dropped.
    """
    if not sport_name:
        return ActivityType.OTHER
    return _SPORT_NAME_MAP.get(sport_name.lower(), ActivityType.OTHER)


# ---------------------------------------------------------------------------
# UTC helpers
# ---------------------------------------------------------------------------

def _parse_utc(ts: str | None) -> datetime | None:
    """Parse a WHOOP ISO-8601 timestamp (may end in Z or +offset) → UTC datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ProviderPayloadError(f"Cannot parse WHOOP timestamp {ts!r}: {exc}") from exc


def _local_date_from_utc(utc_dt: datetime | None, tz_offset: str | None) -> date | None:
    """Return the local date by applying a WHOOP timezone_offset string to a UTC datetime.

    WHOOP provides ``timezone_offset`` as e.g. "+01:00" or "-05:00".
    Applying it before taking the date ensures cross-midnight sessions attribute
    to the correct local day (e.g. 23:30 UTC with +01:00 → 00:30 local → next local day).

    Falls back to the UTC date if the offset cannot be parsed or utc_dt is None.
    """
    if utc_dt is None:
        return None
    if tz_offset:
        try:
            # Parse "+HH:MM" or "-HH:MM"
            sign = 1 if tz_offset[0] != "-" else -1
            parts = tz_offset.lstrip("+-").split(":")
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
            offset = timedelta(hours=hours, minutes=minutes) * sign
            return (utc_dt + offset).date()
        except (ValueError, IndexError):
            pass
    return utc_dt.date()


def _millis_to_minutes(ms: float | int | None) -> float | None:
    """Convert milliseconds → minutes, rounded to 1 decimal. Returns None if None."""
    if ms is None:
        return None
    return round(ms / 60_000, 1)



# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_paginated(
    endpoint: str,
    access_token: str,
    params: dict[str, Any],
) -> list[dict]:
    """Drain a paginated WHOOP list endpoint. Returns all records.

    Raises RateLimited, ProviderUnavailable, or ProviderPayloadError on errors.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    results: list[dict] = []
    page_params = dict(params)

    while True:
        try:
            resp = httpx.get(
                f"{WHOOP_API_BASE}{endpoint}",
                headers=headers,
                params=page_params,
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"WHOOP API unreachable: {exc}") from exc

        if resp.status_code == 401:
            raise AuthRequired("WHOOP access token rejected (401). Call connect() to refresh.")
        if resp.status_code == 429:
            retry_after_raw = resp.headers.get("X-RateLimit-Reset") or resp.headers.get("Retry-After")
            retry_after: float | None = None
            if retry_after_raw:
                try:
                    retry_after = float(retry_after_raw)
                except (ValueError, TypeError):
                    pass
            raise RateLimited("WHOOP rate limit exceeded (429).", retry_after=retry_after)
        if resp.status_code >= 500:
            raise ProviderUnavailable(f"WHOOP API returned {resp.status_code}.")
        if resp.status_code >= 400:
            raise ProviderPayloadError(
                f"WHOOP API returned {resp.status_code} for {endpoint}: {resp.text[:200]}"
            )

        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(f"WHOOP returned non-JSON response: {exc}") from exc

        records = data.get("records")
        if records is None:
            raise ProviderPayloadError(
                f"WHOOP response missing 'records' key for {endpoint}. "
                f"Keys present: {list(data.keys())}"
            )

        results.extend(records)

        next_token = data.get("next_token")
        if not next_token:
            break
        # WHOOP uses 'nextToken' as the query param name for continuation
        page_params["nextToken"] = next_token

    return results


# ---------------------------------------------------------------------------
# WhoopConnector
# ---------------------------------------------------------------------------


class WhoopConnector(BaseConnector):
    """
    OpenVitals connector for the WHOOP Developer API v2.

    Domains: sleep (sleep_session), recovery (daily_recovery), activity (workout).

    Usage::

        from openvitals.connectors.whoop import WhoopConnector
        from openvitals.storage.credentials import EncryptedFileStore

        store = EncryptedFileStore()
        connector = WhoopConnector(credential_store=store)

        # credentials must already be stored (from an OAuth flow)
        account = connector.connect(credentials=store.get("whoop", "<account_ref>"))
        records = list(connector.fetch(account, since=datetime(2026, 6, 1, tzinfo=timezone.utc)))
    """

    source = "whoop"
    domains = ["sleep", "recovery", "activity"]

    # Required scopes — validated in connect()
    _required_scopes = REQUIRED_SCOPES

    def __init__(self, credential_store: CredentialStore) -> None:
        """
        Parameters
        ----------
        credential_store:
            A CredentialStore implementation.  The connector uses it to persist
            rotated refresh tokens.  Never logs token values.
        """
        self._store = credential_store

    # ------------------------------------------------------------------
    # connect()
    # ------------------------------------------------------------------

    def connect(self, credentials: dict[str, Any]) -> Account:
        """Validate credentials and return an Account.

        ``credentials`` must be a dict with keys:
            access_token, refresh_token, expires_at (UTC epoch float), scopes (list[str])

        If the access token is expired or within 5 minutes of expiry, this method
        refreshes it using the stored refresh token and persists the new token
        bundle back to the CredentialStore.

        Raises AuthRequired if no valid credential can be established.
        Raises MissingScope if ``read:cycles`` or any other required scope is absent.

        ``client_id`` and ``client_secret`` are read from the credential dict
        (stored there during OAuth) or must be supplied separately.  If not
        available a ProviderPayloadError is raised with a clear message.
        """
        if not credentials:
            raise AuthRequired(
                "No credentials provided. Run the WHOOP OAuth flow to obtain tokens "
                "and store them via CredentialStore.set()."
            )

        # --- Scope validation (loud failure, not silent empty result) ----------
        granted: set[str] = set(credentials.get("scopes", []))
        missing_scopes = self._required_scopes - granted
        if missing_scopes:
            raise MissingScope(
                f"WHOOP credentials are missing required scopes: {missing_scopes}. "
                f"Re-authenticate with all required scopes: {self._required_scopes}",
                required_scope="read:cycles",  # most critical — often missed
                granted_scopes=granted,
            )

        # --- Token refresh if near expiry -------------------------------------
        expires_at: float = credentials.get("expires_at", 0.0)
        access_token: str = credentials.get("access_token", "")

        if not access_token or time.time() > (expires_at - _EXPIRY_BUFFER_SECONDS):
            credentials = self._refresh_access_token(credentials)
            access_token = credentials["access_token"]

        # --- Fetch stable WHOOP user_id as account_ref -----------------------
        try:
            resp = httpx.get(
                f"{WHOOP_API_BASE}/user/profile/basic",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=30,
            )
        except httpx.TransportError as exc:
            raise ProviderUnavailable(f"WHOOP profile endpoint unreachable: {exc}") from exc

        if resp.status_code == 401:
            raise AuthRequired("WHOOP access token rejected on profile fetch (401).")
        if resp.status_code >= 400:
            raise ProviderPayloadError(
                f"WHOOP profile fetch returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            profile = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(f"WHOOP profile response is not JSON: {exc}") from exc

        whoop_user_id = profile.get("user_id")
        if whoop_user_id is None:
            raise ProviderPayloadError(
                "WHOOP profile response missing 'user_id'. "
                f"Keys present: {list(profile.keys())}"
            )

        account_ref = str(whoop_user_id)

        # Persist (potentially refreshed) credentials keyed by stable account_ref
        # NEVER log the credential dict contents
        self._store.set(self.source, account_ref, credentials)

        return Account(
            account_ref=account_ref,
            source=self.source,
            metadata={
                "scopes": list(granted),
                "expires_at": credentials.get("expires_at"),
                # profile fields for display — no secrets
                "first_name": profile.get("first_name"),
                "last_name": profile.get("last_name"),
                "email": profile.get("email"),
            },
        )

    def _refresh_access_token(self, credentials: dict[str, Any]) -> dict[str, Any]:
        """Use the refresh token to obtain a new access token.

        WHOOP rotates the refresh token on every use — the new refresh token
        is stored back to the credential dict immediately.  Never log token values.

        Raises AuthRequired if the refresh fails (token has been revoked).
        """
        client_id = credentials.get("client_id", "")
        client_secret = credentials.get("client_secret", "")
        refresh_token = credentials.get("refresh_token", "")

        if not refresh_token:
            raise AuthRequired(
                "No refresh token in stored credentials. "
                "Re-run the WHOOP OAuth flow to obtain fresh tokens."
            )
        if not client_id or not client_secret:
            raise AuthRequired(
                "client_id and client_secret must be stored in the credential dict "
                "to enable token refresh. Store them during the initial OAuth exchange."
            )

        try:
            resp = httpx.post(
                WHOOP_TOKEN_URL,
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
            raise ProviderUnavailable(f"WHOOP token endpoint unreachable: {exc}") from exc

        if resp.status_code in (400, 401):
            raise AuthRequired(
                f"WHOOP refresh token rejected ({resp.status_code}). "
                "The token may have been revoked. Re-run the OAuth flow."
            )
        if resp.status_code >= 400:
            raise ProviderPayloadError(
                f"WHOOP token refresh returned {resp.status_code}: {resp.text[:200]}"
            )

        try:
            tokens = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(f"WHOOP token response is not JSON: {exc}") from exc

        new_access_token = tokens.get("access_token")
        new_refresh_token = tokens.get("refresh_token")

        if not new_access_token or not new_refresh_token:
            raise ProviderPayloadError(
                "WHOOP token response missing access_token or refresh_token. "
                f"Keys present: {list(tokens.keys())}"
            )

        # Build updated credential dict — carry forward client_id/secret and scopes
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
        """Yield Records for all three WHOOP domains.

        Drains /activity/sleep, /recovery + /cycle, and /activity/workout.
        All pages are fully consumed before yielding.  If a domain fetch fails
        mid-stream, yields the records collected so far then raises PartialFetchError.

        ``since`` is treated as the start of the fetch window (inclusive).
        If None, fetches all available history (use with care — may be large).

        Records are never filtered on score_state — SCORED, PENDING, and UNSCORED
        are all yielded (consumer decides what to use).
        """
        credentials = self._store.get(self.source, account.account_ref)
        if not credentials:
            raise AuthRequired(
                f"No stored credentials for whoop/{account.account_ref}. "
                "Call connect() first."
            )

        access_token = credentials.get("access_token", "")
        expires_at: float = credentials.get("expires_at", 0.0)
        if not access_token or time.time() > (expires_at - _EXPIRY_BUFFER_SECONDS):
            credentials = self._refresh_access_token(credentials)
            self._store.set(self.source, account.account_ref, credentials)
            access_token = credentials["access_token"]

        ingested_at = datetime.now(timezone.utc)

        # Build time-window params
        base_params: dict[str, Any] = {"limit": _PAGE_LIMIT}
        if since is not None:
            base_params["start"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        # No "end" param → WHOOP defaults to now

        collected: list[Record] = []
        records_fetched = 0

        # ------------------------------------------------------------------
        # 1. Sleep sessions
        # ------------------------------------------------------------------
        try:
            sleep_records = _get_paginated(
                "/activity/sleep", access_token, dict(base_params)
            )
            for raw_sleep in sleep_records:
                record = self._map_sleep(raw_sleep, account.account_ref, ingested_at)
                if record is not None:
                    collected.append(record)
                    records_fetched += 1
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
        # 2. Daily recovery (needs cycle for date attribution)
        # ------------------------------------------------------------------
        try:
            recovery_records = _get_paginated(
                "/recovery", access_token, dict(base_params)
            )
            cycle_records = _get_paginated(
                "/cycle", access_token, dict(base_params)
            )
            cycles_by_id = {c["id"]: c for c in cycle_records}

            for raw_rec in recovery_records:
                record = self._map_recovery(
                    raw_rec, cycles_by_id, account.account_ref, ingested_at
                )
                if record is not None:
                    collected.append(record)
                    records_fetched += 1
        except (RateLimited, ProviderUnavailable, ProviderPayloadError, AuthRequired):
            if collected:
                raise PartialFetchError(
                    "Recovery/cycle fetch failed after sleep records were collected.",
                    records=list(collected),
                )
            raise
        except Exception as exc:
            raise PartialFetchError(
                f"Recovery fetch failed mid-stream: {exc}",
                records=list(collected),
                cause=exc,
            ) from exc

        # ------------------------------------------------------------------
        # 3. Workouts
        # ------------------------------------------------------------------
        try:
            workout_records = _get_paginated(
                "/activity/workout", access_token, dict(base_params)
            )
            for raw_workout in workout_records:
                record = self._map_workout(raw_workout, account.account_ref, ingested_at)
                if record is not None:
                    collected.append(record)
                    records_fetched += 1
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
    ) -> Record | None:
        """Map a WHOOP /activity/sleep record → sleep_session Record.

        Naps (raw["nap"] == True) are included — they are valid sleep events.
        ASSUMPTION: nap filtering is a consumer decision, not connector policy.

        Returns None only if the record is missing a required ID or start time,
        which would make it unidentifiable.  This is logged as a ProviderPayloadError.
        """
        sleep_id = raw.get("id")
        if not sleep_id:
            raise ProviderPayloadError(
                f"WHOOP sleep record missing 'id'. Keys: {list(raw.keys())}"
            )

        start_ts = _parse_utc(raw.get("start"))
        if start_ts is None:
            raise ProviderPayloadError(
                f"WHOOP sleep {sleep_id!r} missing 'start' timestamp."
            )

        end_ts = _parse_utc(raw.get("end"))

        score_state_str = raw.get("score_state")
        score_state = _map_score_state(score_state_str)

        # score block is only present when SCORED
        score = raw.get("score") or {}
        stage_summary = score.get("stage_summary") or {}

        # total_sleep = in_bed - awake
        in_bed_ms = stage_summary.get("total_in_bed_time_milli")
        awake_ms = stage_summary.get("total_awake_time_milli")
        if in_bed_ms is not None and awake_ms is not None:
            total_sleep_ms = in_bed_ms - awake_ms
            total_sleep_min = round(total_sleep_ms / 60_000, 1)
            total_sleep_minutes = int(total_sleep_min)
        else:
            total_sleep_minutes = None

        time_in_bed_minutes_raw = _millis_to_minutes(in_bed_ms)
        time_in_bed_minutes = int(time_in_bed_minutes_raw) if time_in_bed_minutes_raw is not None else None

        # WHOOP HRV is in rMSSD milliseconds — no unit conversion needed
        hrv_rmssd_milli = score.get("hrv_rmssd_milli")

        # sleep_latency is not provided by WHOOP v2 API
        # ASSUMPTION: WHOOP v2 does not expose sleep latency in the score block.
        # Verify against live API if this changes.

        common = SleepSessionCommon(
            total_sleep_minutes=total_sleep_minutes,
            time_in_bed_minutes=time_in_bed_minutes,
            sleep_efficiency_pct=score.get("sleep_efficiency_percentage"),
            sleep_latency_minutes=None,  # not in WHOOP v2 API
            awakenings_count=stage_summary.get("disturbance_count"),
            stage_rem_minutes=int(_millis_to_minutes(stage_summary.get("total_rem_sleep_time_milli")) or 0) or None,
            stage_deep_minutes=int(_millis_to_minutes(stage_summary.get("total_slow_wave_sleep_time_milli")) or 0) or None,
            stage_light_minutes=int(_millis_to_minutes(stage_summary.get("total_light_sleep_time_milli")) or 0) or None,
            stage_awake_minutes=int(_millis_to_minutes(awake_ms) or 0) or None,
            avg_heart_rate_bpm=None,  # not in WHOOP v2 SleepScore (verified against whoop-api-reference.md)
            avg_hrv_ms=float(hrv_rmssd_milli) if hrv_rmssd_milli is not None else None,
            respiratory_rate_bpm=score.get("respiratory_rate"),
            avg_spo2_pct=None,  # not in WHOOP v2 SleepScore (verified against whoop-api-reference.md; spo2 is RecoveryScore only)
            provider_sleep_score=(
                int(score["sleep_performance_percentage"])
                if score.get("sleep_performance_percentage") is not None
                else None
            ),
            score_state=score_state,
        )

        # local_date: apply timezone_offset to the UTC start so cross-midnight
        # sessions attribute to the correct local day (e.g. 23:30Z + 01:00 → 00:30
        # local → the *next* local day, not the UTC day).
        local_date = _local_date_from_utc(start_ts, raw.get("timezone_offset"))

        return Record(
            source=self.source,
            domain="sleep",
            record_type=RecordType.SLEEP_SESSION,
            source_record_id=str(sleep_id),
            account_ref=account_ref,
            occurred_at=start_ts,
            occurred_end=end_ts,
            ingested_at=ingested_at,
            timezone=raw.get("timezone_offset"),
            local_date=local_date,
            common=common,
            raw=raw,
        )

    def _map_recovery(
        self,
        raw_rec: dict[str, Any],
        cycles_by_id: dict[int, dict],
        account_ref: str,
        ingested_at: datetime,
    ) -> Record | None:
        """Map a WHOOP /recovery record → daily_recovery Record.

        Identity: daily aggregate → source_record_id = f"{account_ref}:{local_date}:daily_recovery"
        Date: derived from cycle.start with timezone_offset applied.

        Returns None only if local_date cannot be determined (truly unparseable).
        All score states (SCORED, PENDING, UNSCORED) are emitted.
        """
        rec_score = raw_rec.get("score") or {}
        score_state = _map_score_state(raw_rec.get("score_state"))

        # Date from cycle.start with timezone_offset applied
        cycle_id = raw_rec.get("cycle_id")
        cycle = cycles_by_id.get(cycle_id, {}) if cycle_id is not None else {}
        cycle_start = cycle.get("start") or raw_rec.get("created_at", "")

        if not cycle_start:
            raise ProviderPayloadError(
                f"WHOOP recovery record {raw_rec.get('id')!r} has no usable date "
                "(cycle.start and created_at both missing)."
            )

        # Apply timezone_offset before taking the date — same pattern as sleep/workout.
        # Cycles that start near midnight in the user's local timezone would attribute
        # to the wrong calendar day if we slice the UTC string directly.
        utc_dt = _parse_utc(cycle_start)
        if utc_dt is None:
            raise ProviderPayloadError(
                f"WHOOP recovery record {raw_rec.get('id')!r} has unparseable date: {cycle_start!r}"
            )
        tz_offset = cycle.get("timezone_offset")
        local_date = _local_date_from_utc(utc_dt, tz_offset)
        if local_date is None:
            local_date = utc_dt.date()

        source_record_id = f"{account_ref}:{local_date.isoformat()}:daily_recovery"

        # occurred_at: use cycle start as the "event start" for this daily record
        occurred_at = _parse_utc(cycle_start) or _parse_utc(raw_rec.get("created_at"))
        if occurred_at is None:
            # Fall back to local_date midnight UTC — rare case
            occurred_at = datetime(local_date.year, local_date.month, local_date.day, tzinfo=timezone.utc)

        # HRV: hrv_rmssd_milli is in milliseconds (no conversion needed)
        hrv_rmssd_milli = rec_score.get("hrv_rmssd_milli")

        common = DailyRecoveryCommon(
            hrv_ms=float(hrv_rmssd_milli) if hrv_rmssd_milli is not None else None,
            resting_heart_rate_bpm=(
                int(rec_score["resting_heart_rate"])
                if rec_score.get("resting_heart_rate") is not None
                else None
            ),
            provider_recovery_score=(
                int(rec_score["recovery_score"])
                if rec_score.get("recovery_score") is not None
                else None
            ),
            avg_spo2_pct=rec_score.get("spo2_percentage"),
            skin_temp_celsius=rec_score.get("skin_temp_celsius"),
            respiratory_rate_bpm=None,  # respiratory_rate lives on sleep score, not recovery score
            score_state=score_state,
        )

        return Record(
            source=self.source,
            domain="recovery",
            record_type=RecordType.DAILY_RECOVERY,
            source_record_id=source_record_id,
            account_ref=account_ref,
            occurred_at=occurred_at,
            occurred_end=None,
            ingested_at=ingested_at,
            timezone=cycle.get("timezone_offset"),
            local_date=local_date,
            common=common,
            raw=raw_rec,
            source_extras={
                "cycle_id": cycle_id,
                "sleep_id": raw_rec.get("sleep_id"),
                "cycle_strain": (cycle.get("score") or {}).get("strain"),
            },
        )

    def _map_workout(
        self,
        raw: dict[str, Any],
        account_ref: str,
        ingested_at: datetime,
    ) -> Record | None:
        """Map a WHOOP /activity/workout record → workout Record.

        Identity: native WHOOP UUID → source_record_id = raw["id"].
        All score states emitted (never filtered).
        Sauna and wellness activities → ActivityType.SAUNA or ActivityType.OTHER.
        """
        workout_id = raw.get("id")
        if not workout_id:
            raise ProviderPayloadError(
                f"WHOOP workout record missing 'id'. Keys: {list(raw.keys())}"
            )

        start_ts = _parse_utc(raw.get("start"))
        if start_ts is None:
            raise ProviderPayloadError(
                f"WHOOP workout {workout_id!r} missing 'start' timestamp."
            )

        end_ts = _parse_utc(raw.get("end"))
        score_state = _map_score_state(raw.get("score_state"))

        sport_name = raw.get("sport_name")
        activity_type = _map_activity_type(sport_name)

        score = raw.get("score") or {}

        # Duration: derive from start/end timestamps
        duration_minutes: float | None = None
        if start_ts and end_ts:
            duration_seconds = (end_ts - start_ts).total_seconds()
            duration_minutes = round(duration_seconds / 60, 1)

        # Energy: WHOOP stores kilojoules; convert to kcal
        energy_kcal = _kj_to_kcal(score.get("kilojoule"))

        # Heart rate: WHOOP API returns int32 for avg and max HR
        avg_hr = score.get("average_heart_rate")
        max_hr = score.get("max_heart_rate")

        # distance_meter is only present if GPS data exists
        distance_meters = score.get("distance_meter")

        common = WorkoutCommon(
            activity_type=activity_type,
            activity_label=sport_name,  # original provider string preserved
            duration_minutes=duration_minutes,
            elapsed_minutes=duration_minutes,  # WHOOP does not distinguish active vs elapsed
            distance_meters=float(distance_meters) if distance_meters is not None else None,
            elevation_gain_meters=score.get("altitude_gain_meter"),
            avg_heart_rate_bpm=int(avg_hr) if avg_hr is not None else None,
            max_heart_rate_bpm=int(max_hr) if max_hr is not None else None,
            energy_kcal=energy_kcal,
            avg_speed_mps=None,   # not in WHOOP v2 workout score
            avg_power_watts=None, # not in WHOOP v2 workout score
            total_volume_kg=None, # WHOOP is not a strength platform
            set_count=None,
            exercises=[],
            provider_strain=score.get("strain"),
            score_state=score_state,
        )

        # local_date: apply timezone_offset to the UTC start so cross-midnight
        # sessions attribute to the correct local day (same logic as sleep).
        local_date = _local_date_from_utc(start_ts, raw.get("timezone_offset"))

        return Record(
            source=self.source,
            domain="activity",
            record_type=RecordType.WORKOUT,
            source_record_id=str(workout_id),
            account_ref=account_ref,
            occurred_at=start_ts,
            occurred_end=end_ts,
            ingested_at=ingested_at,
            timezone=raw.get("timezone_offset"),
            local_date=local_date,
            common=common,
            raw=raw,
            source_extras={
                "sport_id": raw.get("sport_id"),
                "percent_recorded": score.get("percent_recorded"),
                "zone_durations": score.get("zone_durations"),
            },
        )
