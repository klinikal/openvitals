"""
HevyConnector — OpenVitals connector for the Hevy fitness tracking API.

Auth: Static API key (no OAuth). Hevy uses ``api-key`` request header.
Scopes: N/A — all access is governed by the key itself.
API base: https://api.hevyapp.com
Verified against: Hevy API docs + live API (2026-06-07).

Emits:
  - ``activity / workout``         — from GET /v1/workouts/events (incremental)
  - ``body / body_measurement``    — from GET /v1/body_measurements (full paginated)

Identity (connector-contract.md §2):
  - workout:           Hevy workout UUID (provider-native) — stable across re-syncs.
  - body_measurement:  Compound ``account_ref:local_date:body_measurement`` — Hevy
                       body measurements are keyed by date only (no native ID in the
                       list endpoint). This matches the daily-aggregate identity rule.

Pagination: Hevy uses 1-based ``page`` / ``page_count`` pagination.
  - Workout events:    pageSize max 10 (API limit, verified against live API).
  - Body measurements: pageSize max 10 default (no documented max on list endpoint;
                       using 10 to stay conservative — same as workouts).
  - A 1-second throttle is applied between pages (conservative; Hevy has no published limit).

Rate limits: Hevy does not publish an explicit rate limit. We apply 1 req/sec
  between pages conservatively.

Late updates: The events endpoint (``/v1/workouts/events?since=``) surfaces both
  ``updated`` and ``deleted`` events. ``deleted`` events are signalled via
  ``source_extras.deleted = True`` so the caller can handle them. Body measurements
  have no events endpoint — a full pass is done each sync; callers upsert on dedup key.

Deletions: surfaced for workouts via ``source_extras.deleted = True`` on deleted events.
  Body measurement deletions are not detectable (no events endpoint).

Never log: the API key. It is retrieved from CredentialStore only — never printed,
  returned in errors, or included in source_extras.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Iterable

log = logging.getLogger(__name__)

import httpx

from openvitals.connectors.base import Account, BaseConnector
from openvitals.errors import (
    AuthRequired,
    PartialFetchError,
    ProviderPayloadError,
    ProviderUnavailable,
    RateLimited,
    WriteDisabled,
)
from openvitals.schema.domains import (
    ActivityType,
    Domain,
    RecordType,
    SetType,
)
from openvitals.schema.records import (
    BodyMeasurementCommon,
    Exercise,
    ExerciseSet,
    Record,
    WorkoutCommon,
)
from openvitals.storage.credentials import CredentialStore

# ---------------------------------------------------------------------------
# Constants (verified against live API)
# ---------------------------------------------------------------------------

HEVY_API_BASE = "https://api.hevyapp.com"
PAGE_SIZE = 10          # max allowed by Hevy API for workout endpoints (verified)
THROTTLE_SEC = 1.0      # conservative: 1 req/sec between pages
REQUEST_TIMEOUT = 30    # seconds

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Set type mapping  (Hevy API ``type`` → OpenVitals SetType)
# ---------------------------------------------------------------------------

_SET_TYPE_MAP: dict[str, SetType] = {
    "warmup": SetType.WARMUP,
    "normal": SetType.NORMAL,
    "dropset": SetType.DROPSET,
    "failure": SetType.FAILURE,
}


def _map_set_type(hevy_type: str | None) -> SetType:
    """Map Hevy set type string to SetType enum. Unknown values default to NORMAL with a warning."""
    if hevy_type is None:
        return SetType.NORMAL
    mapped = _SET_TYPE_MAP.get(hevy_type.lower())
    if mapped is None:
        log.warning(
            "Hevy: unrecognised set type %r — defaulting to NORMAL. "
            "Original value is preserved in raw.exercises[].sets[].type.",
            hevy_type,
        )
        return SetType.NORMAL
    return mapped


# ---------------------------------------------------------------------------
# Mapping helpers
# ---------------------------------------------------------------------------

def _map_exercises(raw_exercises: list[dict[str, Any]]) -> list[Exercise]:
    """
    Map Hevy exercises array → OpenVitals Exercise list.

    Ordering: Hevy uses ``index`` (0-based) to express exercise order within a
    workout, and ``index`` within each exercise's ``sets`` array for set order.
    We sort explicitly on ``index`` before building, then assign ``exercise_order``
    and ``set_order`` from those sorted indices.  This preserves order even if the
    API ever returns items out of order.
    """
    sorted_exercises = sorted(raw_exercises, key=lambda ex: ex.get("index", 0))

    exercises: list[Exercise] = []
    for ex in sorted_exercises:
        raw_sets = ex.get("sets", [])
        sorted_sets = sorted(raw_sets, key=lambda s: s.get("index", 0))

        exercise_sets = [
            ExerciseSet(
                set_order=s.get("index", i),
                set_ref_id=None,            # Hevy set list payloads have no per-set UUID
                set_type=_map_set_type(s.get("type")),
                weight_kg=s.get("weight_kg"),
                reps=s.get("reps"),
                rpe=s.get("rpe"),
                distance_meters=s.get("distance_meters"),
                duration_seconds=s.get("duration_seconds"),
            )
            for i, s in enumerate(sorted_sets)
        ]

        exercises.append(
            Exercise(
                exercise_order=ex.get("index", 0),
                exercise_name=ex.get("title"),
                exercise_ref_id=ex.get("exercise_template_id") or None,
                primary_muscle=None,   # Hevy does not return muscle group in workout payload
                superset_id=str(ex["supersets_id"]) if ex.get("supersets_id") is not None else None,
                notes=ex.get("notes") or None,
                sets=exercise_sets,
            )
        )

    return exercises


def _compute_volume(raw_exercises: list[dict[str, Any]]) -> tuple[float | None, int | None]:
    """
    Return (total_volume_kg, set_count).

    total_volume_kg = Σ(weight_kg × reps) across all sets where both are non-null.
    set_count = total number of sets across all exercises.
    """
    total_vol = 0.0
    has_any_vol = False
    total_sets = 0

    for ex in raw_exercises:
        for s in ex.get("sets", []):
            total_sets += 1
            w = s.get("weight_kg")
            r = s.get("reps")
            if w is not None and r is not None:
                total_vol += w * r
                has_any_vol = True

    return (round(total_vol, 2) if has_any_vol else None), (total_sets if total_sets > 0 else None)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse ISO-8601 UTC timestamp from Hevy; return None if absent."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _workout_to_record(
    workout: dict[str, Any],
    account_ref: str,
    ingested_at: datetime,
    deleted: bool = False,
) -> Record:
    """Map one Hevy workout payload → a ``workout`` Record."""
    raw_exercises = workout.get("exercises", [])
    exercises = _map_exercises(raw_exercises)
    total_volume_kg, set_count = _compute_volume(raw_exercises)

    occurred_at = _parse_dt(workout.get("start_time"))
    if occurred_at is None:
        raise ProviderPayloadError(
            f"Hevy workout {workout.get('id')!r} has no start_time — cannot build Record."
        )
    occurred_end = _parse_dt(workout.get("end_time"))

    duration_minutes: float | None = None
    if occurred_end is not None:
        duration_minutes = round((occurred_end - occurred_at).total_seconds() / 60, 2)

    common = WorkoutCommon(
        activity_type=ActivityType.STRENGTH,
        activity_label=workout.get("title"),
        duration_minutes=duration_minutes,
        elapsed_minutes=duration_minutes,   # Hevy doesn't distinguish active vs elapsed
        total_volume_kg=total_volume_kg,
        set_count=set_count,
        exercises=exercises,
    )

    source_extras: dict[str, Any] = {}
    if deleted:
        source_extras["deleted"] = True
    if workout.get("routine_id"):
        source_extras["routine_id"] = workout["routine_id"]
    if workout.get("description"):
        source_extras["description"] = workout["description"]

    # local_date: Hevy workouts carry no explicit local-date or timezone field.
    # Use the UTC date of occurred_at as a proxy. Consumers in non-UTC timezones should
    # apply the offset themselves using the occurred_at timestamp.
    local_date = occurred_at.date()

    return Record(
        source="hevy",
        domain=Domain.ACTIVITY.value,
        record_type=RecordType.WORKOUT,
        source_record_id=workout["id"],
        account_ref=account_ref,
        occurred_at=occurred_at,
        occurred_end=occurred_end,
        ingested_at=ingested_at,
        local_date=local_date,
        common=common,
        raw=workout,
        source_extras=source_extras or None,
    )


_BODY_EXTRA_KEYS = (
    "neck_cm", "shoulder_cm", "chest_cm", "left_bicep_cm", "right_bicep_cm",
    "left_forearm_cm", "right_forearm_cm", "abdomen", "waist", "hips",
    "left_thigh", "right_thigh", "left_calf", "right_calf",
)


def _body_measurement_to_record(
    measurement: dict[str, Any],
    account_ref: str,
    ingested_at: datetime,
) -> Record:
    """
    Map one Hevy body_measurement payload → a ``body_measurement`` Record.

    Identity: compound ``account_ref:local_date:body_measurement`` — Hevy keyed
    by date only (no native record ID on the list endpoint). Matches the
    daily-aggregate identity rule in connector-contract.md §2.

    local_date: the ``date`` field from Hevy (YYYY-MM-DD); Hevy attributes the
    measurement to the user's local day, so we preserve it as local_date rather
    than inferring from UTC.

    occurred_at: we use midnight UTC of that date as the event instant. Hevy
    body measurements have no time component — midnight UTC is a safe, stable
    sentinel.
    """
    date_str: str | None = measurement.get("date")
    if not date_str:
        raise ProviderPayloadError(
            "Hevy body measurement missing 'date' field — cannot build Record."
        )

    from datetime import date as date_type

    local_date = date_type.fromisoformat(date_str)
    occurred_at = datetime(local_date.year, local_date.month, local_date.day, 0, 0, 0, tzinfo=UTC)

    fat_percent = measurement.get("fat_percent")

    common = BodyMeasurementCommon(
        weight_kg=measurement.get("weight_kg"),
        lean_mass_kg=measurement.get("lean_mass_kg"),
        body_fat_pct=fat_percent,
        # fat_mass_kg, muscle_mass_kg, bone_mass_kg, body_water_pct, visceral_fat_index, bmi
        # are not provided by Hevy body_measurements endpoint — left null.
    )

    source_record_id = f"{account_ref}:{date_str}:body_measurement"

    # Circumferences have no common-schema fields; keep non-null ones in source_extras.
    source_extras = {k: measurement[k] for k in _BODY_EXTRA_KEYS if measurement.get(k) is not None}

    return Record(
        source="hevy",
        domain=Domain.BODY.value,
        record_type=RecordType.BODY_MEASUREMENT,
        source_record_id=source_record_id,
        account_ref=account_ref,
        occurred_at=occurred_at,
        occurred_end=None,
        ingested_at=ingested_at,
        local_date=local_date,
        common=common,
        raw=measurement,
        source_extras=source_extras or None,
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _raise_for_status(resp: httpx.Response, path: str) -> None:
    """Map a non-success Hevy response to a typed OpenVitals error (shared by reads and writes)."""
    if resp.status_code == 401:
        raise AuthRequired("Hevy API key is invalid or expired (401).")
    if resp.status_code == 429:
        retry_after = _parse_retry_after(resp)
        raise RateLimited("Hevy rate limit hit (429).", retry_after=retry_after)
    if resp.status_code >= 500:
        raise ProviderUnavailable(f"Hevy API server error {resp.status_code} on {path}.")
    if not resp.is_success:
        raise ProviderPayloadError(
            f"Hevy API unexpected status {resp.status_code} on {path}: {resp.text[:200]}"
        )


def _hevy_write(
    method: str, path: str, api_key: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Single authenticated POST/PUT. Same error mapping as ``_hevy_get``.

    4xx validation failures surface Hevy's own message in ProviderPayloadError.
    Empty success bodies (Hevy returns 201/200 with no body for measurements) -> ``{}``.
    """
    try:
        resp = httpx.request(
            method,
            f"{HEVY_API_BASE}{path}",
            headers={"api-key": api_key},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise ProviderUnavailable(f"Hevy API timed out on {path}: {exc}") from exc
    except httpx.RequestError as exc:
        raise ProviderUnavailable(f"Hevy API unreachable ({path}): {exc}") from exc

    _raise_for_status(resp, path)
    if not resp.content:
        return {}
    try:
        return resp.json()
    except Exception as exc:
        raise ProviderPayloadError(f"Hevy API returned non-JSON on {path}: {exc}") from exc


def _hevy_get(path: str, api_key: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Single authenticated GET. Raises typed OpenVitals errors on failure."""
    try:
        resp = httpx.get(
            f"{HEVY_API_BASE}{path}",
            headers={"api-key": api_key},
            params=params or {},
            timeout=REQUEST_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise ProviderUnavailable(f"Hevy API timed out on {path}: {exc}") from exc
    except httpx.RequestError as exc:
        raise ProviderUnavailable(f"Hevy API unreachable ({path}): {exc}") from exc

    _raise_for_status(resp, path)

    try:
        return resp.json()
    except Exception as exc:
        raise ProviderPayloadError(f"Hevy API returned non-JSON on {path}: {exc}") from exc


def _parse_retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None


def _paginate_all(
    path: str,
    api_key: str,
    items_key: str,
    extra_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Drain all pages from a Hevy paginated endpoint.

    Hevy pagination: ``page`` (1-based) + ``pageSize`` + ``page_count`` in response.
    Applies THROTTLE_SEC between pages to stay within informal rate limit.
    """
    params: dict[str, Any] = {"page": 1, "pageSize": PAGE_SIZE, **(extra_params or {})}
    results: list[dict[str, Any]] = []
    page_count = 1  # updated from first response

    while params["page"] <= page_count:
        data = _hevy_get(path, api_key, params)
        page_count = data.get("page_count", 1)
        items = data.get(items_key)
        if items is None:
            raise ProviderPayloadError(
                f"Hevy API response for {path} missing expected key '{items_key}'. "
                f"Got keys: {list(data.keys())}"
            )
        results.extend(items)
        params["page"] += 1
        if params["page"] <= page_count:
            time.sleep(THROTTLE_SEC)

    return results


# ---------------------------------------------------------------------------
# HevyConnector
# ---------------------------------------------------------------------------


_WORKOUT_REQUIRED = ("title", "start_time", "end_time", "exercises")


def _require(payload: dict[str, Any], keys: tuple[str, ...], what: str) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise ProviderPayloadError(f"Hevy {what} missing required field(s): {', '.join(missing)}.")


class HevyConnector(BaseConnector):
    """
    OpenVitals connector for Hevy.

    Emits:
      - ``activity/workout`` records (strength workouts, full exercises + sets).
      - ``body/body_measurement`` records (weight, lean mass, body fat %).

    Auth: API key stored in CredentialStore under key ``"hevy_api_key"``.
    No OAuth — the key is static until the user rotates it in Hevy settings.
    ``connect()`` validates the key against the live API before returning an Account.
    """

    source = "hevy"
    domains = [Domain.ACTIVITY.value, Domain.BODY.value]

    def __init__(self, credential_store: CredentialStore, allow_write: bool = False) -> None:
        self._store = credential_store
        self._allow_write = allow_write

    # -----------------------------------------------------------------------
    # connect
    # -----------------------------------------------------------------------

    def connect(self, credentials: dict[str, Any]) -> Account:
        """
        Authenticate with Hevy using a static API key.

        ``credentials`` must contain ``"hevy_api_key"``.  The key is validated
        by GET /v1/user/info, whose ``data.id`` becomes the stable account_ref.

        Raises:
            AuthRequired: key missing, empty, or rejected by Hevy (401).
            ProviderUnavailable: network / 5xx.
        """
        api_key = credentials.get("hevy_api_key", "")
        if not api_key:
            raise AuthRequired(
                "No Hevy API key provided. Pass {'hevy_api_key': '<key>'} as credentials. "
                "Get your key from https://hevy.com/settings?developer (Hevy Pro required)."
            )

        info = _hevy_get("/v1/user/info", api_key)
        user_id = (info.get("data") or {}).get("id")
        if not user_id:
            raise ProviderPayloadError("Hevy /v1/user/info response missing data.id.")
        account_ref = f"hevy:{user_id}"

        # Persist the key so fetch() can retrieve it
        self._store.set(self.source, account_ref, {"hevy_api_key": api_key})

        return Account(
            account_ref=account_ref,
            source=self.source,
            metadata={"key_suffix": api_key[-4:]},  # last 4 chars for debugging only
        )

    # -----------------------------------------------------------------------
    # optional write (opt-in; Hevy-native payloads, no cross-provider mapping)
    # -----------------------------------------------------------------------

    def create_workout(
        self, account: Account, workout: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """POST /v1/workouts with ``{"workout": workout}``."""
        _require(workout, _WORKOUT_REQUIRED, "workout")
        return self._write(account, "POST", "/v1/workouts", {"workout": workout}, dry_run)

    def update_workout(
        self, account: Account, workout_id: str, workout: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """PUT /v1/workouts/{workout_id} with ``{"workout": workout}``."""
        _require(workout, _WORKOUT_REQUIRED, "workout")
        return self._write(
            account, "PUT", f"/v1/workouts/{workout_id}", {"workout": workout}, dry_run
        )

    def create_body_measurement(
        self, account: Account, measurement: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """POST /v1/body_measurements (body is the measurement itself, incl. ``date``)."""
        _require(measurement, ("date",), "body measurement")
        return self._write(account, "POST", "/v1/body_measurements", measurement, dry_run)

    def update_body_measurement(
        self, account: Account, date: str, measurement: dict[str, Any], *, dry_run: bool = False
    ) -> dict[str, Any]:
        """PUT /v1/body_measurements/{date} (body has no ``date`` field; it is in the path)."""
        return self._write(
            account, "PUT", f"/v1/body_measurements/{date}", measurement, dry_run
        )

    def _write(
        self, account: Account, method: str, path: str, body: dict[str, Any], dry_run: bool
    ) -> dict[str, Any]:
        if dry_run:
            return {"method": method, "path": path, "body": body}
        if not self._allow_write:
            raise WriteDisabled(
                "Hevy write access is disabled. Opt in with HevyConnector(allow_write=True) "
                "or OpenVitals.with_builtins(hevy_allow_write=True)."
            )
        creds = self._store.get(self.source, account.account_ref)
        if not creds:
            raise AuthRequired(
                f"No stored credentials for account {account.account_ref!r}. "
                "Call connect() first."
            )
        return _hevy_write(method, path, creds["hevy_api_key"], body)

    # -----------------------------------------------------------------------
    # fetch
    # -----------------------------------------------------------------------

    def fetch(
        self,
        account: Account,
        since: datetime | None = None,
    ) -> Iterable[Record]:
        """
        Yield Records for this account.

        Workouts are fetched incrementally via GET /v1/workouts/events?since=.
        Events arrive newest-first; we yield oldest-first for consistency.
        Deleted events are yielded as Records with ``source_extras.deleted = True``.

        Body measurements are fetched in full each sync (no events endpoint).

        ``since`` — if provided, only workout events at or after this instant
        are fetched. Body measurements are always fetched in full (Hevy has no
        incremental cursor for them).

        Raises:
            AuthRequired: stored key missing or key rejected (401).
            RateLimited: 429 from Hevy.
            ProviderUnavailable: network / 5xx.
            ProviderPayloadError: unexpected payload shape.
            PartialFetchError: failure mid-stream (some records already yielded).
        """
        creds = self._store.get(self.source, account.account_ref)
        if not creds:
            raise AuthRequired(
                f"No stored credentials for account {account.account_ref!r}. "
                "Call connect() first."
            )
        api_key: str = creds["hevy_api_key"]
        ingested_at = datetime.now(UTC)

        collected: list[Record] = []
        try:
            for record in self._fetch_workouts(account.account_ref, api_key, since, ingested_at):
                collected.append(record)
                yield record
            # Body measurements: full pass each time (no cursor available)
            for record in self._fetch_body_measurements(account.account_ref, api_key, ingested_at):
                collected.append(record)
                yield record
        except (AuthRequired, RateLimited, ProviderUnavailable, ProviderPayloadError) as exc:
            if collected:
                raise PartialFetchError(
                    "Fetch failed mid-stream.",
                    records=list(collected),
                ) from exc
            raise

    def _fetch_workouts(
        self,
        account_ref: str,
        api_key: str,
        since: datetime | None,
        ingested_at: datetime,
    ) -> Iterable[Record]:
        """Fetch workout events and yield Records oldest-first."""
        since_str = (
            since.strftime("%Y-%m-%dT%H:%M:%SZ")
            if since is not None
            else "1970-01-01T00:00:00Z"
        )

        all_events = _paginate_all(
            "/v1/workouts/events",
            api_key,
            "events",
            extra_params={"since": since_str},
        )

        # Events arrive newest-first (verified against live API) — reverse for oldest-first yield
        for event in reversed(all_events):
            event_type = event.get("type")
            if event_type == "updated":
                workout = event.get("workout")
                if not isinstance(workout, dict):
                    raise ProviderPayloadError(
                        f"Hevy event of type 'updated' missing 'workout' dict. Event: {event!r}"
                    )
                yield _workout_to_record(workout, account_ref, ingested_at, deleted=False)
            elif event_type == "deleted":
                # Deleted event: emit a minimal Record with deleted flag so callers can act.
                # The event carries only the workout ID (no full payload).
                workout_id = event.get("id")
                if not workout_id:
                    raise ProviderPayloadError(
                        f"Hevy 'deleted' event missing 'id'. Event: {event!r}"
                    )
                # Build a minimal workout Record flagged as deleted.
                # occurred_at = event deleted_at when present, else ingested_at
                # (no payload to parse start_time from).
                occurred_at = _parse_dt(event.get("deleted_at")) or ingested_at
                yield Record(
                    source="hevy",
                    domain=Domain.ACTIVITY.value,
                    record_type=RecordType.WORKOUT,
                    source_record_id=workout_id,
                    account_ref=account_ref,
                    occurred_at=occurred_at,
                    ingested_at=ingested_at,
                    local_date=occurred_at.date(),
                    common=WorkoutCommon(activity_type=ActivityType.STRENGTH),
                    raw=event,
                    source_extras={"deleted": True},
                )
            else:
                log.warning(
                    "Hevy: unrecognised workout event type %r — skipping. "
                    "If Hevy added a new event type, add support here.",
                    event_type,
                )

    def _fetch_body_measurements(
        self,
        account_ref: str,
        api_key: str,
        ingested_at: datetime,
    ) -> Iterable[Record]:
        """Fetch all body measurements and yield Records."""
        measurements = _paginate_all(
            "/v1/body_measurements",
            api_key,
            "body_measurements",
        )
        for m in measurements:
            yield _body_measurement_to_record(m, account_ref, ingested_at)
