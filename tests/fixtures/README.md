# tests/fixtures/

This directory holds golden fixtures — redacted, real provider payloads used to assert that:

1. `raw` equals the provider payload verbatim (no-data-loss).
2. Every `common` field maps correctly from the source payload.

**Connector authors:** when you implement a connector, add a redacted fixture
file here named `<source>_<record_type>.json`, e.g. `whoop_sleep_session.json`.

The fixture must:
- Be a real provider payload (captured from the live API during development).
- Have all PII removed (user IDs replaced with `"REDACTED"`, names cleared, etc.).
- Be committed alongside the connector code — it is the ground truth for the
  mapping tests required by connector-contract.md §6.

**Never commit unredacted fixtures.** Run `git diff --stat HEAD` before pushing
and confirm no `fixtures/*.json` files contain real names, emails, or location data.
