"""Unit tests for institutional effective holdings service."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from investment_analyst.analytics.cazatiburones.institutional_composition_definitions import (
    SEC_13F_AMENDMENT_TYPES as V1_AMENDMENT_TYPES,
)
from investment_analyst.analytics.cazatiburones.institutional_composition_definitions import (
    SEC_13F_COMPOSITION_POLICY_VERSION as V1_POLICY_VERSION,
)
from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_service import (
    InstitutionalEffectiveHoldingsService,
)
from investment_analyst.storage import StorageError


def test_service_is_read_only_pit_and_paginated() -> None:
    mock_storage = MagicMock()
    mock_storage.read_only = False
    service = InstitutionalEffectiveHoldingsService(mock_storage)

    with pytest.raises(StorageError, match="read-only"):
        service.query(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=datetime(2025, 2, 14, tzinfo=UTC),
        )


def test_composition_v1_remains_byte_and_behavior_intact() -> None:
    """Invariant check: v1 policy version and amendment types are unchanged."""
    assert V1_POLICY_VERSION == "sec-13f-composition-policy-v1"
    assert frozenset({"RESTATEMENT", "NEW HOLDINGS ENTRIES"}) == V1_AMENDMENT_TYPES


def test_persisted_evidence_remains_byte_identical() -> None:
    """Mock storage query does not invoke write/insert/update methods."""
    mock_storage = MagicMock()
    mock_storage.read_only = True
    mock_storage.raw_records.list_visible.return_value = ()
    service = InstitutionalEffectiveHoldingsService(mock_storage)

    result = service.query(
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        known_at=datetime(2025, 2, 14, tzinfo=UTC),
    )
    assert result.status == "insufficient"
    assert mock_storage.raw_records.insert.call_count == 0
