"""
BaseConnector — the operational contract every connector must implement.

See connector-contract.md for the full spec.  This module defines the ABC
and the ``Account`` model.  Connector authors extend ``BaseConnector``; they
do NOT need to touch pyproject.toml (all deps are pre-declared).

Sync semantics (connector-contract.md §1):
- ``fetch(account, since)`` is idempotent: re-fetching an overlapping range
  produces the same logical records.
- ``since`` is inclusive: fetch everything that changed at or after this
  instant.  The caller is responsible for maintaining the watermark and should
  use a look-back overlap window rather than trusting exact boundaries.
- The connector persists NO watermark.  It may use ``CredentialStore`` only
  for auth tokens, never for sync state.
- Late-updated records must re-emit with the same ``source_record_id`` so the
  caller's upsert can correct them.
- If one page/record fails mid-stream, raise ``PartialFetchError`` after
  yielding what was already gathered.

Identity rules (connector-contract.md §2):
- Provider-native ID exists → use it verbatim as ``source_record_id``.
- Daily/aggregate records → compound ``account_ref:local_date:record_type``.
- Derived-from-group → ``<measure_id>:<marker_type>`` etc.
- Document, per connector, which IDs are native vs synthesized.

Dedup/upsert key (consumer side):
    source + account_ref + record_type + source_record_id
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Iterable

from pydantic import BaseModel

from openvitals.schema.records import Record


class Account(BaseModel):
    """
    Opaque account handle returned by ``BaseConnector.connect()``.

    ``account_ref`` is used as the stable, provider-scoped user identifier in
    every ``Record`` this account produces.  Connectors must ensure it is
    stable across re-auths (e.g. use the provider's user ID, not a token).

    ``metadata`` holds any provider-native account details the connector
    finds useful (display name, granted scopes, token expiry, etc.).  It is
    not part of the ``Record`` schema — it is connector-internal bookkeeping.
    Never store raw secrets here; those go in ``CredentialStore``.
    """

    account_ref: str
    source: str
    metadata: dict[str, Any] = {}


class BaseConnector(ABC):
    """
    Abstract base class for all OpenVitals connectors.

    Subclass this for each provider.  Override ``source``, ``domains``,
    ``connect``, and ``fetch``.

    Example usage::

        class WHOOPConnector(BaseConnector):
            source = "whoop"
            domains = ["sleep", "activity", "recovery"]

            def connect(self, credentials):
                ...

            def fetch(self, account, since=None):
                ...
    """

    # --- Class-level declarations (override in subclasses) ------------------

    #: Provider slug used in every Record emitted, e.g. ``"whoop"``.
    source: str

    #: Domain slugs this connector can produce, e.g. ``["sleep", "activity"]``.
    #: Use ``Domain`` enum values.
    domains: list[str]

    # --- Interface ----------------------------------------------------------

    @abstractmethod
    def connect(self, credentials: dict[str, Any]) -> Account:
        """
        Authenticate with the provider and return an ``Account``.

        ``credentials`` is a provider-specific dict — typically an OAuth token
        bundle (``access_token``, ``refresh_token``, ``expires_at``) or an
        API key.  The caller retrieves these from ``CredentialStore``; the
        connector is given them pre-fetched.

        Auth lifecycle (connector-contract.md §3):
        - OAuth: validate scopes immediately after auth.  Raise ``MissingScope``
          if a required scope is absent (loud failure, not a silent empty result).
        - Rotate and persist refresh tokens immediately on use (WHOOP, Strava,
          Withings all rotate).  Losing this orphans the token.
        - For session-based connectors (Cronometer): detect session expiry
          explicitly and raise ``AuthRequired``, not a generic error.

        Returns an ``Account`` with a stable ``account_ref``.
        Raises ``AuthRequired``, ``MissingScope``.
        """

    @abstractmethod
    def fetch(
        self,
        account: Account,
        since: datetime | None = None,
    ) -> Iterable[Record]:
        """
        Yield ``Record`` objects for this account.

        ``since`` — if provided, fetch everything that changed at or after
        this UTC instant.  If None, fetch all available history (use with
        care — may be slow).  Connectors should prefer the provider's
        ``updated`` cursor where one exists, else ``created`` time.

        Pagination: drain all pages.  Never return a partial page silently.

        Error handling:
        - Transient failures (``RateLimited``, ``ProviderUnavailable``):
          apply bounded backoff before raising.
        - Mid-stream failure: yield collected records, then raise
          ``PartialFetchError`` so the caller knows not to advance its
          watermark past the gap.
        - No records in range: return an empty iterable (do NOT raise
          ``NoData`` unless the source signals explicit absence).

        Raises ``AuthRequired``, ``RateLimited``, ``ProviderUnavailable``,
        ``ProviderPayloadError``, ``PartialFetchError``.
        """
