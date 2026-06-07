"""
Shared pytest fixtures.

``make_record`` is the canonical helper for constructing a minimal valid
``Record`` in tests.  Connector agents should use this rather than
constructing Records by hand — it ensures the envelope fields are always
populated correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from openvitals.schema.records import Record


UTC = timezone.utc


@pytest.fixture
def make_record():
    """
    Factory fixture: ``make_record(record_type, common, **envelope_overrides)``.

    Returns a fully validated ``Record``.  All envelope fields have sensible
    defaults.  ``raw`` defaults to a non-empty dict so the no-data-loss
    invariant is testable.
    """

    def _make(
        record_type,
        common,
        *,
        source: str = "test_source",
        domain: str = "test_domain",
        source_record_id: str = "test_id_001",
        account_ref: str = "acct_001",
        occurred_at: datetime | None = None,
        raw: dict[str, Any] | None = None,
        **kwargs,
    ) -> Record:
        return Record(
            source=source,
            domain=domain,
            record_type=record_type,
            source_record_id=source_record_id,
            account_ref=account_ref,
            occurred_at=occurred_at or datetime(2026, 6, 6, 8, 0, 0, tzinfo=UTC),
            ingested_at=datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC),
            common=common,
            raw=raw if raw is not None else {"_test": True},
            **kwargs,
        )

    return _make
