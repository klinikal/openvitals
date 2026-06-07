# OpenVitals — Record Schema v0.1

**Status:** alpha. The `common` layer may change before v1.0. From v1.0 onward, `common` is strictly add-only — no removals, renames, or repurposing.

**Scope:** five sources (WHOOP, Strava, Withings, Hevy, Cronometer) across five domains — `sleep`, `activity`, `recovery`, `body`, `nutrition`. The `biomarker` and `genetics` domains are defined below but not yet implemented.

---

## Record envelope

Every record is a `Record` object with the following fields.

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `str` | yes | Always `"0.1"` for v0.1 records. |
| `source` | `str` | yes | Provider slug — `whoop`, `strava`, `withings`, `hevy`, `cronometer`. |
| `domain` | `str` | yes | Domain slug — `sleep`, `activity`, `recovery`, `body`, `nutrition`. |
| `record_type` | `RecordType` | yes | See record types below. |
| `source_record_id` | `str` | yes | Stable ID — provider-native where available; compound `account_ref:local_date:record_type` for daily aggregates. See connector contract §2 for full identity rules. |
| `account_ref` | `str` | yes | Opaque account identifier, provider-scoped. |
| `occurred_at` | `datetime` | yes | Event start, UTC ISO-8601. |
| `occurred_end` | `datetime \| None` | no | Session end, UTC ISO-8601. |
| `ingested_at` | `datetime` | yes | When this record was fetched, UTC ISO-8601. |
| `timezone` | `str \| None` | no | IANA timezone string (e.g. `Europe/London`). Lets consumers reconstruct local time exactly. |
| `local_date` | `date \| None` | no | Calendar date the provider attributes this record to. Set for day-based records (sleep, recovery, daily nutrition, body). UTC date alone is insufficient for records that cross midnight. |
| `common` | `CommonModel` | yes | Typed per-record_type model. All fields nullable — not every source fills every field. |
| `raw` | `dict` | yes | Verbatim provider payload. Never decoded, renamed, or restructured. The no-data-loss snapshot — all decoding lives in `common` and the envelope. |
| `source_extras` | `dict \| None` | no | Provider-native values that do not generalize across sources. Documented per connector. |

**Consumer dedup/upsert key:** `source + account_ref + record_type + source_record_id`

---

## Conventions

**Units are in the field name.** `_kg`, `_minutes`, `_pct`, `_bpm`, `_ms`, `_meters`, `_kcal`, `_celsius`, `_g`, `_watts`, `_mps`.

**Every `common` field is nullable.** Sources vary in what they expose. Consumers must handle nulls.

**All timestamps are UTC ISO-8601.** `occurred_at`, `occurred_end`, `ingested_at` are always UTC. Use `timezone` and `local_date` for local-time display.

**`raw` is verbatim.** Never decode, drop, or rename fields inside `raw`. Decoded and normalized values live in `common` and the envelope — for example, Withings encodes measurements as `value × 10^unit`; `common.weight_kg` carries the decoded value; `raw` retains the encoded form unchanged.

**Provider scores are prefixed `provider_`.** For example, `provider_sleep_score`. These are the provider's own numbers, not computed by OpenVitals.

**Categoricals use a controlled vocabulary.** Activity type, meal slot, body segment, and cardiovascular marker type are normalized. The original provider string is preserved alongside as `*_label` where relevant.

**Field placement rule.**
- `common` — cross-source fields useful to most consumers.
- `source_extras` — provider-native headline values that do not generalize; must be documented per connector.
- `raw` — everything else, verbatim.

**Score state is surfaced, never filtered.** Where a provider marks data completeness (WHOOP: `scored` / `unscored` / `pending`), the state is passed through via `score_state`. OpenVitals never drops records based on score state — filtering is the consumer's responsibility.

---

## `sleep` → `sleep_session`

*Sources: WHOOP.*

| Field | Type | Notes |
|---|---|---|
| `total_sleep_minutes` | `int` | Time asleep, excluding awake periods. |
| `time_in_bed_minutes` | `int` | |
| `sleep_efficiency_pct` | `float` | `total_sleep / time_in_bed` |
| `sleep_latency_minutes` | `int` | Time to fall asleep. |
| `awakenings_count` | `int` | |
| `stage_rem_minutes` | `int` | |
| `stage_deep_minutes` | `int` | |
| `stage_light_minutes` | `int` | |
| `stage_awake_minutes` | `int` | |
| `avg_heart_rate_bpm` | `int` | |
| `avg_hrv_ms` | `float` | Overnight HRV (rMSSD), milliseconds. |
| `respiratory_rate_bpm` | `float` | Breaths per minute. |
| `avg_spo2_pct` | `float` | Blood oxygen saturation. |
| `provider_sleep_score` | `int` | Provider's own 0–100 score, if available. |
| `score_state` | `enum` | `scored` \| `unscored` \| `pending` |

*In `raw`, not `common`: per-stage cycle breakdowns, sleep need and debt, temperature deltas.*

---

## `activity` → `workout`

*Sources: WHOOP, Strava, Hevy. Cardio and strength share this record type.*

| Field | Type | Notes |
|---|---|---|
| `activity_type` | `enum` | `run` \| `ride` \| `swim` \| `walk` \| `hike` \| `strength` \| `cardio` \| `sauna` \| `other` |
| `activity_label` | `str` | Original provider sport string, e.g. `TrailRun`, `Sauna`. |
| `duration_minutes` | `float` | Active/moving time. |
| `elapsed_minutes` | `float` | Wall-clock time including pauses. |
| `distance_meters` | `float` | Null for strength. |
| `elevation_gain_meters` | `float` | |
| `avg_heart_rate_bpm` | `int` | |
| `max_heart_rate_bpm` | `int` | |
| `energy_kcal` | `float` | |
| `avg_speed_mps` | `float` | Metres per second. |
| `avg_power_watts` | `float` | |
| `total_volume_kg` | `float` | Strength: Σ(weight × reps) across all sets. |
| `set_count` | `int` | Strength. |
| `exercises` | `array` | Strength: full exercise and set breakdown — see structure below. |
| `provider_strain` | `float` | Provider's own effort/strain score, if available. |
| `score_state` | `enum` | `scored` \| `unscored` \| `pending` |

**`exercises[]` structure.** Order is load-bearing — preserve it.

```
exercises: [{
  exercise_order:  int,           // position in the workout — required
  exercise_name:   str | null,
  exercise_ref_id: str | null,    // source's template ID, if given
  primary_muscle:  str | null,
  superset_id:     str | null,
  notes:           str | null,
  sets: [{
    set_order:        int,        // position within the exercise — required
    set_ref_id:       str | null,
    set_type:         enum,       // normal | warmup | dropset | failure
    weight_kg:        float | null,
    reps:             int | null,
    rpe:              float | null,
    distance_meters:  float | null,
    duration_seconds: float | null
  }]
}]
```

*In `raw`, not `common`: HR zones, lap splits, GPS tracks.*

---

## `recovery` → `daily_recovery`

*One record per day. Sources: WHOOP.*

| Field | Type | Notes |
|---|---|---|
| `hrv_ms` | `float` | Morning HRV (rMSSD), milliseconds. |
| `resting_heart_rate_bpm` | `int` | |
| `provider_recovery_score` | `int` | Provider's own 0–100 score, if available. |
| `avg_spo2_pct` | `float` | |
| `skin_temp_celsius` | `float` | |
| `respiratory_rate_bpm` | `float` | |
| `score_state` | `enum` | `scored` \| `unscored` \| `pending` |

---

## `body` → `body_measurement`

*Sources: Withings, Hevy.*

| Field | Type | Notes |
|---|---|---|
| `weight_kg` | `float` | |
| `body_fat_pct` | `float` | |
| `fat_mass_kg` | `float` | |
| `lean_mass_kg` | `float` | Fat-free mass. |
| `muscle_mass_kg` | `float` | |
| `bone_mass_kg` | `float` | |
| `body_water_pct` | `float` | Currently null for Withings — provider reports hydration in kg; see `source_extras.hydration_kg`. |
| `visceral_fat_index` | `float` | |
| `bmi` | `float` | Populated only when the source provides it directly. |

*In `raw`, not `common`: circumference measurements; device metadata.*

## `body` → `segmental_composition`

*Per-limb composition. Sources: Withings (Body Scan). One record per measurement group.*

| Field | Type | Notes |
|---|---|---|
| `segments` | `array` | `[{ segment, fat_pct, muscle_mass_kg }]` where `segment ∈ { left_arm, right_arm, trunk, left_leg, right_leg }` |

## `body` → `cardiovascular_marker`

*Device-measured cardiovascular metrics. Sources: Withings. One record per marker per measurement group.*

| Field | Type | Notes |
|---|---|---|
| `marker_type` | `enum` | `vascular_age` \| `pulse_wave_velocity` \| `ecg_result` \| `nerve_health_score` |
| `value` | `float \| str` | Numeric, or categorical string for `ecg_result`. |
| `unit` | `str` | e.g. `years`, `mps`. Null for categorical markers. |

---

## `nutrition` → `food_entry`

*One record per logged food item. Sources: Cronometer.*

| Field | Type | Notes |
|---|---|---|
| `food_name` | `str` | Resolved human-readable name. Connectors resolve opaque food IDs internally — see "Food resolution" below. |
| `food_ref_id` | `str` | Source food identifier. Join key to `food_definition`. |
| `food_tags` | `list[str]` | e.g. `["Whole", "Vegetarian"]` |
| `food_category` | `str` | Food group, if available. |
| `food_source` | `str` | Provenance — `NCCDB`, `USDA`, `Custom`, etc. |
| `brand` | `str` | If provided by the source. |
| `serving_amount` | `float` | |
| `serving_unit` | `str` | e.g. `g`, `ml`, `cup` |
| `meal` | `enum` | `breakfast` \| `lunch` \| `dinner` \| `snack` \| `uncategorized` |
| `energy_kcal` | `float` | |
| `protein_g` | `float` | |
| `carbs_g` | `float` | |
| `fat_g` | `float` | |
| `fiber_g` | `float` | |
| `sugar_g` | `float` | |
| `alcohol_g` | `float` | |
| `micronutrients` | `object` | Canonical micronutrient set — see below. |

## `nutrition` → `daily_summary`

*One record per day. Sources: Cronometer.*

| Field | Type | Notes |
|---|---|---|
| `energy_kcal` | `float` | |
| `protein_g` | `float` | |
| `carbs_g` | `float` | |
| `fat_g` | `float` | |
| `fiber_g` | `float` | |
| `sugar_g` | `float` | |
| `alcohol_g` | `float` | |
| `energy_target_kcal` | `float` | User's calorie goal, if set. |
| `micronutrients` | `object` | Currently null for Cronometer — the provider's daily summary block is macro-only. Per-item micronutrients are on each `food_entry`. |

## `nutrition` → `food_definition`

*Reference food data — the food database entry, not a diary event. One record per distinct food. May include personal data (custom foods, recipes, barcodes). Sources: Cronometer.*

| Field | Type | Notes |
|---|---|---|
| `food_ref_id` | `str` | Source food ID. Matches `food_entry.food_ref_id`. Also the `source_record_id`. |
| `food_name` | `str` | |
| `food_category` | `str` | |
| `food_source` | `str` | `NCCDB`, `USDA`, `Custom`, etc. |
| `food_tags` | `list[str]` | |
| `barcode` | `str` | If present. |
| `energy_kcal` | `float` | Per reference serving. |
| `protein_g` / `carbs_g` / `fat_g` | `float` | Per reference serving. |
| `reference_serving_amount` | `float` | The serving size the macros above describe. |
| `reference_serving_unit` | `str` | |
| `micronutrients` | `object` | Canonical micronutrient set per reference serving — see below. |

*In `raw`, not `common`: full named nutrient list (all nutrients the source exposes, with names and units), unit measures, translations, owner and retired flags.*

**Food resolution.** Where a source stores opaque food IDs in the diary (Cronometer), the connector resolves them internally — fetching food details and joining the source's nutrient-ID-to-name dictionary — so emitted records carry human-readable names and nutrients. The nutrient dictionary is internal connector state, never an emitted record. Consumers should cache resolved food definitions by `food_ref_id` to avoid redundant lookups.

---

## Canonical micronutrients

A fixed key set with standardized units. Every field is nullable — present only when the source reports it. The full provider nutrient panel is always in `raw`. This set is add-only from v1.0.

```
micronutrients: {
  # Vitamins
  vitamin_a_mcg, vitamin_c_mg, vitamin_d_mcg, vitamin_e_mg, vitamin_k_mcg,
  thiamin_b1_mg, riboflavin_b2_mg, niacin_b3_mg, vitamin_b6_mg,
  folate_b9_mcg, vitamin_b12_mcg,
  # Minerals
  calcium_mg, iron_mg, magnesium_mg, zinc_mg, potassium_mg,
  sodium_mg, selenium_mcg, iodine_mcg, phosphorus_mg,
  # Fats
  omega3_g, omega6_g, saturated_fat_g, cholesterol_mg
}
```

---

## `biomarker` → `lab_result`

*Not yet implemented. Shape is provisional.*

| Field | Type | Notes |
|---|---|---|
| `marker_name` | `str` | Display name, e.g. `LDL Cholesterol`. |
| `marker_code` | `str` | LOINC or standard code, if known. |
| `value` | `float \| str` | |
| `unit` | `str` | e.g. `mmol/L` |
| `reference_low` | `float` | Lower bound of the normal range. |
| `reference_high` | `float` | Upper bound. |
| `flag` | `enum` | `low` \| `normal` \| `high` — if the source provides it. |

## `genetics` → `genetic_variant`

*Not yet implemented. Shape is provisional.*

| Field | Type | Notes |
|---|---|---|
| `rsid` | `str` | e.g. `rs1801133` |
| `genotype` | `str` | e.g. `CT` |
| `chromosome` | `str` | If given. |
| `position` | `int` | If given. |
| `gene` | `str` | Only if stated by the source. |

---

## What is not in `common`

- **No derived or computed values.** No scoring, no trends, no cross-source fusion. Everything in `common` is a direct observation from the provider.
- **No biomarker or SNP interpretation.** `gene` is populated only when the source file states it explicitly.
- **Provider-specific depth stays in `raw`.** HR zones, lap splits, GPS tracks, full micronutrient panels beyond the canonical set, circumferences — all available in `raw`, not promoted to `common`.
