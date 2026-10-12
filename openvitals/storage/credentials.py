"""
CredentialStore — pluggable credential storage.

``CredentialStore`` is the ABC.  ``EncryptedFileStore`` is the default
implementation using Fernet symmetric encryption.

Encryption key: read from the env var ``OPENVITALS_ENCRYPTION_KEY``.
Generate a key with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Then set::

    export OPENVITALS_ENCRYPTION_KEY=<key>

The store file lives at ``~/.openvitals/credentials.json`` by default (this
path is gitignored).  The file is written with mode 0600 (owner read/write
only) — never commit it.

Rule: NEVER log credential values (access tokens, refresh tokens, passwords).
Use ``[[ -n "$OPENVITALS_ENCRYPTION_KEY" ]] && echo set`` to check existence.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from openvitals.errors import OpenVitalsError

try:
    import fcntl
except ImportError:  # ponytail: no locking on Windows; single-writer assumed there
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

ENV_VAR_FERNET_KEY = "OPENVITALS_ENCRYPTION_KEY"
"""Environment variable that holds the Fernet key for EncryptedFileStore."""


class EncryptionKeyMissingError(RuntimeError):
    """
    Raised when ``OPENVITALS_ENCRYPTION_KEY`` is not set in the environment.
    This is a configuration error — the caller must set the env var before
    constructing an ``EncryptedFileStore``.
    """


class CredentialStoreError(OpenVitalsError):
    """The credential store is unreadable, corrupt, or cannot be decrypted with the key."""


class CredentialStore(ABC):
    """
    Pluggable credential storage interface.

    Keys are ``(source, account_ref)`` tuples.  Values are arbitrary dicts
    (OAuth token bundles, API keys, session cookies — whatever the connector
    needs).

    Consumers may supply their own implementation of this ABC (e.g. a
    database-backed store); ``EncryptedFileStore`` is the default for
    local/self-hosted use.
    """

    @abstractmethod
    def get(self, source: str, account_ref: str) -> dict | None:
        """
        Return the stored credential dict for ``(source, account_ref)``, or
        ``None`` if no credential exists.
        """

    @abstractmethod
    def set(self, source: str, account_ref: str, credential: dict) -> None:
        """
        Persist ``credential`` for ``(source, account_ref)``.
        Overwrites any existing entry.  Must write atomically enough that a
        crash mid-write doesn't corrupt the store.
        NEVER log ``credential`` contents.
        """

    @abstractmethod
    def delete(self, source: str, account_ref: str) -> None:
        """
        Remove the credential for ``(source, account_ref)``.
        No-op if not found.
        """


# ---------------------------------------------------------------------------
# Default implementation: Fernet-encrypted JSON file
# ---------------------------------------------------------------------------


class EncryptedFileStore(CredentialStore):
    """
    Fernet-encrypted JSON credential store backed by a local file.

    The file is a JSON object mapping ``"<source>:<account_ref>"`` →
    Fernet-encrypted credential blob (bytes, base64-encoded inside JSON as a
    string).  The file is written with 0600 permissions.

    Encryption key source: ``OPENVITALS_ENCRYPTION_KEY`` env var (URL-safe
    base64-encoded 32-byte key).  See module docstring for how to generate one.

    The default store path is ``~/.openvitals/credentials.json``; override via
    the ``path`` constructor argument.  This path is gitignored — never commit.
    """

    DEFAULT_PATH = Path.home() / ".openvitals" / "credentials.json"

    def __init__(
        self,
        path: Path | None = None,
        fernet_key: bytes | str | None = None,
    ) -> None:
        """
        Parameters
        ----------
        path:
            Path to the encrypted credential file.  Defaults to
            ``~/.openvitals/credentials.json``.
        fernet_key:
            Fernet key (bytes or str).  If None, read from the
            ``OPENVITALS_ENCRYPTION_KEY`` environment variable.
        """
        self._path = path or self.DEFAULT_PATH

        if fernet_key is None:
            raw_key = os.environ.get(ENV_VAR_FERNET_KEY)
            if not raw_key:
                raise EncryptionKeyMissingError(
                    f"Encryption key not found.  Set {ENV_VAR_FERNET_KEY} env var.  "
                    "Generate a key with: "
                    "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )
            fernet_key = raw_key

        if isinstance(fernet_key, str):
            fernet_key = fernet_key.encode()

        self._fernet = Fernet(fernet_key)

    # --- Internal helpers ---------------------------------------------------

    def _load(self) -> dict[str, str]:
        """Load the raw (encrypted blobs) JSON dict from disk.  Returns {} only if the file is missing."""
        try:
            with self._path.open("r") as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(
                f"Credential store at {self._path} is corrupt ({type(exc).__name__}); "
                "refusing to continue so other accounts are not overwritten."
            ) from exc
        except OSError as exc:
            raise CredentialStoreError(
                f"Could not read credential store at {self._path} ({type(exc).__name__})."
            ) from exc
        if not isinstance(data, dict):
            raise CredentialStoreError(f"Credential store at {self._path} is corrupt (not a JSON object).")
        return data

    def _save(self, data: dict[str, str]) -> None:
        """Atomically write the encrypted dict to disk with 0600 permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkstemp creates the file 0600 with a unique name in the same dir
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    @contextmanager
    def _locked(self):
        """Serialise read-modify-write across processes via flock on a sibling lockfile."""
        if fcntl is None:
            yield
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self._path.with_name(self._path.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    @staticmethod
    def _key(source: str, account_ref: str) -> str:
        return f"{source}:{account_ref}"

    # --- CredentialStore interface ------------------------------------------

    def get(self, source: str, account_ref: str) -> dict | None:
        data = self._load()
        blob = data.get(self._key(source, account_ref))
        if blob is None:
            return None
        try:
            decrypted = self._fernet.decrypt(blob.encode())
        except InvalidToken as exc:
            raise CredentialStoreError(
                "Could not decrypt credential: wrong or rotated encryption key "
                f"({ENV_VAR_FERNET_KEY}), or the stored blob is corrupt."
            ) from exc
        return json.loads(decrypted)

    def set(self, source: str, account_ref: str, credential: dict) -> None:
        # NEVER log credential contents
        plaintext = json.dumps(credential).encode()
        encrypted = self._fernet.encrypt(plaintext).decode()
        with self._locked():
            data = self._load()
            data[self._key(source, account_ref)] = encrypted
            self._save(data)

    def delete(self, source: str, account_ref: str) -> None:
        with self._locked():
            data = self._load()
            data.pop(self._key(source, account_ref), None)
            self._save(data)
