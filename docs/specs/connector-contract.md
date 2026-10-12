# OpenVitals — Connector Contract

Every connector must satisfy this contract. It pairs with `record-schema-v1.md`, which defines the data shape.

---

## 1. Sync semantics

**Signature:** `fetch(account: Account, since: datetime | None = None) -> Iterable[Record]`

- `since` means "return everything that changed at or after this instant." Connectors use the provider's updated/modified cursor where available, otherwise created time, otherwise a provider-specific cursor — documented per connector.
- **Idempotency:** re-fetching an overlapping range must produce the same logical records. Callers are expected to use a look-back window rather than an exact high-water mark.
- **Late updates:** a record edited after creation re-emits with the same `source_record_id` and updated fields, so the caller's upsert corrects it. Connectors use updated-time cursors where the provider supports them.
- **Deletions:** if the provider exposes deletions, surface them via `source_extras.deleted = True`. If not, document explicitly that deletions are undetectable for that source.
- **Pagination:** fully drained. Multi-page fetch is covered by tests.
- **Statelessness:** connectors persist no watermark. The caller owns the cursor. The `CredentialStore` is used only for auth tokens, never for sync state.

---

## 2. Record identity

`source_record_id` must be stable across re-fetches. Rules by record shape:

| Shape | Rule |
|---|---|
| Provider-native ID exists (workouts, sleep sessions, food entries, food definitions) | Use it verbatim. |
| Daily / aggregate records (daily_recovery, daily_summary) | Compound `account_ref:local_date:record_type` — e.g. `acct_001:2026-06-06:daily_recovery` |
| Multiple records from one provider event (Withings measure group) | Append a type suffix — e.g. `<grpid>:vascular_age`, `<grpid>:segmental_composition` |

Each connector's documentation states which IDs are provider-native and which are synthesized, and confirms stability across re-syncs.

**Consumer dedup key:** `source + account_ref + record_type + source_record_id`

---

## 3. Auth lifecycle

Every connector must satisfy and test:

- **Token rotation:** newly rotated refresh tokens are persisted to the `CredentialStore` immediately, before the new access token is used. WHOOP, Strava, and Withings all rotate on every use — losing the persist step orphans the token permanently.
- **Expiry:** access-token refresh-on-expiry is tested.
- **Scope validation:** granted scopes are parsed after auth and validated against required scopes. A missing scope raises `MissingScope` loudly — not a downstream 401 or silent empty result.
- **Session-based connectors (Cronometer):** session expiry is detected and handled explicitly — re-login or a typed error. Not surfaced as a generic failure.
- **Secrets:** tokens and credentials are never logged. Tests assert that credential keys do not appear in `account.metadata`.

---

## 4. Error and partial-failure model

Connectors raise typed errors so callers can react correctly:

| Error | When |
|---|---|
| `AuthRequired` | No credential stored, or credential is unrecoverable |
| `MissingScope` | Required OAuth scope was not granted |
| `RateLimited` | Provider returned 429; carries `retry_after` if known |
| `ProviderUnavailable` | Provider returned 5xx or network failure |
| `ProviderPayloadError` | Provider returned a malformed or unexpected response shape |
| `NoData` | No records exist in the requested range — normal result, not an error |
| `PartialFetchError` | Failure mid-stream; carries `records` (collected before failure) so the caller does not lose work. Caller should not advance its watermark past the gap. |

Transient errors (`RateLimited`, `ProviderUnavailable`) use bounded exponential backoff.

---

## 5. Adding a new connector

Before implementation, complete and commit:

- [ ] Provider API documentation reviewed (or a live API sample captured).
- [ ] `docs/specs/<provider>-api-reference.md` saved — endpoints, auth flow, required scopes, pagination scheme, rate limits, relevant response fields.
- [ ] Redacted golden fixture captured in `tests/fixtures/<provider>/`.
- [ ] Endpoint paths, scopes, pagination, and field shapes confirmed against the live API — not documentation alone.

---

## 6. Testing requirements

A connector is considered complete when all of the following are covered:

- **Mapping:** provider fixture → `Record` mapping, every `common` field verified.
- **Contract:** every emitted `Record` validates against the schema (Pydantic model validation passes).
- **`raw` faithfulness:** a golden fixture asserts `raw == provider_payload` — no fields dropped, renamed, or decoded.
- **Auth:** refresh-token rotation, missing-scope error, token-not-in-metadata assertion.
- **Pagination:** multi-page fetch drains fully and produces the correct record count.
- **Idempotency:** re-fetching an overlapping range produces identical `source_record_id` sets.
- **Live smoke tests:** opt-in only, gated behind an environment variable, never run in CI by default.

---

## Optional write

Connectors are read-only by default. A connector may offer opt-in writes (currently only Hevy): construct it with an explicit flag (`allow_write=True`), accept Provider-native payloads with no cross-provider mapping, support `dry_run=True` (returns `{method, path, body}`, sends nothing), and raise `WriteDisabled` before any network call when not opted in. Errors map as for reads.

## 7. Cronometer — unofficial API

The Cronometer connector uses a reverse-engineered mobile API, not an official OAuth integration. Its failure modes differ from the OAuth-based connectors.

- Requires explicit opt-in: `OpenVitals.with_builtins(enable_unofficial=True)`. Not registered by default.
- Runs against the user's own account only. No hosted or multi-tenant use is implied.
- Session expiry is detected and handled per §3. A session expiry produces a re-login attempt, not a generic error.
- A breaking provider-side change (endpoint moved, protocol changed) is an **unsupported provider change**, not a connector bug. Users should understand this risk before depending on it.
- Redacted fixture coverage is required.
