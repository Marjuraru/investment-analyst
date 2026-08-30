from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.analytics.cazatiburones.beneficial_ownership_change_engine import (
    calculate_beneficial_features,
)
from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipStatement


def _statement(
    *,
    available_at: datetime,
    event_date: date | None,
    shares: Decimal | None,
    percent: Decimal | None,
    form: str = "SC 13G",
    asset_id: str = "equity:us:aapl",
) -> BeneficialOwnershipStatement:
    statement_id = uuid4()
    return BeneficialOwnershipStatement.model_construct(
        statement_id=statement_id,
        asset_id=asset_id,
        document_revision=SimpleNamespace(revision_id=uuid4()),
        form=form,
        subject_cik="0000320193",
        reporting_person_cik="0000000002",
        event_date=event_date,
        shares_beneficially_owned=shares,
        percent_of_class=percent,
        available_at=available_at,
    )


def _metrics(result) -> dict[str, object]:
    return {metric.key: metric.value for metric in result.metrics}


def _statuses(result) -> dict[str, str]:
    return {metric.key: metric.status for metric in result.metrics}


def test_beneficial_engine_uses_consecutive_declared_values_and_literal_form() -> None:
    first = _statement(
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        event_date=date(2025, 1, 20),
        shares=Decimal("100"),
        percent=Decimal("4"),
    )
    second = _statement(
        available_at=datetime(2025, 2, 10, tzinfo=UTC),
        event_date=date(2025, 2, 2),
        shares=Decimal("150"),
        percent=Decimal("5"),
        form="SC 13G/A",
    )

    first_result, second_result = calculate_beneficial_features((first, second))

    assert first_result.comparison_status == "not_evaluable"
    assert _statuses(first_result)["delta_percent_of_class"] == "not_evaluable"
    assert second_result.declared_nature == "SC 13G/A"
    assert second_result.available_at == second.available_at
    assert second_result.revision_ids == (
        str(first.document_revision.revision_id),
        str(second.document_revision.revision_id),
    )
    assert _metrics(second_result) == {
        "delta_percent_of_class": Decimal("1"),
        "delta_shares_beneficially_owned": Decimal("50"),
        "threshold_appearance": False,
        "threshold_exit": False,
        "is_amendment": True,
        "filing_delay_days": Decimal("8"),
    }


def test_beneficial_engine_marks_absent_values_and_date_missing() -> None:
    first = _statement(
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        event_date=date(2025, 1, 20),
        shares=Decimal("100"),
        percent=Decimal("4"),
    )
    second = _statement(
        available_at=datetime(2025, 2, 10, tzinfo=UTC),
        event_date=None,
        shares=None,
        percent=None,
    )

    _, result = calculate_beneficial_features((first, second))

    statuses = _statuses(result)
    assert statuses["delta_percent_of_class"] == "missing"
    assert statuses["delta_shares_beneficially_owned"] == "missing"
    assert statuses["filing_delay_days"] == "missing"
    assert _metrics(result)["threshold_exit"] is True


def test_beneficial_engine_rejects_cross_asset_input() -> None:
    first = _statement(
        available_at=datetime(2025, 2, 1, tzinfo=UTC),
        event_date=date(2025, 1, 20),
        shares=Decimal("100"),
        percent=Decimal("4"),
    )
    second = _statement(
        available_at=datetime(2025, 2, 10, tzinfo=UTC),
        event_date=date(2025, 2, 2),
        shares=Decimal("150"),
        percent=Decimal("5"),
        asset_id="equity:us:msft",
    )

    with pytest.raises(ValueError, match="one asset"):
        calculate_beneficial_features((first, second))
