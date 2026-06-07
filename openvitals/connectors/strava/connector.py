"""
StravaConnector — OpenVitals connector for Strava.

Domain: activity → workout (cardio only; Strava has no strength data).

Auth (connector-contract.md §3):
- OAuth 2.0; credentials dict keys: ``access_token``, ``refresh_token``,
  ``expires_at`` (Unix epoch int), ``athlete_id`` (int), ``scope`` (str).
- Access token is refreshed if expiry is within 1 hour of now.
- Rotated refresh token is persisted to CredentialStore BEFORE the new
  access token is returned — losing this orphans the token on Strava's side.
- Required scope: ``activity:read_all``. Missing scope → ``MissingScope``.

Strava scope-string quirk:
  Strava returns the granted scopes as a *comma-delimited* string in the
  token exchange response (e.g. ``"read,activity:read_all"``), NOT
  space-delimited. ``_check_scopes`` handles this correctly.

Identity (connector-contract.md §2):
- ``source_record_id`` = Strava activity ``id`` (int → str). Provider-native.
- Stable across re-fetches (Strava IDs never change).

Sync semantics:
- ``fetch(account, since)`` uses the Strava ``after`` epoch parameter to
  filter by ``start_date`` (creation time). Strava does not expose an
  ``updated_after`` cursor, so edits to old activities will NOT appear in
  an incremental fetch. Callers should use a look-back overlap window.
- Pagination: drains all pages via ``/athlete/activities`` (per_page=200).
- Deletes: Strava does NOT expose deletions via the summary endpoint.
  Deletions are not detectable for this connector.

Mapping decisions — sport_type → ActivityType:
  Strava uses ``sport_type`` (the modern field; ``type`` is legacy/deprecated).
  Normalization table:
    Run, TrailRun, VirtualRun           → run
    Ride, GravelRide, MountainBikeRide,
      VirtualRide, EMountainBikeRide,
      Handcycle, Velomobile             → ride
    Swim                                → swim
    Walk, Hike                          → walk / hike
    WeightTraining, Crossfit,
      RockClimbing                      → strength  (ASSUMPTION: these Strava
                                          types are closest to strength;
                                          Strava is cardio-primary so treat
                                          as best-effort)
    Sauna                               → sauna
    Yoga, Pilates, Stretching,
      Meditation, MindAndBody           → cardio   (ASSUMPTION: low-intensity
                                          mindful activities mapped to cardio
                                          as the closest normalized type)
    Workout, Elliptical, StairStepper,
      HighIntensityIntervalTraining,
      Rowing, Kayaking, Canoeing,
      Skating, IceSkate, NordicSki,
      Skateboard, Snowboard,
      BackcountrySkiing, AlpineSki,
      Surfing, Windsurf, Kitesurfing,
      StandUpPaddling, Golf, …          → cardio
    Everything else                     → other
  Original ``sport_type`` is always preserved in ``activity_label``.

Energy conversion:
  Strava reports ``kilojoules`` (kJ). Conversion: kcal = kJ / 4.184.
  This is standard thermochemical conversion. ``kilojoules`` stays in raw.

raw:
  The verbatim SummaryActivity dict from the Strava API. Lap splits and GPS
  are NOT fetched by this connector (they require per-activity endpoints and
  separate rate-limit budget); they stay out of raw unless already present in
  the summary response. Callers that need laps should extend via a separate
  enrichment step.

Partial-failure:
  If one page request fails mid-stream, the connector yields collected records
  and raises ``PartialFetchError`` so the caller knows not to advance its
  watermark past the gap.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
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
from openvitals.schema.domains import ActivityType, Domain, RecordType
from openvitals.schema.records import Record, WorkoutCommon
from openvitals.storage.credentials import CredentialStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
REQUIRED_SCOPE = "activity:read_all"
PER_PAGE = 200
# Refresh if token expires within this many seconds
REFRESH_BUFFER_SECS = 3600

# ---------------------------------------------------------------------------
# sport_type normalization table
# ---------------------------------------------------------------------------

# Map Strava sport_type strings → ActivityType enum values.
# Unmapped types fall through to ActivityType.OTHER.
_SPORT_TYPE_MAP: dict[str, ActivityType] = {
    # Running
    "Run": ActivityType.RUN,
    "TrailRun": ActivityType.RUN,
    "VirtualRun": ActivityType.RUN,
    # Riding
    "Ride": ActivityType.RIDE,
    "GravelRide": ActivityType.RIDE,
    "MountainBikeRide": ActivityType.RIDE,
    "VirtualRide": ActivityType.RIDE,
    "EMountainBikeRide": ActivityType.RIDE,
    "Handcycle": ActivityType.RIDE,
    "Velomobile": ActivityType.RIDE,
    # Swimming
    "Swim": ActivityType.SWIM,
    # Walking
    "Walk": ActivityType.WALK,
    # Hiking
    "Hike": ActivityType.HIKE,
    # Strength-adjacent (ASSUMPTION: best-effort; Strava has no sets/reps data)
    "WeightTraining": ActivityType.STRENGTH,
    "Crossfit": ActivityType.STRENGTH,
    "RockClimbing": ActivityType.STRENGTH,
    # Sauna
    "Sauna": ActivityType.SAUNA,
    # Cardio / mindful (ASSUMPTION: mapped to cardio as closest normalized type)
    "Yoga": ActivityType.CARDIO,
    "Pilates": ActivityType.CARDIO,
    "Stretching": ActivityType.CARDIO,
    "Meditation": ActivityType.CARDIO,
    "MindAndBody": ActivityType.CARDIO,
    "Workout": ActivityType.CARDIO,
    "Elliptical": ActivityType.CARDIO,
    "StairStepper": ActivityType.CARDIO,
    "HighIntensityIntervalTraining": ActivityType.CARDIO,
    "Rowing": ActivityType.CARDIO,
    "Kayaking": ActivityType.CARDIO,
    "Canoeing": ActivityType.CARDIO,
    "Skating": ActivityType.CARDIO,
    "IceSkate": ActivityType.CARDIO,
    "NordicSki": ActivityType.CARDIO,
    "AlpineSki": ActivityType.CARDIO,
    "BackcountrySkiing": ActivityType.CARDIO,
    "Snowboard": ActivityType.CARDIO,
    "Skateboard": ActivityType.CARDIO,
    "Surfing": ActivityType.CARDIO,
    "Windsurf": ActivityType.CARDIO,
    "Kitesurfing": ActivityType.CARDIO,
    "StandUpPaddling": ActivityType.CARDIO,
    "Golf": ActivityType.CARDIO,
    "Soccer": ActivityType.CARDIO,
    "Tennis": ActivityType.CARDIO,
    "Badminton": ActivityType.CARDIO,
    "Volleyball": ActivityType.CARDIO,
    "Basketball": ActivityType.CARDIO,
    "Football": ActivityType.CARDIO,
    "Rugby": ActivityType.CARDIO,
    "Hockey": ActivityType.CARDIO,
    "Lacrosse": ActivityType.CARDIO,
    "Baseball": ActivityType.CARDIO,
    "Cricket": ActivityType.CARDIO,
    "Boxing": ActivityType.CARDIO,
    "MartialArts": ActivityType.CARDIO,
    "Gymnastics": ActivityType.CARDIO,
    "Dance": ActivityType.CARDIO,
    "Wheelchair": ActivityType.CARDIO,
}


def _normalize_sport_type(sport_type: str | None) -> ActivityType:
    """Map a Strava sport_type string → ActivityType enum. Unknown → OTHER."""
    if not sport_type:
        return ActivityType.OTHER
    return _SPORT_TYPE_MAP.get(sport_type, ActivityType.OTHER)


def _kj_to_kcal(kj: float | None) -> float | None:
    """Convert kilojoules → kilocalories (thermochemical). Returns None if input is None."""
    if kj is None:
        return None
    return kj / 4.184


# ---------------------------------------------------------------------------
# StravaConnector
# ---------------------------------------------------------------------------


class StravaConnector(BaseConnector):
    """
    OpenVitals connector for Strava.

    ``source = "strava"``, ``domains = ["activity"]``.

    Credential dict (stored in / loaded from ``CredentialStore``):
        access_token  : str   — OAuth bearer token
        refresh_token : str   — OAuth refresh token (rotates on use)
        expires_at    : int   — Unix epoch when access_token expires
        athlete_id    : int   — Strava athlete ID (stable user identifier)
        scope         : str   — comma-delimited granted scopes

    Construction::

        store = EncryptedFileStore()
        connector = StravaConnector(
            client_id="...",
            client_secret="...",
            credential_store=store,
        )
        account = connector.connect(store.get("strava", "<athlete_id>"))
        records = list(connector.fetch(account, since=datetime(...)))
    """

    source = "strava"
    domains = [Domain.ACTIVITY.value]

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        credential_store: CredentialStore,
        http_client: httpx.Client | None = None,
    ) -> None:
        """
        Parameters
        ----------
        client_id:
            Strava OAuth application client_id.
        client_secret:
            Strava OAuth application client_secret.
        credential_store:
            ``CredentialStore`` instance for loading/persisting tokens.
            Must already contain a valid credential dict for this account
            (seeded by the OAuth flow, not by this connector).
        http_client:
            Optional pre-configured ``httpx.Client`` (useful for testing).
            If None, a default client is constructed per ``connect()`` call.
        """
        self._client_id = client_id
        self._client_secret = client_secret
        self._store = credential_store
        self._http = http_client

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _get_http(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        return httpx.Client(timeout=30)

    def _check_scopes(self, scope_str: str | None) -> None:
        """
        Validate that ``activity:read_all`` is in the granted scopes string.

        Strava returns scopes as a *comma-delimited* string (e.g.
        ``"read,activity:read_all"``), not space-delimited.
        Raises ``MissingScope`` if required scope is absent.
        """
        if not scope_str:
            raise MissingScope(
                f"No scopes granted; required: {REQUIRED_SCOPE}",
                required_scope=REQUIRED_SCOPE,
                granted_scopes=set(),
            )
        granted = {s.strip() for s in scope_str.split(",")}
        if REQUIRED_SCOPE not in granted:
            raise MissingScope(
                f"Missing required scope '{REQUIRED_SCOPE}'; granted: {granted}",
                required_scope=REQUIRED_SCOPE,
                granted_scopes=granted,
            )

    def _refresh_token(self, credential: dict[str, Any]) -> dict[str, Any]:
        """
        Exchange the stored refresh token for a new access + refresh token pair.

        Returns the updated credential dict. Raises ``AuthRequired`` if the
        refresh_token is missing; raises ``ProviderUnavailable`` on HTTP error.

        IMPORTANT: The rotated refresh token is persisted to the CredentialStore
        by the caller (``connect()``) *before* the new access token is used.
        This matches the HealthOS pattern in ``get_strava_access_token()``.
        """
        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            raise AuthRequired(
                "No refresh_token in stored credentials; re-run OAuth flow."
            )

        http = self._get_http()
        resp = http.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code == 401:
            raise AuthRequired(
                "Strava refresh token rejected (401); re-run OAuth flow."
            )
        if resp.status_code >= 500:
            raise ProviderUnavailable(
                f"Strava token endpoint returned {resp.status_code}."
            )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailable(
                f"Strava token endpoint error: {exc}"
            ) from exc

        try:
            tokens = resp.json()
            new_credential = dict(credential)
            new_credential["access_token"] = tokens["access_token"]
            new_credential["refresh_token"] = tokens["refresh_token"]
            new_credential["expires_at"] = tokens["expires_at"]
            # Preserve scope if returned; else keep existing
            if "scope" in tokens:
                new_credential["scope"] = tokens["scope"]
            return new_credential
        except (KeyError, ValueError) as exc:
            raise ProviderPayloadError(
                f"Unexpected shape in Strava token response: {exc}"
            ) from exc

    # -----------------------------------------------------------------------
    # BaseConnector interface
    # -----------------------------------------------------------------------

    def connect(self, credentials: dict[str, Any]) -> Account:
        """
        Validate/refresh credentials and return an Account.

        ``credentials`` must be a dict with at least:
            - ``refresh_token`` (str)
            - ``athlete_id`` (int)
            - ``expires_at`` (int, Unix epoch)
            - ``scope`` (str, comma-delimited)
            - ``access_token`` (str, may be stale — will be refreshed if needed)

        Scopes are validated immediately. Missing ``activity:read_all`` raises
        ``MissingScope``.

        Token refresh: if the access token expires within ``REFRESH_BUFFER_SECS``
        (1 hour), the token is refreshed and the NEW credential dict is persisted
        to the CredentialStore BEFORE the Account is returned.

        Returns ``Account`` with ``account_ref = str(athlete_id)``.
        Raises ``AuthRequired``, ``MissingScope``.
        """
        if not credentials:
            raise AuthRequired(
                "No credentials provided; run the Strava OAuth flow first."
            )

        athlete_id = credentials.get("athlete_id")
        if not athlete_id:
            raise AuthRequired(
                "Credential dict missing 'athlete_id'; re-run OAuth flow."
            )

        account_ref = str(athlete_id)

        # Check if we need to refresh the access token
        expires_at = credentials.get("expires_at", 0)
        if time.time() + REFRESH_BUFFER_SECS >= expires_at:
            credentials = self._refresh_token(credentials)
            # Persist rotated tokens BEFORE returning — if we crash here, the
            # old refresh token is already invalidated on Strava's side.
            self._store.set(self.source, account_ref, credentials)

        # Validate scopes (after refresh, scope may be updated in new cred)
        self._check_scopes(credentials.get("scope"))

        return Account(
            account_ref=account_ref,
            source=self.source,
            metadata={
                "athlete_id": athlete_id,
                "scope": credentials.get("scope"),
                "expires_at": credentials.get("expires_at"),
                # NEVER store raw tokens in metadata
            },
        )

    def fetch(
        self,
        account: Account,
        since: datetime | None = None,
    ) -> Iterable[Record]:
        """
        Yield ``workout`` Records for all Strava activities for this account.

        ``since`` — if provided, filter activities with ``start_date`` at or
        after this UTC datetime (converted to Unix epoch → Strava ``after``
        param). Strava filters by ``start_date`` (creation time), not updated
        time — callers should use a look-back overlap window to catch edits.

        Pagination: drains all pages via ``/athlete/activities`` (per_page=200).

        Raises ``AuthRequired``, ``RateLimited``, ``ProviderUnavailable``,
        ``ProviderPayloadError``, ``PartialFetchError``.
        """
        # Load fresh credential to get the current access token
        credential = self._store.get(self.source, account.account_ref)
        if not credential:
            raise AuthRequired(
                f"No credential in store for strava:{account.account_ref}; "
                "re-run OAuth flow."
            )

        # Re-validate (may refresh again if token expired while fetch was queued)
        # We re-call connect() to ensure token is current
        _ = self.connect(credential)
        # Reload after possible refresh
        credential = self._store.get(self.source, account.account_ref)
        access_token = credential["access_token"]  # type: ignore[index]

        headers = {"Authorization": f"Bearer {access_token}"}
        after_epoch: int | None = None
        if since is not None:
            # Convert UTC datetime → Unix epoch for Strava ``after`` param
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            after_epoch = int(since.timestamp())

        page = 1
        collected: list[Record] = []
        ingested_at = datetime.now(timezone.utc)
        http = self._get_http()

        while True:
            params: dict[str, Any] = {"per_page": PER_PAGE, "page": page}
            if after_epoch is not None:
                params["after"] = after_epoch

            try:
                resp = http.get(
                    f"{STRAVA_API_BASE}/athlete/activities",
                    headers=headers,
                    params=params,
                )
            except httpx.RequestError as exc:
                if collected:
                    raise PartialFetchError(
                        f"Network error on page {page}: {exc}",
                        records_fetched=len(collected),
                        cause=exc,
                    ) from exc
                raise ProviderUnavailable(
                    f"Network error fetching Strava activities: {exc}"
                ) from exc

            if resp.status_code == 401:
                raise AuthRequired(
                    "Strava returned 401; access token rejected. Re-run connect()."
                )
            if resp.status_code == 429:
                retry_after = None
                try:
                    retry_after = float(resp.headers.get("Retry-After", 0)) or None
                except (TypeError, ValueError):
                    pass
                if collected:
                    raise PartialFetchError(
                        f"Rate limited on page {page}.",
                        records_fetched=len(collected),
                        cause=RateLimited("Strava rate limit hit.", retry_after=retry_after),
                    )
                raise RateLimited(
                    "Strava rate limit hit during activities fetch.",
                    retry_after=retry_after,
                )
            if resp.status_code >= 500:
                exc_5xx = ProviderUnavailable(
                    f"Strava returned {resp.status_code} on page {page}."
                )
                if collected:
                    raise PartialFetchError(
                        f"Provider error on page {page}.",
                        records_fetched=len(collected),
                        cause=exc_5xx,
                    )
                raise exc_5xx

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                pfail = PartialFetchError(
                    f"HTTP error on page {page}: {exc}",
                    records_fetched=len(collected),
                    cause=exc,
                ) if collected else None
                if pfail:
                    raise pfail from exc
                raise ProviderUnavailable(str(exc)) from exc

            try:
                batch: list[dict] = resp.json()
            except Exception as exc:
                raise ProviderPayloadError(
                    f"Failed to parse Strava response as JSON on page {page}: {exc}"
                ) from exc

            if not isinstance(batch, list):
                raise ProviderPayloadError(
                    f"Expected list from Strava activities endpoint, got {type(batch).__name__}."
                )

            if not batch:
                # No more pages
                break

            for raw_activity in batch:
                try:
                    record = self._map_activity(raw_activity, account.account_ref, ingested_at)
                except ProviderPayloadError:
                    raise
                except Exception as exc:
                    activity_id = raw_activity.get("id", "unknown")
                    raise ProviderPayloadError(
                        f"Failed to map Strava activity id={activity_id}: {exc}"
                    ) from exc

                collected.append(record)
                yield record

            page += 1

    # -----------------------------------------------------------------------
    # Activity mapping
    # -----------------------------------------------------------------------

    def _map_activity(
        self,
        a: dict[str, Any],
        account_ref: str,
        ingested_at: datetime,
    ) -> Record:
        """
        Map a single Strava SummaryActivity dict → OpenVitals Record.

        Raises ``ProviderPayloadError`` if required fields (``id``, ``start_date``)
        are absent — explicit failure, never silent empty.
        """
        activity_id = a.get("id")
        if activity_id is None:
            raise ProviderPayloadError(
                "Strava activity missing required field 'id'."
            )

        start_date_str = a.get("start_date")
        if not start_date_str:
            raise ProviderPayloadError(
                f"Strava activity id={activity_id} missing required field 'start_date'."
            )

        # Parse occurred_at (UTC)
        try:
            occurred_at = datetime.fromisoformat(
                start_date_str.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ProviderPayloadError(
                f"Strava activity id={activity_id} has unparseable start_date "
                f"'{start_date_str}': {exc}"
            ) from exc

        # Parse occurred_end from elapsed_time (seconds)
        elapsed_s = a.get("elapsed_time")
        occurred_end: datetime | None = None
        if elapsed_s is not None:
            try:
                from datetime import timedelta
                occurred_end = occurred_at + timedelta(seconds=int(elapsed_s))
            except (TypeError, ValueError):
                occurred_end = None

        # Timezone: Strava provides a string like "(GMT+01:00) Europe/London"
        # We want only the IANA part after the space, if present.
        tz_raw = a.get("timezone")
        iana_timezone: str | None = None
        if tz_raw:
            # e.g. "(GMT+01:00) Europe/London" → "Europe/London"
            parts = tz_raw.split(" ", 1)
            if len(parts) == 2 and "/" in parts[1]:
                iana_timezone = parts[1]
            elif "/" in tz_raw:
                # Already IANA-style (no GMT prefix)
                iana_timezone = tz_raw

        # sport_type normalization
        sport_type = a.get("sport_type") or a.get("type")
        activity_type = _normalize_sport_type(sport_type)

        # Duration: moving_time is "active/moving time" (seconds)
        moving_time_s = a.get("moving_time")
        duration_minutes: float | None = None
        if moving_time_s is not None:
            try:
                duration_minutes = int(moving_time_s) / 60.0
            except (TypeError, ValueError):
                duration_minutes = None

        elapsed_minutes: float | None = None
        if elapsed_s is not None:
            try:
                elapsed_minutes = int(elapsed_s) / 60.0
            except (TypeError, ValueError):
                elapsed_minutes = None

        # Energy: Strava gives kilojoules → convert to kcal
        kj = a.get("kilojoules")
        energy_kcal = _kj_to_kcal(kj)

        # avg_heart_rate_bpm: Strava returns as float; schema is int | None
        avg_hr_raw = a.get("average_heartrate")
        avg_hr: int | None = None
        if avg_hr_raw is not None:
            try:
                avg_hr = int(round(float(avg_hr_raw)))
            except (TypeError, ValueError):
                avg_hr = None

        max_hr_raw = a.get("max_heartrate")
        max_hr: int | None = None
        if max_hr_raw is not None:
            try:
                max_hr = int(round(float(max_hr_raw)))
            except (TypeError, ValueError):
                max_hr = None

        common = WorkoutCommon(
            activity_type=activity_type,
            activity_label=sport_type,
            duration_minutes=duration_minutes,
            elapsed_minutes=elapsed_minutes,
            distance_meters=a.get("distance"),
            elevation_gain_meters=a.get("total_elevation_gain"),
            avg_heart_rate_bpm=avg_hr,
            max_heart_rate_bpm=max_hr,
            energy_kcal=energy_kcal,
            avg_speed_mps=a.get("average_speed"),
            avg_power_watts=a.get("average_watts"),
            # Strava is cardio — no strength fields (total_volume_kg, set_count, exercises)
        )

        # local_date: Strava provides start_date_local (local-timezone wall-clock time).
        # Prefer that over the UTC start_date so cross-midnight sessions attribute
        # to the correct local day (e.g. 23:00 local doesn't become the next UTC date).
        local_date: date | None = None
        start_date_local = a.get("start_date_local")
        if start_date_local:
            try:
                local_date = date.fromisoformat(start_date_local[:10])
            except (ValueError, TypeError):
                local_date = occurred_at.date()  # fallback to UTC date
        else:
            local_date = occurred_at.date()

        return Record(
            source=self.source,
            domain=Domain.ACTIVITY.value,
            record_type=RecordType.WORKOUT,
            source_record_id=str(activity_id),
            account_ref=account_ref,
            occurred_at=occurred_at,
            occurred_end=occurred_end,
            ingested_at=ingested_at,
            timezone=iana_timezone,
            local_date=local_date,
            common=common,
            raw=a,  # verbatim, byte-faithful
            source_extras={
                # Provider-native headline values that don't generalize
                "strava_activity_name": a.get("name"),
                "kilojoules": kj,
                "suffer_score": a.get("suffer_score"),
                "gear_id": a.get("gear_id"),
                "has_heartrate": a.get("has_heartrate"),
                "average_cadence": a.get("average_cadence"),
                "weighted_average_watts": a.get("weighted_average_watts"),
                "device_watts": a.get("device_watts"),
                "max_speed_mps": a.get("max_speed"),
            },
        )
