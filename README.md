# OpenVitals

Self-hosted Python library that normalizes consumer health and fitness data from multiple providers into a single `Record` output. Bring your own API keys — no cloud, no phone-home.

**Status:** v0.1 alpha.

---

> **OpenVitals is a library you integrate into your own stack — not a service, platform, or product provided to you.**
>
> There is no "OpenVitals" to sign up to, log into, or rely on. It is open-source code that helps you connect your application to health and fitness providers — handling authentication, data fetching, and normalisation. OpenVitals never stores, persists, or transmits your health data. Records are returned directly to your application as Python objects; where you store them, and how you secure them, is entirely your responsibility. All responsibility for storage, security, and engineering is entirely your own.
---

## Supported sources

| Source | Domains | Notes |
|---|---|---|
| WHOOP | sleep, recovery, activity | OAuth 2.0 |
| Strava | activity | OAuth 2.0; requires `STRAVA_CLIENT_ID` + `STRAVA_CLIENT_SECRET` |
| Withings | body, activity | OAuth 2.0; requires `WITHINGS_CLIENT_ID` + `WITHINGS_CLIENT_SECRET` |
| Hevy | activity, body | API key |
| Cronometer | nutrition | Unofficial API — requires explicit opt-in; see below |

---

## Install

OpenVitals is not yet published to PyPI. Install directly from the repository:

```bash
pip install git+https://github.com/klinikal/openvitals.git
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add git+https://github.com/klinikal/openvitals.git
```

**Python 3.12+ required.**

---

## Quickstart

### 1. Generate an encryption key

Credentials are encrypted at rest with AES-128 (Fernet). Generate a key and export it:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
export OPENVITALS_ENCRYPTION_KEY=<key>
```

The key must be set in the environment whenever OpenVitals runs. Keep it secret — losing it means re-authenticating all accounts. Back it up.

How you store and protect this key is entirely your responsibility. Common approaches include shell profiles (`.zshrc` / `.bashrc`), gitignored `.env` files, or a dedicated secrets manager (Doppler, AWS Secrets Manager, GCP Secret Manager, HashiCorp Vault, and others). OpenVitals does not prescribe a method — use whatever suits your setup and threat model. Securing your secrets is your call and your risk.

### 2. Set OAuth app credentials (Strava and Withings only)

```bash
export STRAVA_CLIENT_ID=<your_strava_client_id>
export STRAVA_CLIENT_SECRET=<your_strava_client_secret>

export WITHINGS_CLIENT_ID=<your_withings_client_id>
export WITHINGS_CLIENT_SECRET=<your_withings_client_secret>
```

WHOOP also requires a developer app — create one at [developer.whoop.com](https://developer.whoop.com). Unlike Strava and Withings, the WHOOP `client_id` and `client_secret` are passed in the credential dict when you call `connect()` rather than as environment variables. Hevy uses an API key only (no app registration required).

### 3. Create a client

```python
from openvitals import OpenVitals
from openvitals.storage import EncryptedFileStore

ov = OpenVitals.with_builtins(
    credential_store=EncryptedFileStore()  # saves to ~/.openvitals/credentials.json
)
```

### 4. Authenticate

Pass credentials from your OAuth callback (or an API key) to `connect()`. The connector validates scopes, refreshes the token if needed, and persists the credential bundle.

```python
# WHOOP — pass the token dict from your OAuth callback
account = ov.connect("whoop", credentials={
    "access_token": "...",
    "refresh_token": "...",
    "expires_at": 1234567890.0,       # UTC epoch float
    "scopes": ["read:recovery", "read:cycles", "read:sleep",
               "read:workout", "read:profile", "offline"],
    "client_id": "...",               # from your WHOOP developer app
    "client_secret": "...",
})

# Hevy — API key only
account = ov.connect("hevy", credentials={"hevy_api_key": "..."})
```

See each connector's module docstring for the full credential shape.

### 5. Fetch records

```python
from datetime import datetime, timezone

since = datetime(2026, 1, 1, tzinfo=timezone.utc)

for record in ov.fetch("whoop", since=since):
    print(record.record_type, record.occurred_at, record.common)
```

`fetch()` returns an `Iterable[Record]`. Each `Record` has:

- **`record_type`** — what kind of data this is (`sleep_session`, `workout`, `daily_recovery`, etc.)
- **`occurred_at`** / **`occurred_end`** — UTC timestamps
- **`local_date`** — calendar date in the provider's local timezone (for day-attributed records)
- **`common`** — typed, nullable fields that generalize across providers
- **`raw`** — verbatim provider payload; byte-faithful, never modified

Full schema: [`docs/specs/record-schema-v1.md`](docs/specs/record-schema-v1.md)

---

## Working with records

```python
import json
from datetime import datetime, timezone
from openvitals import OpenVitals
from openvitals.storage import EncryptedFileStore

ov = OpenVitals.with_builtins(credential_store=EncryptedFileStore())
since = datetime(2026, 1, 1, tzinfo=timezone.utc)

for record in ov.fetch("whoop", since=since):
    # Access typed common fields
    if record.common.hrv_ms is not None:
        print(f"HRV: {record.common.hrv_ms} ms")

    # Serialize to dict / JSON
    data = record.model_dump()
    json_str = record.model_dump_json()

    # Access verbatim provider payload
    raw_payload = record.raw
```

All `common` fields are nullable — not every provider fills every field. Always check for `None`.

---

## Error handling

```python
from openvitals import (
    AuthRequired,       # No credential stored, or credential is unrecoverable
    MissingScope,       # OAuth scope was not granted
    RateLimited,        # Provider returned 429; exc.retry_after gives the wait time
    ProviderUnavailable, # 5xx or network failure
    PartialFetchError,  # Failure mid-stream; exc.records has data collected before failure
)

try:
    for record in ov.fetch("strava", since=since):
        process(record)
except PartialFetchError as exc:
    # Save what was collected before the failure
    for record in exc.records:
        process(record)
    # Do not advance your watermark — re-fetch from the same `since`
except RateLimited as exc:
    print(f"Rate limited. Retry after {exc.retry_after}s")
except AuthRequired:
    print("Re-run connect() to refresh credentials")
```

---

## Cronometer (unofficial API)

Cronometer does not expose an official OAuth API. The connector uses a reverse-engineered mobile API and requires explicit opt-in:

```python
ov = OpenVitals.with_builtins(
    credential_store=EncryptedFileStore(),
    enable_unofficial=True,
)
```

**Risks:** the endpoint may change without notice; a provider-side breaking change is an unsupported provider change, not a library bug. Do not use on shared or multi-tenant systems.

---

## Custom credential store

Implement the `CredentialStore` ABC to plug in your own persistence layer (database, secrets manager, etc.):

```python
from openvitals.storage import CredentialStore

class MyStore(CredentialStore):
    def get(self, source: str, account_ref: str) -> dict | None: ...
    def set(self, source: str, account_ref: str, credentials: dict) -> None: ...
    def delete(self, source: str, account_ref: str) -> None: ...

ov = OpenVitals.with_builtins(credential_store=MyStore())
```

### Token rotation — read this before building a custom store

WHOOP, Strava, and Withings rotate their refresh tokens on every use. Each time OpenVitals refreshes an access token, the provider simultaneously issues a new refresh token and invalidates the old one. OpenVitals calls `set()` on your store immediately after every rotation, before using the new access token.

**Your `set()` implementation must be durable** — it must complete the write before returning. If it fails, raises, or returns before the write is committed, the rotated token is lost and the only recovery is a new OAuth flow from scratch.

Concretely:
- An in-memory store works within a single process run but tokens are lost on exit. The next run will raise `AuthRequired` and require re-authentication.
- A database-backed store must write synchronously. Do not fire-and-forget writes.
- If your store can fail (network, disk), handle the exception and surface it — a swallowed error here means a silently orphaned token.

The built-in `EncryptedFileStore` writes to disk synchronously and handles rotation correctly. If you are only using Hevy (API key) or Cronometer (session token), token rotation does not apply.

---

## Adding a connector

See [`docs/specs/connector-contract.md`](docs/specs/connector-contract.md) for the full contract — sync semantics, auth lifecycle, error taxonomy, identity rules, and the contributor checklist.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Disclaimer

> **EARLY-STAGE SOFTWARE — USE AT YOUR OWN RISK**
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
>
> This project is in active early development. APIs, data models, and interfaces may change without notice. Any user of this software is solely responsible for the use, access, storage, and maintenance of their own data. The authors are not affiliated with, endorsed by, or officially associated with any of the third-party services or platforms referenced (including but not limited to WHOOP, Strava, Withings, Hevy, and Cronometer). This is unofficial software built against public and unofficial APIs.
>
> By downloading or using this software you indemnify and release the creators from and against any liability whatsoever resulting from or in connection with this software.
