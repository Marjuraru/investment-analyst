from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.insider_activity_engine import (
    calculate_insider_features,
)
from investment_analyst.evidence.sec_ownership.models import OwnershipEntry, OwnershipStatement


def _statement(
    *,
    available_at: datetime,
    transaction_date: date | None,
    shares: Decimal | None,
    following: Decimal | None,
    code: str | None,
    asset_id: str = "equity:us:aapl",
) -> OwnershipStatement:
    statement_id = uuid4()
    entry = OwnershipEntry.model_construct(
        entry_id=uuid4(),
        table="non_derivative",
        kind="transaction",
        ordinal=0,
        owner_cik="0000000001",
        security_title="Common Stock",
        transaction_date=transaction_date,
        acquired_disposed=code,
        shares=shares,
        shares_owned_following=following,
    )
    return OwnershipStatement.model_construct(
        statement_id=statement_id,
        asset_id=asset_id,
        document_revision=SimpleNamespace(revision_id=uuid4()),
        form="4",
        period_of_report=transaction_date or date(2025, 1, 20),
        available_at=available_at,
        entries=(entry,),
    )


def _metrics(result) -> dict[str, object]:
    return {metric.key: metric.value for metric in result.metrics}


def _statuses(result) -> dict[str, str]:
    return {metric.key: metric.status for metric in result.metrics}


def test_insider_engine_uses_declared_holdings_and_same_participant_cluster() -> None:
    first = _statement(
        available_at=datetime(2025, 1, 13, tzinfo=UTC),
        transaction_date=date(2025, 1, 10),
        shares=Decimal("10"),
        following=Decimal("100"),
        code="A",
    )
    second = _statement(
        available_at=datetime(2025, 1, 23, tzinfo=UTC),
        transaction_date=date(2025, 1, 20),
        shares=Decimal("25"),
        following=Decimal("125"),
        code="D",
    )

    first_result, second_result = calculate_insider_features((first, second))

    assert first_result.comparison_status == "not_evaluable"
    assert _statuses(first_result)["holding_delta_ratio"] == "not_evaluable"
    assert second_result.available_at == second.available_at
    assert second_result.revision_ids == (
        str(first.document_revision.revision_id),
        str(second.document_revision.revision_id),
    )
    assert _metrics(second_result) == {
        "transaction_shares": Decimal("25"),
        "prior_holding": Decimal("100"),
        "post_holding": Decimal("125"),
        "holding_delta_ratio": Decimal("0.25"),
        "acquisition_count": Decimal("0"),
        "disposition_count": Decimal("1"),
        "clustered_transaction_count": Decimal("2"),
        "participant_recurrence": Decimal("2"),
        "is_amendment": False,
        "filing_delay_days": Decimal("3"),
    }


def test_insider_engine_keeps_missing_date_and_zero_prior_distinct_from_zero() -> None:
    first = _statement(
        available_at=datetime(2025, 1, 13, tzinfo=UTC),
        transaction_date=date(2025, 1, 10),
        shares=Decimal("10"),
        following=Decimal("0"),
        code="A",
    )
    second = _statement(
        available_at=datetime(2025, 1, 23, tzinfo=UTC),
        transaction_date=None,
        shares=None,
        following=Decimal("5"),
        code=None,
    )

    _, result = calculate_insider_features((first, second))

    statuses = _statuses(result)
    assert statuses["transaction_shares"] == "missing"
    assert statuses["holding_delta_ratio"] == "missing"
    assert statuses["filing_delay_days"] == "missing"
    assert statuses["clustered_transaction_count"] == "missing"


def test_insider_engine_rejects_cross_asset_input() -> None:
    first = _statement(
        available_at=datetime(2025, 1, 13, tzinfo=UTC),
        transaction_date=date(2025, 1, 10),
        shares=Decimal("10"),
        following=Decimal("100"),
        code="A",
    )
    second = _statement(
        available_at=datetime(2025, 1, 23, tzinfo=UTC),
        transaction_date=date(2025, 1, 20),
        shares=Decimal("10"),
        following=Decimal("110"),
        code="A",
        asset_id="equity:us:msft",
    )

    with pytest.raises(ValueError, match="one asset"):
        calculate_insider_features((first, second))
