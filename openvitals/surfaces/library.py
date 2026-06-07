"""
OpenVitals — the public Python library surface.

Wiring only.  No provider logic here.

Usage::

    from openvitals import OpenVitals
    from openvitals.storage import EncryptedFileStore

    # All built-in connectors pre-registered:
    ov = OpenVitals.with_builtins(credential_store=EncryptedFileStore())

    # Or register a single connector manually:
    ov = OpenVitals(credential_store=EncryptedFileStore())
    ov.register(WhoopConnector())

    account = ov.connect("whoop", credentials={"access_token": "...", ...})
    for record in ov.fetch("whoop", since=datetime(2026, 1, 1, tzinfo=timezone.utc)):
        print(record.record_type, record.occurred_at)
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Iterable

from openvitals.connectors.base import Account, BaseConnector
from openvitals.connectors.cronometer.connector import CronometerConnector
from openvitals.connectors.hevy.connector import HevyConnector
from openvitals.connectors.strava.connector import StravaConnector
from openvitals.connectors.whoop.connector import WhoopConnector
from openvitals.connectors.withings.connector import WithingsConnector
from openvitals.schema.records import Record
from openvitals.storage.credentials import CredentialStore

BUILTIN_CONNECTORS: list[type[BaseConnector]] = [
    WhoopConnector,
    StravaConnector,
    WithingsConnector,
    HevyConnector,
    CronometerConnector,
]


class OpenVitals:
    """
    Main entry point for the OpenVitals library.

    Manages a registry of connectors and delegates ``connect`` / ``fetch``
    calls to the appropriate one by source slug.
    """

    def __init__(self, credential_store: CredentialStore | None = None) -> None:
        """
        Parameters
        ----------
        credential_store:
            The store to use for persisting credentials.  If None, the caller
            must pass credentials directly to ``connect()`` on every call
            (i.e. no persistence).
        """
        self._connectors: dict[str, BaseConnector] = {}
        self._accounts: dict[str, Account] = {}  # source → last connected Account
        self._credential_store = credential_store

    @classmethod
    def with_builtins(
        cls,
        credential_store: CredentialStore | None = None,
        *,
        enable_unofficial: bool = False,
    ) -> "OpenVitals":
        """
        Return an ``OpenVitals`` instance with built-in connectors pre-registered.

        By default registers: WHOOP, Strava, Withings, Hevy.

        Parameters
        ----------
        credential_store:
            Passed through to ``__init__``.  See :meth:`__init__` for details.
        enable_unofficial:
            Set to ``True`` to also register CronometerConnector.  Cronometer
            uses an unofficial API, stores your password encrypted at rest, and
            requires explicit opt-in per connector-contract.md.  Do not enable
            on shared systems.
        """
        if credential_store is None:
            raise ValueError(
                "with_builtins() requires a credential_store — built-in connectors "
                "persist auth tokens between calls. Pass an EncryptedFileStore or "
                "your own CredentialStore implementation.\n\n"
                "Example:\n"
                "    from openvitals.storage import EncryptedFileStore\n"
                "    ov = OpenVitals.with_builtins(credential_store=EncryptedFileStore())"
            )
        missing_env: list[str] = []
        strava_client_id = os.environ.get("STRAVA_CLIENT_ID", "")
        strava_client_secret = os.environ.get("STRAVA_CLIENT_SECRET", "")
        if not strava_client_id:
            missing_env.append("STRAVA_CLIENT_ID")
        if not strava_client_secret:
            missing_env.append("STRAVA_CLIENT_SECRET")
        withings_client_id = os.environ.get("WITHINGS_CLIENT_ID", "")
        withings_client_secret = os.environ.get("WITHINGS_CLIENT_SECRET", "")
        if not withings_client_id:
            missing_env.append("WITHINGS_CLIENT_ID")
        if not withings_client_secret:
            missing_env.append("WITHINGS_CLIENT_SECRET")
        if missing_env:
            raise ValueError(
                f"with_builtins() requires these environment variables to be set: "
                f"{', '.join(missing_env)}\n\n"
                "Strava and Withings connectors need OAuth app credentials. "
                "Create apps at:\n"
                "  Strava:   https://www.strava.com/settings/api\n"
                "  Withings: https://developer.withings.com/\n\n"
                "Then set the env vars before calling with_builtins()."
            )

        instance = cls(credential_store=credential_store)
        instance.register(WhoopConnector(credential_store=credential_store))
        instance.register(
            StravaConnector(
                client_id=strava_client_id,
                client_secret=strava_client_secret,
                credential_store=credential_store,
            )
        )
        instance.register(
            WithingsConnector(
                client_id=withings_client_id,
                client_secret=withings_client_secret,
                credential_store=credential_store,
            )
        )
        instance.register(HevyConnector(credential_store=credential_store))
        if enable_unofficial:
            instance.register(
                CronometerConnector(credential_store=credential_store, enabled=True)
            )
        return instance

    def register(self, connector: BaseConnector) -> None:
        """
        Register a connector.  Replaces any existing connector for the same
        source slug.
        """
        self._connectors[connector.source] = connector

    def connect(self, source: str, credentials: dict[str, Any]) -> Account:
        """
        Authenticate with ``source`` and cache the returned ``Account``.

        If a ``credential_store`` was provided, the caller is responsible for
        pre-loading ``credentials`` from it (or supplying fresh ones during
        the initial auth flow).  The connector may persist updated tokens back
        via the store if passed to it directly — this library does not mediate
        token refresh automatically.  (That lifecycle belongs to the connector
        per connector-contract.md §3.)

        Raises ``KeyError`` if ``source`` is not registered.
        Raises ``AuthRequired``, ``MissingScope`` from the connector.
        """
        connector = self._get_connector(source)
        account = connector.connect(credentials)
        self._accounts[source] = account
        return account

    def fetch(
        self,
        source: str,
        since: datetime | None = None,
        account: Account | None = None,
    ) -> Iterable[Record]:
        """
        Yield ``Record`` objects from ``source``.

        Uses the cached ``Account`` from the last ``connect()`` call, or the
        ``account`` argument if supplied (useful for multi-account scenarios).

        Raises ``KeyError`` if ``source`` is not registered or no account is
        cached and none is provided.
        """
        connector = self._get_connector(source)
        if account is None:
            account = self._accounts.get(source)
            if account is None:
                raise KeyError(
                    f"No account cached for source '{source}'. "
                    "Call connect() first or pass account= explicitly."
                )
        return connector.fetch(account, since=since)

    @property
    def sources(self) -> list[str]:
        """Return the slugs of all registered connectors."""
        return list(self._connectors.keys())

    def available(self) -> set[str]:
        """Return the set of slugs for all registered connectors."""
        return set(self._connectors.keys())

    def _get_connector(self, source: str) -> BaseConnector:
        connector = self._connectors.get(source)
        if connector is None:
            raise KeyError(
                f"Source '{source}' is not registered. "
                f"Registered sources: {list(self._connectors.keys())}"
            )
        return connector
