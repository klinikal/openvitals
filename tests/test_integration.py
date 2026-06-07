"""
Integration tests for the OpenVitals client with built-in connectors wired.

These tests verify the public surface only — no live API calls, no connector
internals touched.
"""

from __future__ import annotations

import pytest

from openvitals import OpenVitals
from tests.conftest import FakeStore


OFFICIAL_SOURCES = {"whoop", "strava", "withings", "hevy"}
ALL_SOURCES = OFFICIAL_SOURCES | {"cronometer"}

FAKE_OAUTH_ENV = {
    "STRAVA_CLIENT_ID": "test_strava_id",
    "STRAVA_CLIENT_SECRET": "test_strava_secret",
    "WITHINGS_CLIENT_ID": "test_withings_id",
    "WITHINGS_CLIENT_SECRET": "test_withings_secret",
}


@pytest.fixture(autouse=True)
def oauth_env(monkeypatch):
    for k, v in FAKE_OAUTH_ENV.items():
        monkeypatch.setenv(k, v)


class TestWithBuiltins:
    def test_available_returns_four_official_sources(self):
        """with_builtins() without enable_unofficial registers only 4 official connectors."""
        client = OpenVitals.with_builtins(credential_store=FakeStore())
        assert client.available() == OFFICIAL_SOURCES

    def test_available_with_unofficial_returns_all_five(self):
        """with_builtins(enable_unofficial=True) registers all 5 connectors."""
        client = OpenVitals.with_builtins(credential_store=FakeStore(), enable_unofficial=True)
        assert client.available() == ALL_SOURCES

    def test_sources_property_matches_available(self):
        """sources property and available() must agree."""
        client = OpenVitals.with_builtins(credential_store=FakeStore())
        assert set(client.sources) == client.available()

    def test_connect_unregistered_raises_key_error(self):
        """connect() on an unknown source raises KeyError with 'not registered'."""
        client = OpenVitals.with_builtins(credential_store=FakeStore())
        with pytest.raises(KeyError, match="not registered"):
            client.connect("unknown_source", credentials={})

    def test_fetch_unregistered_raises_key_error(self):
        """fetch() on an unknown source raises KeyError with 'not registered'."""
        client = OpenVitals.with_builtins(credential_store=FakeStore())
        with pytest.raises(KeyError, match="not registered"):
            list(client.fetch("unknown_source"))

    def test_empty_client_has_no_sources(self):
        """A plain OpenVitals() with no registrations reports an empty set."""
        client = OpenVitals()
        assert client.available() == set()

    def test_with_builtins_does_not_mutate_default_instance(self):
        """with_builtins() returns a new instance; bare OpenVitals() is unaffected."""
        bare = OpenVitals()
        OpenVitals.with_builtins(credential_store=FakeStore())
        assert bare.available() == set()

    def test_with_builtins_requires_credential_store(self):
        """with_builtins() without a credential_store raises ValueError with helpful message."""
        with pytest.raises(ValueError, match="credential_store"):
            OpenVitals.with_builtins()

    def test_with_builtins_requires_oauth_env_vars(self, monkeypatch):
        """with_builtins() raises ValueError listing missing OAuth env vars."""
        monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
        monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
        with pytest.raises(ValueError, match="STRAVA_CLIENT_ID"):
            OpenVitals.with_builtins(credential_store=FakeStore())
