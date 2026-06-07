"""
Tests for the OpenVitals library surface (wiring layer).

Tests that:
- Connectors can be registered and dispatched.
- connect() delegates to the connector and caches the Account.
- fetch() delegates to the connector using the cached Account.
- KeyError is raised for unknown sources.
- Multi-connector scenarios work independently.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import pytest

from openvitals.connectors.base import Account, BaseConnector
from openvitals.schema.records import Record, SleepSessionCommon
from openvitals.schema.domains import RecordType
from openvitals.surfaces.library import OpenVitals
from tests.conftest import FakeStore

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Stub connector for testing
# ---------------------------------------------------------------------------


class StubConnector(BaseConnector):
    """A minimal connector that returns canned records."""

    def __init__(self, source: str, domains: list[str], records: list[Record] | None = None):
        self.source = source
        self.domains = domains
        self._records = records or []
        self.connect_called_with: list[dict] = []
        self.fetch_called_with: list[tuple] = []

    def connect(self, credentials: dict[str, Any]) -> Account:
        self.connect_called_with.append(credentials)
        return Account(source=self.source, account_ref="test_acct_001")

    def fetch(self, account: Account, since: datetime | None = None) -> Iterable[Record]:
        self.fetch_called_with.append((account, since))
        return iter(self._records)


def _make_stub_record() -> Record:
    return Record(
        source="stub",
        domain="sleep",
        record_type=RecordType.SLEEP_SESSION,
        source_record_id="rid_001",
        account_ref="test_acct_001",
        occurred_at=datetime(2026, 6, 6, 8, 0, 0, tzinfo=UTC),
        ingested_at=datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC),
        common=SleepSessionCommon(total_sleep_minutes=420),
        raw={"provider_id": "abc"},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenVitalsLibrary:
    def test_register_and_sources(self):
        ov = OpenVitals()
        connector = StubConnector("whoop", ["sleep", "activity"])
        ov.register(connector)
        assert "whoop" in ov.sources

    def test_connect_delegates_to_connector(self):
        ov = OpenVitals()
        connector = StubConnector("whoop", ["sleep"])
        ov.register(connector)
        creds = {"access_token": "tok_abc"}
        account = ov.connect("whoop", credentials=creds)
        assert account.account_ref == "test_acct_001"
        assert connector.connect_called_with == [creds]

    def test_fetch_uses_cached_account(self):
        record = _make_stub_record()
        ov = OpenVitals()
        connector = StubConnector("whoop", ["sleep"], records=[record])
        ov.register(connector)
        ov.connect("whoop", credentials={"access_token": "tok"})
        results = list(ov.fetch("whoop"))
        assert results == [record]
        account_used, since_used = connector.fetch_called_with[0]
        assert account_used.account_ref == "test_acct_001"
        assert since_used is None

    def test_fetch_with_since(self):
        ov = OpenVitals()
        connector = StubConnector("whoop", ["sleep"])
        ov.register(connector)
        ov.connect("whoop", credentials={})
        since = datetime(2026, 1, 1, tzinfo=UTC)
        list(ov.fetch("whoop", since=since))
        _, since_used = connector.fetch_called_with[0]
        assert since_used == since

    def test_unknown_source_raises(self):
        ov = OpenVitals()
        with pytest.raises(KeyError, match="not registered"):
            ov.connect("nonexistent", credentials={})

    def test_fetch_without_connect_raises(self):
        ov = OpenVitals()
        ov.register(StubConnector("whoop", ["sleep"]))
        with pytest.raises(KeyError, match="No account cached"):
            list(ov.fetch("whoop"))

    def test_fetch_with_explicit_account(self):
        """fetch() accepts an account= kwarg to bypass the cache."""
        record = _make_stub_record()
        ov = OpenVitals()
        connector = StubConnector("whoop", ["sleep"], records=[record])
        ov.register(connector)
        explicit_account = Account(source="whoop", account_ref="explicit_acct")
        results = list(ov.fetch("whoop", account=explicit_account))
        assert results == [record]
        account_used, _ = connector.fetch_called_with[0]
        assert account_used.account_ref == "explicit_acct"

    def test_register_replaces_connector(self):
        ov = OpenVitals()
        c1 = StubConnector("whoop", ["sleep"])
        c2 = StubConnector("whoop", ["activity"])
        ov.register(c1)
        ov.register(c2)
        assert len(ov.sources) == 1  # still just one 'whoop'
        ov.connect("whoop", credentials={})
        assert c1.connect_called_with == []  # c1 was replaced
        assert len(c2.connect_called_with) == 1

    def test_multiple_sources_independent(self):
        ov = OpenVitals()
        ov.register(StubConnector("whoop", ["sleep"]))
        ov.register(StubConnector("strava", ["activity"]))
        assert set(ov.sources) == {"whoop", "strava"}
        ov.connect("whoop", credentials={"tok": "w"})
        ov.connect("strava", credentials={"tok": "s"})
        list(ov.fetch("whoop"))
        list(ov.fetch("strava"))


# ---------------------------------------------------------------------------
# Built-in connector registration (integration)
# ---------------------------------------------------------------------------


_FAKE_OAUTH_ENV = {
    "STRAVA_CLIENT_ID": "test_strava_id",
    "STRAVA_CLIENT_SECRET": "test_strava_secret",
    "WITHINGS_CLIENT_ID": "test_withings_id",
    "WITHINGS_CLIENT_SECRET": "test_withings_secret",
}


class TestBuiltins:
    """with_builtins() registers official connectors by default; Cronometer needs enable_unofficial=True."""

    @pytest.fixture(autouse=True)
    def oauth_env(self, monkeypatch):
        for k, v in _FAKE_OAUTH_ENV.items():
            monkeypatch.setenv(k, v)

    def test_with_builtins_registers_four_official(self):
        """Default with_builtins() excludes Cronometer (unofficial API requires opt-in)."""
        ov = OpenVitals.with_builtins(credential_store=FakeStore())
        assert set(ov.sources) == {"whoop", "strava", "withings", "hevy"}

    def test_with_builtins_with_unofficial_registers_all_five(self):
        """enable_unofficial=True adds Cronometer to the registry."""
        ov = OpenVitals.with_builtins(credential_store=FakeStore(), enable_unofficial=True)
        assert set(ov.sources) == {"whoop", "strava", "withings", "hevy", "cronometer"}

    def test_builtins_resolvable_and_unknown_raises(self):
        ov = OpenVitals.with_builtins(credential_store=FakeStore(), enable_unofficial=True)
        for source in ("whoop", "strava", "withings", "hevy", "cronometer"):
            assert source in ov.available()
        with pytest.raises(KeyError):
            ov.connect("garmin", credentials={})

    def test_with_builtins_requires_credential_store(self):
        """with_builtins() raises ValueError when no credential_store is given."""
        with pytest.raises(ValueError, match="credential_store"):
            OpenVitals.with_builtins()
