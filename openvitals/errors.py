"""
OpenVitals typed error taxonomy.

Per connector-contract.md §4 — callers react to these types, not to generic
exceptions.  All errors derive from ``OpenVitalsError`` so callers can catch
the whole family if needed.
"""

from __future__ import annotations


class OpenVitalsError(Exception):
    """Base class for all OpenVitals errors."""


class AuthRequired(OpenVitalsError):
    """
    The connector has no valid credentials for this account.
    Caller should initiate the auth flow and retry.
    """


class MissingScope(OpenVitalsError):
    """
    The granted OAuth scopes are insufficient for the requested operation.
    ``required_scope`` is the missing scope string; ``granted_scopes`` is the
    set the provider actually returned.
    """

    def __init__(
        self,
        message: str,
        *,
        required_scope: str | None = None,
        granted_scopes: set[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.required_scope = required_scope
        self.granted_scopes = granted_scopes or set()


class RateLimited(OpenVitalsError):
    """
    The provider is rate-limiting requests.
    ``retry_after`` is the number of seconds to wait before retrying, if the
    provider supplies it (else None).  Callers should apply bounded backoff.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(OpenVitalsError):
    """
    The provider's API is unreachable or returned a 5xx.
    Transient — callers should retry with backoff.
    """


class ProviderPayloadError(OpenVitalsError):
    """
    The provider returned an unexpected payload shape that the connector
    cannot parse.  Likely a provider-side change.  Not retryable without a
    code fix.
    """


class NoData(OpenVitalsError):
    """
    The provider has no records for the requested range / account.
    This is a normal result, not a failure — callers should not retry.
    """


class PartialFetchError(OpenVitalsError):
    """
    The fetch succeeded partially: some pages/records were retrieved but at
    least one failed mid-stream.  The caller should NOT advance its watermark
    past the gap.

    ``records_fetched`` is the count of records that were successfully yielded
    before the error.  ``cause`` is the underlying exception.
    """

    def __init__(
        self,
        message: str,
        *,
        records_fetched: int = 0,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.records_fetched = records_fetched
        self.cause = cause
