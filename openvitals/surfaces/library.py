"""
OpenVitals — the public Python library surface.

Wiring only.  No provider logic here.

Usage::

    from openvitals import OpenVitals
    from openvitals.storage import EncryptedFileStore

    ov = OpenVitals(credential_store=EncryptedFileStore())
    ov.register(WHOOPConnector())

    account = ov.connect("whoop", credentials={"access_token": "...", ...})
    for record in ov.fetch("whoop", since=datetime(2026, 1, 1, tzinfo=timezone.utc)):
        print(record.record_type, record.occurred_at)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from openvitals.connectors.base import Account, BaseConnector
from openvitals.schema.records import Record
from openvitals.storage.credentials import CredentialStore


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

    def _get_connector(self, source: str) -> BaseConnector:
        connector = self._connectors.get(source)
        if connector is None:
            raise KeyError(
                f"Source '{source}' is not registered. "
                f"Registered sources: {list(self._connectors.keys())}"
            )
        return connector
