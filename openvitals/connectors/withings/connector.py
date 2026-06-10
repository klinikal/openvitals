"""
WithingsConnector — OpenVitals connector for Withings body measurements.

Emits three record types (all domain ``body``):
- ``body_measurement`` — weight, fat %, lean/muscle/bone/water/visceral
- ``segmental_composition`` — per-limb fat %  + muscle mass (left/right arm/leg, trunk)
- ``cardiovascular_marker`` — one record per marker (vascular_age, pulse_wave_velocity,
  ecg_result, nerve_health_score)

Withings API notes (verified against live API and official docs):
- Auth endpoint: ``https://wbsapi.withings.net/v2/oauth2``  — non-standard OAuth2:
  token requests require ``action=requesttoken`` in the POST body.
- Auth code expires in **30 seconds** — callers must exchange it promptly.
- Measure endpoint: ``https://wbsapi.withings.net/measure`` with ``action=getmeas``.
- Status 0 = success; status 181 = no data.
- Refresh tokens rotate on every use. New token is persisted to ``CredentialStore``
  BEFORE any API call that uses it, so a crash cannot orphan the refresh token.
- Measurement value encoding: ``real_value = value × 10^unit`` (unit is a signed int,
  typically negative). Decoding lives in ``common`` only — ``raw`` retains verbatim.
- Required scopes: ``user.info,user.metrics``.

Measurement type codes (verified against live API):
  1   = weight (kg)
  5   = fat-free mass / lean mass (kg)
  6   = fat ratio / body fat %
  8   = fat mass (kg)
  76  = muscle mass (kg)
  77  = hydration (kg)  — used as body_water_pct proxy (stored as hydration_kg; no pct)
  88  = bone mass (kg)
  170 = visceral fat index (unitless)
  174 = segmental fat mass per limb (kg)
  175 = segmental muscle mass per limb (kg)
  91  = pulse wave velocity (m/s)
  130 = ECG afib result (0=normal, 1=afib, 2=irregular)
  155 = vascular age (years)
  167 = nerve health score
  196 = additional cardiovascular marker (deferred; mapped to raw only if present)

Segmental position codes (``fm`` field on the measure object):
  1 = left_arm, 2 = right_arm, 3 = trunk, 4 = left_leg, 5 = right_leg
  ASSUMPTION: inferred from open-source Withings clients and community docs.
  Verify with real Body Scan hardware if results look wrong.

Identity:
- ``body_measurement``: Withings native measure group id (``grp["grpid"]``).
- ``cardiovascular_marker``: ``f"{grpid}:{marker_type}"``
- ``segmental_composition``: ``f"{grpid}:segmental_composition"``

Auth credential dict keys (from/to CredentialStore):
  ``access_token``, ``refresh_token``, ``expires_at`` (ISO-8601 UTC str),
  ``withings_user_id`` (str — used as account_ref).

Never log token values.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from openvitals.connectors.base import Account, BaseConnector
from openvitals.errors import (
    AuthRequired,
    MissingScope,
    ProviderPayloadError,
    ProviderUnavailable,
    RateLimited,
)
from openvitals.schema.domains import (
    BodySegment,
    CardiovascularMarkerType,
    RecordType,
)
from openvitals.schema.records import (
    BodyMeasurementCommon,
    BodySegmentComposition,
    CardiovascularMarkerCommon,
    Record,
    SegmentalCompositionCommon,
)
from openvitals.storage.credentials import CredentialStore

# ---------------------------------------------------------------------------
# Withings API endpoints
# ---------------------------------------------------------------------------

WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"
WITHINGS_AUTH_URL = "https://account.withings.com/oauth2_user/authorize2"

REQUIRED_SCOPES = {"user.info", "user.metrics"}

# ---------------------------------------------------------------------------
# Measurement type code maps
# ---------------------------------------------------------------------------

# Body composition: {type_code: common_field_name}
# Type 77 is hydration in kg — we store as hydration_kg in source_extras because
# BodyMeasurementCommon has body_water_pct (a %) not hydration_kg. The connector
# cannot compute the % without weight, so it is mapped to source_extras instead.
BODY_COMP_TYPES: dict[int, str] = {
    1: "weight_kg",
    5: "lean_mass_kg",
    6: "body_fat_pct",
    8: "fat_mass_kg",
    76: "muscle_mass_kg",
    88: "bone_mass_kg",
    170: "visceral_fat_index",
}

# body_water_pct: type 77 is hydration in kg — requires weight to compute pct.
# Withings Body Scan also provides type 77 as absolute hydration kg, not %.
# We leave body_water_pct null in common and store hydration_kg in source_extras.
HYDRATION_TYPE = 77

# Segmental types
SEG_FAT_TYPE = 174      # fat mass per limb (kg)
SEG_MUSCLE_TYPE = 175   # muscle mass per limb (kg)
MEASTYPES_SEGMENTAL = {SEG_FAT_TYPE, SEG_MUSCLE_TYPE}

# Cardiovascular: {type_code: (marker_type_enum, unit)}
# Type 130: ECG afib (0=normal_sinus_rhythm, 1=afib, 2=irregular) → categorical str
# Type 196: present in some Body Scan models but not mapped to a named marker; skipped
CARDIO_TYPES: dict[int, tuple[CardiovascularMarkerType, str | None]] = {
    91: (CardiovascularMarkerType.PULSE_WAVE_VELOCITY, "mps"),
    130: (CardiovascularMarkerType.ECG_RESULT, None),   # categorical
    155: (CardiovascularMarkerType.VASCULAR_AGE, "years"),
    167: (CardiovascularMarkerType.NERVE_HEALTH_SCORE, None),
}

# ECG result integer → canonical string
ECG_RESULT_MAP: dict[int, str] = {
    0: "normal_sinus_rhythm",
    1: "atrial_fibrillation",
    2: "irregular_rhythm",
}

# Segment position code (fm field) → BodySegment enum
SEGMENT_POSITIONS: dict[int, BodySegment] = {
    1: BodySegment.LEFT_ARM,
    2: BodySegment.RIGHT_ARM,
    3: BodySegment.TRUNK,
    4: BodySegment.LEFT_LEG,
    5: BodySegment.RIGHT_LEG,
}

ALL_MEASTYPES = ",".join(
    str(t)
    for t in sorted(
        set(BODY_COMP_TYPES.keys())
        | {HYDRATION_TYPE}
        | MEASTYPES_SEGMENTAL
        | set(CARDIO_TYPES.keys())
    )
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Value decode
# ---------------------------------------------------------------------------


def decode_value(value: int, unit: int) -> float:
    """Decode Withings encoded measure: real = value × 10^unit."""
    return value * (10 ** unit)


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class WithingsConnector(BaseConnector):
    """
    OpenVitals connector for Withings.

    Emits: ``body_measurement``, ``segmental_composition``,
    ``cardiovascular_marker`` (all domain ``body``).

    Instantiate with a ``CredentialStore`` (required) and optionally
    ``client_id`` / ``client_secret`` (read from env vars
    ``WITHINGS_CLIENT_ID`` / ``WITHINGS_CLIENT_SECRET`` if not provided).

    Usage::

        store = EncryptedFileStore()
        connector = WithingsConnector(credential_store=store)
        account = connector.connect(store.get("withings", "user_123"))
        records = list(connector.fetch(account, since=datetime(..., tzinfo=UTC)))
    """

    source = "withings"
    domains = ["body"]

    def __init__(
        self,
        credential_store: CredentialStore,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._store = credential_store
        self._client_id = client_id or os.environ.get("WITHINGS_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("WITHINGS_CLIENT_SECRET", "")

    # -------------------------------------------------------------------------
    # Auth lifecycle
    # -------------------------------------------------------------------------

    def connect(self, credentials: dict[str, Any]) -> Account:
        """
        Validate credentials, refresh access token if expired.

        ``credentials`` must contain:
          ``access_token``, ``refresh_token``, ``expires_at`` (ISO-8601 UTC str),
          ``withings_user_id`` (str).

        Raises ``AuthRequired`` if credentials are missing/invalid.
        Raises ``MissingScope`` if granted scopes don't include required scopes.
        Persists rotated tokens immediately on refresh.
        """
        if not credentials:
            raise AuthRequired(
                "No Withings credentials found. Run the OAuth flow first."
            )

        required_keys = {"access_token", "refresh_token", "withings_user_id"}
        missing = required_keys - set(credentials.keys())
        if missing:
            raise AuthRequired(
                f"Withings credentials missing keys: {missing}"
            )

        # Validate scopes — fail loud if absent or insufficient
        granted_raw = credentials.get("scope_granted", "")
        if not granted_raw:
            raise MissingScope(
                "Withings credentials are missing 'scope_granted'. "
                "Re-authenticate to capture granted scopes.",
                required_scope=",".join(REQUIRED_SCOPES),
                granted_scopes=set(),
            )
        granted = set(granted_raw.replace(",", " ").split())
        missing_scopes = REQUIRED_SCOPES - granted
        if missing_scopes:
            raise MissingScope(
                f"Withings missing required scopes: {missing_scopes}",
                required_scope=",".join(missing_scopes),
                granted_scopes=granted,
            )

        withings_user_id = str(credentials["withings_user_id"])

        # Refresh if token expired (or will expire within 5 minutes)
        expires_at_raw = credentials.get("expires_at")
        needs_refresh = True
        if expires_at_raw:
            try:
                expires_dt = datetime.fromisoformat(
                    expires_at_raw.replace("Z", "+00:00")
                )
                needs_refresh = (
                    expires_dt - datetime.now(UTC)
                ).total_seconds() < 300
            except (ValueError, TypeError):
                needs_refresh = True

        if needs_refresh:
            credentials = self._refresh_token(credentials, withings_user_id)
        # Always persist after connect — fetch() reads credentials from the store.
        self._store.set(self.source, withings_user_id, credentials)

        return Account(
            account_ref=withings_user_id,
            source=self.source,
            metadata={
                "scope_granted": credentials.get("scope_granted", ""),
            },
        )

    def _refresh_token(
        self, credentials: dict[str, Any], withings_user_id: str
    ) -> dict[str, Any]:
        """
        Exchange refresh token for new tokens.  Persists BEFORE returning.
        Raises ``AuthRequired`` if the refresh fails (expired or revoked token).
        Never logs token values.
        """
        try:
            resp = httpx.post(
                WITHINGS_TOKEN_URL,
                data={
                    "action": "requesttoken",
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": credentials["refresh_token"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable("Withings token refresh timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"Withings token refresh HTTP error: {exc}"
            ) from exc

        if resp.status_code != 200:
            raise ProviderUnavailable(
                f"Withings token refresh returned HTTP {resp.status_code}."
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(
                "Withings token refresh returned non-JSON response."
            ) from exc

        status = payload.get("status")
        if status in (100, 101, 102):
            # Invalid/expired token codes
            raise AuthRequired(
                f"Withings refresh token rejected (status {status}). "
                "Re-run the OAuth flow."
            )
        if status != 0:
            raise ProviderPayloadError(
                f"Withings token refresh unexpected status: {status}"
            )

        tokens = payload["body"]
        expires_in = tokens.get("expires_in", 10800)
        new_expires_at = (
            datetime.now(UTC) + timedelta(seconds=expires_in)
        ).isoformat()

        new_creds = {
            **credentials,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expires_at": new_expires_at,
        }

        # Persist BEFORE returning — never log values
        self._store.set(self.source, withings_user_id, new_creds)

        return new_creds

    # -------------------------------------------------------------------------
    # Fetch
    # -------------------------------------------------------------------------

    def fetch(
        self,
        account: Account,
        since: datetime | None = None,
    ) -> Iterable[Record]:
        """
        Yield Records for the account.  Drains all pages.

        ``since``: fetch everything at or after this UTC instant.
        If None, fetches all available history.

        Emits: body_measurement, segmental_composition, cardiovascular_marker.

        Raises: ``AuthRequired``, ``RateLimited``, ``ProviderUnavailable``,
        ``ProviderPayloadError``.
        """
        credentials = self._store.get(self.source, account.account_ref)
        if not credentials:
            raise AuthRequired(
                f"No credentials for Withings account '{account.account_ref}'."
            )

        access_token = credentials.get("access_token")
        if not access_token:
            raise AuthRequired("Withings access token missing.")

        startdate = int(since.timestamp()) if since else 0
        enddate = int(datetime.now(UTC).timestamp())
        ingested_at = datetime.now(UTC)

        measuregrps = self._fetch_measuregrps(access_token, startdate, enddate)

        # Route each group to the right record builder(s).
        # One group can contribute to multiple record types.
        for grp in measuregrps:
            grp_types = {m.get("type") for m in grp.get("measures", [])}
            grpid = str(grp.get("grpid", ""))
            if not grpid:
                raise ProviderPayloadError(
                    f"Withings measuregrp missing 'grpid': {grp}"
                )

            occurred_at = datetime.fromtimestamp(grp["date"], tz=UTC)
            local_date = occurred_at.date()

            body_types = set(BODY_COMP_TYPES.keys()) | {HYDRATION_TYPE}
            if grp_types & body_types:
                rec = self._build_body_measurement(
                    grp, grpid, occurred_at, local_date, ingested_at, account
                )
                if rec is not None:
                    yield rec

            if grp_types & set(CARDIO_TYPES.keys()):
                yield from self._build_cardiovascular_markers(
                    grp, grpid, occurred_at, local_date, ingested_at, account
                )

            if grp_types & MEASTYPES_SEGMENTAL:
                rec = self._build_segmental_composition(
                    grp, grpid, occurred_at, local_date, ingested_at, account
                )
                if rec is not None:
                    yield rec

    def _fetch_measuregrps(
        self, access_token: str, startdate: int, enddate: int
    ) -> list[dict]:
        """
        Call Withings getmeas, handle status codes, return all measuregrps.
        Withings returns all pages in one call (no cursor pagination for getmeas).
        """
        try:
            resp = httpx.post(
                WITHINGS_MEASURE_URL,
                data={
                    "action": "getmeas",
                    "startdate": startdate,
                    "enddate": enddate,
                    "meastypes": ALL_MEASTYPES,
                    "category": "1",
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=60,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable("Withings getmeas timed out.") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Withings getmeas HTTP error: {exc}") from exc

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 60))
            raise RateLimited(
                "Withings rate limit hit.", retry_after=retry_after
            )
        if resp.status_code >= 500:
            raise ProviderUnavailable(
                f"Withings returned HTTP {resp.status_code}."
            )
        if resp.status_code == 401:
            raise AuthRequired("Withings access token rejected (401).")
        if resp.status_code != 200:
            raise ProviderPayloadError(
                f"Withings getmeas unexpected HTTP {resp.status_code}."
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise ProviderPayloadError(
                "Withings getmeas returned non-JSON response."
            ) from exc

        status = payload.get("status")
        if status == 181:
            return []  # no data — normal result
        if status in (100, 101, 102):
            raise AuthRequired(
                f"Withings access token rejected by getmeas (status {status})."
            )
        if status != 0:
            raise ProviderPayloadError(
                f"Withings getmeas error status {status}: {payload}"
            )

        raw_grps = payload.get("body", {}).get("measuregrps")
        if raw_grps is None:
            raise ProviderPayloadError(
                "Withings getmeas response missing 'body.measuregrps'."
            )

        return raw_grps

    # -------------------------------------------------------------------------
    # Record builders
    # -------------------------------------------------------------------------

    def _build_body_measurement(
        self,
        grp: dict,
        grpid: str,
        occurred_at: datetime,
        local_date: Any,
        ingested_at: datetime,
        account: Account,
    ) -> Record | None:
        """Build a body_measurement Record from a measuregrp."""
        measures = grp.get("measures", [])
        common_kwargs: dict[str, Any] = {}
        hydration_kg: float | None = None

        for m in measures:
            t = m.get("type")
            if t in BODY_COMP_TYPES:
                field = BODY_COMP_TYPES[t]
                val = decode_value(m["value"], m["unit"])
                if t == 170:
                    # visceral fat index is an integer scale
                    common_kwargs[field] = int(val)
                else:
                    common_kwargs[field] = val
            elif t == HYDRATION_TYPE:
                hydration_kg = decode_value(m["value"], m["unit"])

        if not common_kwargs and hydration_kg is None:
            return None

        source_extras: dict[str, Any] = {}
        if hydration_kg is not None:
            # hydration_kg is always kept in source_extras (absolute kg value)
            source_extras["hydration_kg"] = hydration_kg
            # body_water_pct: computable when weight is in the same measure group
            # (same-event decode — not a cross-source computation).
            weight_kg = common_kwargs.get("weight_kg")
            if weight_kg is not None and weight_kg > 0:
                common_kwargs["body_water_pct"] = hydration_kg / weight_kg * 100

        return Record(
            source=self.source,
            domain="body",
            record_type=RecordType.BODY_MEASUREMENT,
            source_record_id=grpid,
            account_ref=account.account_ref,
            occurred_at=occurred_at,
            ingested_at=ingested_at,
            local_date=local_date,
            common=BodyMeasurementCommon(**common_kwargs),
            raw=grp,
            source_extras=source_extras if source_extras else None,
        )

    def _build_cardiovascular_markers(
        self,
        grp: dict,
        grpid: str,
        occurred_at: datetime,
        local_date: Any,
        ingested_at: datetime,
        account: Account,
    ) -> Iterable[Record]:
        """
        Yield one cardiovascular_marker Record per marker found in the group.
        Identity: ``f"{grpid}:{marker_type}"``
        """
        measures = grp.get("measures", [])
        for m in measures:
            t = m.get("type")
            if t not in CARDIO_TYPES:
                continue

            marker_type_enum, unit = CARDIO_TYPES[t]
            decoded_val = decode_value(m["value"], m["unit"])

            # ECG result is categorical — map integer to string
            if marker_type_enum == CardiovascularMarkerType.ECG_RESULT:
                value: float | str = ECG_RESULT_MAP.get(
                    int(decoded_val),
                    f"unknown_{int(decoded_val)}",
                )
            elif marker_type_enum == CardiovascularMarkerType.VASCULAR_AGE:
                value = int(decoded_val)
            else:
                value = decoded_val

            marker_type_str = marker_type_enum.value
            source_record_id = f"{grpid}:{marker_type_str}"

            yield Record(
                source=self.source,
                domain="body",
                record_type=RecordType.CARDIOVASCULAR_MARKER,
                source_record_id=source_record_id,
                account_ref=account.account_ref,
                occurred_at=occurred_at,
                ingested_at=ingested_at,
                local_date=local_date,
                common=CardiovascularMarkerCommon(
                    marker_type=marker_type_enum,
                    value=value,
                    unit=unit,
                ),
                raw=grp,
            )

    def _build_segmental_composition(
        self,
        grp: dict,
        grpid: str,
        occurred_at: datetime,
        local_date: Any,
        ingested_at: datetime,
        account: Account,
    ) -> Record | None:
        """
        Build a segmental_composition Record from a single measuregrp.

        fat_pct per segment = fat_mass / (fat_mass + muscle_mass) * 100
        muscle_mass_kg per segment = decoded type 175 value

        Identity: ``f"{grpid}:segmental_composition"``
        """
        seg_fat: dict[BodySegment, float] = {}
        seg_muscle: dict[BodySegment, float] = {}

        measures = grp.get("measures", [])
        for m in measures:
            t = m.get("type")
            pos_code = m.get("fm")
            seg = SEGMENT_POSITIONS.get(pos_code)
            if seg is None:
                continue
            val = decode_value(m["value"], m["unit"])
            if t == SEG_FAT_TYPE:
                seg_fat[seg] = val
            elif t == SEG_MUSCLE_TYPE:
                seg_muscle[seg] = val

        if not seg_fat and not seg_muscle:
            return None

        all_segs = set(seg_fat.keys()) | set(seg_muscle.keys())
        segments = []
        for seg in all_segs:
            fat = seg_fat.get(seg)
            muscle = seg_muscle.get(seg)
            if fat is not None:
                total = fat + (muscle or 0.0)
                fat_pct = round(fat / total * 100, 2) if total > 0 else None
            else:
                fat_pct = None
            segments.append(
                BodySegmentComposition(
                    segment=seg,
                    fat_pct=fat_pct,
                    muscle_mass_kg=muscle,
                )
            )

        # Sort for deterministic output (canonical segment order)
        _order = [
            BodySegment.LEFT_ARM,
            BodySegment.RIGHT_ARM,
            BodySegment.TRUNK,
            BodySegment.LEFT_LEG,
            BodySegment.RIGHT_LEG,
        ]
        segments.sort(key=lambda s: _order.index(s.segment))

        return Record(
            source=self.source,
            domain="body",
            record_type=RecordType.SEGMENTAL_COMPOSITION,
            source_record_id=f"{grpid}:segmental_composition",
            account_ref=account.account_ref,
            occurred_at=occurred_at,
            ingested_at=ingested_at,
            local_date=local_date,
            common=SegmentalCompositionCommon(segments=segments),
            raw=grp,
        )
