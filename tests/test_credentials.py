"""
Tests for EncryptedFileStore.

- Round-trip: encrypt → write → read → decrypt returns original credential.
- File permissions are 0600 after write.
- Missing env var raises RuntimeError (not a silent failure).
- delete() removes the entry; subsequent get() returns None.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from openvitals.storage.credentials import (
    ENV_VAR_FERNET_KEY,
    CredentialStoreError,
    EncryptedFileStore,
)


@pytest.fixture
def fernet_key() -> bytes:
    return Fernet.generate_key()


@pytest.fixture
def store(tmp_path, fernet_key) -> EncryptedFileStore:
    store_path = tmp_path / "credentials.json"
    return EncryptedFileStore(path=store_path, fernet_key=fernet_key)


class TestEncryptedFileStore:
    def test_round_trip(self, store):
        """Credential stored and retrieved must equal the original."""
        cred = {"access_token": "tok_abc", "refresh_token": "ref_xyz", "expires_at": 9999999999}
        store.set("whoop", "acct_001", cred)
        result = store.get("whoop", "acct_001")
        assert result == cred

    def test_get_missing_returns_none(self, store):
        assert store.get("whoop", "no_such_account") is None

    def test_delete_removes_entry(self, store):
        cred = {"access_token": "tok_to_delete"}
        store.set("strava", "acct_002", cred)
        assert store.get("strava", "acct_002") is not None
        store.delete("strava", "acct_002")
        assert store.get("strava", "acct_002") is None

    def test_delete_nonexistent_is_noop(self, store):
        """delete() on a missing key must not raise."""
        store.delete("whoop", "ghost_account")  # should not raise

    def test_set_overwrites(self, store):
        """Setting a credential twice must overwrite — not duplicate."""
        store.set("withings", "acct_003", {"token": "v1"})
        store.set("withings", "acct_003", {"token": "v2"})
        result = store.get("withings", "acct_003")
        assert result == {"token": "v2"}

    def test_multiple_accounts_independent(self, store):
        """Different (source, account_ref) pairs must not interfere."""
        store.set("whoop", "acct_001", {"tok": "a"})
        store.set("whoop", "acct_002", {"tok": "b"})
        store.set("strava", "acct_001", {"tok": "c"})
        assert store.get("whoop", "acct_001") == {"tok": "a"}
        assert store.get("whoop", "acct_002") == {"tok": "b"}
        assert store.get("strava", "acct_001") == {"tok": "c"}

    def test_file_permissions_0600(self, tmp_path):
        """The credential file must be written with 0600 permissions."""
        store_path = tmp_path / "credentials.json"
        store2 = EncryptedFileStore(path=store_path, fernet_key=Fernet.generate_key())
        store2.set("test", "acct", {"key": "value"})
        file_stat = store_path.stat()
        # Only owner read+write — no group or other bits
        assert file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
        assert file_stat.st_mode & stat.S_IRUSR
        assert file_stat.st_mode & stat.S_IWUSR

    def test_missing_env_var_raises(self, monkeypatch):
        """If OPENVITALS_ENCRYPTION_KEY is unset and no key is passed, must raise a clear error."""
        monkeypatch.delenv(ENV_VAR_FERNET_KEY, raising=False)
        from openvitals.storage.credentials import EncryptionKeyMissingError
        with pytest.raises(EncryptionKeyMissingError, match=ENV_VAR_FERNET_KEY):
            EncryptedFileStore()

    def test_key_from_env_var(self, monkeypatch, tmp_path):
        """EncryptedFileStore accepts the key from the env var."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv(ENV_VAR_FERNET_KEY, key)
        store_path = tmp_path / "credentials.json"
        s = EncryptedFileStore(path=store_path)  # key not passed explicitly
        s.set("test", "acct", {"token": "secret"})
        assert s.get("test", "acct") == {"token": "secret"}

    def test_different_key_cannot_decrypt(self, tmp_path):
        """Data written with one key cannot be decrypted with a different key."""
        key1 = Fernet.generate_key()
        key2 = Fernet.generate_key()
        store_path = tmp_path / "credentials.json"
        s1 = EncryptedFileStore(path=store_path, fernet_key=key1)
        s1.set("test", "acct", {"token": "secret"})
        s2 = EncryptedFileStore(path=store_path, fernet_key=key2)
        with pytest.raises(CredentialStoreError, match="encryption key"):
            s2.get("test", "acct")

    def test_corrupt_store_raises_and_is_not_overwritten(self, store, tmp_path):
        path = tmp_path / "credentials.json"
        path.write_text("{not json")
        with pytest.raises(CredentialStoreError, match="corrupt"):
            store.get("a", "b")
        with pytest.raises(CredentialStoreError):
            store.set("a", "b", {"k": "v"})
        with pytest.raises(CredentialStoreError):
            store.delete("a", "b")
        assert path.read_text() == "{not json"

    def test_non_object_json_raises(self, store, tmp_path):
        (tmp_path / "credentials.json").write_text("[]")
        with pytest.raises(CredentialStoreError):
            store.get("a", "b")

    def test_parent_dir_created_0700_and_no_tmp_left(self, fernet_key, tmp_path):
        path = tmp_path / "sub" / "credentials.json"
        EncryptedFileStore(path=path, fernet_key=fernet_key).set("a", "b", {"k": "v"})
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not list(path.parent.glob("*.tmp"))

    def test_concurrent_sets_do_not_lose_updates(self, store):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda i: store.set("s", f"a{i}", {"i": i}), range(24)))
        assert all(store.get("s", f"a{i}") == {"i": i} for i in range(24))
