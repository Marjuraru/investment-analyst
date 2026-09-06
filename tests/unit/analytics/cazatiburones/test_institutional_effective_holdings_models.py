"""Unit tests for institutional effective holdings models."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_effective_holdings_models import (
    InstitutionalEffectiveContributor,
    InstitutionalEffectiveHoldingsQuery,
    InstitutionalEffectiveHoldingsResult,
)


def test_effective_holdings_contract_is_strict_versioned_and_bounded() -> None:
    now = datetime(2025, 2, 14, tzinfo=UTC)
    query = InstitutionalEffectiveHoldingsQuery(
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        known_at=now,
        offset=0,
        limit=100,
    )
    assert query.limit == 100

    # Forbid extra fields
    with pytest.raises(ValidationError, match="extra_forbidden"):
        InstitutionalEffectiveHoldingsQuery(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=now,
            extra="forbidden",  # type: ignore[call-arg]
        )

    # Upper bound on limit
    with pytest.raises(ValidationError):
        InstitutionalEffectiveHoldingsQuery(
            manager_cik="0001067983",
            report_period=date(2024, 12, 31),
            known_at=now,
            limit=20_000,
        )


def test_query_rejects_non_normalized_manager_cik() -> None:
    with pytest.raises(ValidationError, match="normalized"):
        InstitutionalEffectiveHoldingsQuery(
            manager_cik="1067983",
            report_period=date(2024, 12, 31),
            known_at=datetime(2025, 2, 14, tzinfo=UTC),
        )


def test_unresolved_monetary_scale_has_no_composed_money_total() -> None:
    now = datetime(2025, 2, 14, tzinfo=UTC)
    result = InstitutionalEffectiveHoldingsResult(
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        known_at=now,
        status="effective",
        reason="effective_rows_empty",
        total_rows=0,
        offset=0,
        limit=1000,
        truncated=False,
    )
    assert result.monetary_scale_status == "unresolved"
    assert not hasattr(result, "composed_money_total")
    assert not hasattr(result, "total_value")


def test_confidential_and_combination_limitations_remain_explicit() -> None:
    now = datetime(2025, 2, 14, tzinfo=UTC)
    contributor = InstitutionalEffectiveContributor(
        artifact_id=uuid4(),
        accession="0000950123-25-000001",
        is_amendment=False,
        available_at=now,
        report_type="13F COMBINATION REPORT",
        confidential_omitted=True,
        other_manager_count=2,
    )
    assert contributor.confidential_omitted is True
    assert contributor.report_type == "13F COMBINATION REPORT"
    assert contributor.other_manager_count == 2
