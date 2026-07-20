"""Tests for the bounded read-only Apple fundamental-trend contract."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.fundamental_trend_models import (
    AaplFundamentalTrendRequest,
)
from investment_analyst.analytics.fundamental_trend_service import (
    AaplFundamentalTrendQueryError,
    AaplFundamentalTrendService,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_fact_models import COMPANYFACTS_SOURCE_ID
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecFundamentalQueryError,
)
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPeriodView,
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
    SecSelectedFundamentalFact,
    allowed_sec_fundamental_fields,
)

_KNOWN_AT = datetime(2026, 7, 19, 18, 8, 17, tzinfo=UTC)
_PERIOD_END = datetime(2026, 3, 28, tzinfo=UTC)


class _FakeFundamentals:
    def __init__(self, result: SecFundamentalPointInTimeResult) -> None:
        self._result = result
        self.requests: list[SecFundamentalQuery] = []

    def query(self, request: SecFundamentalQuery) -> SecFundamentalPointInTimeResult:
        self.requests.append(request)
        return self._result.model_copy(update={"query": request})


class _ExplodingFundamentals:
    def query(self, request: SecFundamentalQuery) -> SecFundamentalPointInTimeResult:
        del request
        raise SecFundamentalQueryError("malformed secret detail")


def _fact(field_name: str, index: int) -> SecSelectedFundamentalFact:
    return SecSelectedFundamentalFact(
        observation_id=UUID(int=index * 2 + 1),
        raw_record_id=UUID(int=index * 2 + 2),
        field_name=field_name,
        value=Decimal(index + 1) * Decimal("1000000000"),
        unit="USD",
        frequency=DataFrequency.QUARTERLY,
        period_end=_PERIOD_END,
        available_at=datetime(2026, 5, 1, tzinfo=UTC),
        normalized_at=datetime(2026, 7, 14, tzinfo=UTC),
        accession_number="0000320193-26-000001",
        taxonomy="us-gaap",
        tag=f"Tag{index}",
        form="10-Q",
        fiscal_year="2026",
        fiscal_period="Q2",
        source_id=COMPANYFACTS_SOURCE_ID,
        record_key=f"record-{index}",
        superseded_count=0,
    )


def _result(*, populated: bool) -> SecFundamentalPointInTimeResult:
    query = SecFundamentalQuery(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.QUARTERLY,
        limit=8,
    )
    periods: tuple[SecFundamentalPeriodView, ...] = ()
    if populated:
        facts = tuple(
            sorted(
                (
                    _fact(field_name, index)
                    for index, field_name in enumerate(allowed_sec_fundamental_fields())
                ),
                key=lambda fact: fact.field_name,
            )
        )
        periods = (
            SecFundamentalPeriodView(
                period_end=_PERIOD_END,
                frequency=DataFrequency.QUARTERLY,
                facts=facts,
                missing_fields=(),
                available_fields=tuple(fact.field_name for fact in facts),
                is_complete=True,
                latest_available_at=max(fact.available_at for fact in facts),
            ),
        )
    return SecFundamentalPointInTimeResult(
        query=query,
        periods=periods,
        observations_examined=len(periods) * 5,
        observations_eligible=len(periods) * 5,
        observations_selected=len(periods) * 5,
        observations_superseded=0,
        periods_returned=len(periods),
        earliest_period_end=_PERIOD_END if periods else None,
        latest_period_end=_PERIOD_END if periods else None,
        latest_period_complete=bool(periods),
        traceability_verified=True,
    )


def test_trend_preserves_exact_sec_facts_and_point_in_time_scope() -> None:
    fundamentals = _FakeFundamentals(_result(populated=True))
    request = AaplFundamentalTrendRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.QUARTERLY,
        period_limit=8,
    )

    trend = AaplFundamentalTrendService(fundamentals).query(request)

    assert trend.schema_version == "aapl-fundamental-trend-v1"
    assert trend.source_id == COMPANYFACTS_SOURCE_ID
    assert trend.coverage.periods_returned == 1
    assert trend.coverage.latest_period_complete
    expected_values = {
        field_name: Decimal(index + 1) * Decimal("1000000000")
        for index, field_name in enumerate(allowed_sec_fundamental_fields())
    }
    assert {fact.field_name: fact.value for fact in trend.periods[0].facts} == expected_values
    assert {
        fact["field_name"]: fact["value"] for fact in trend.to_json_dict()["periods"][0]["facts"]
    } == {field_name: str(value) for field_name, value in expected_values.items()}
    assert fundamentals.requests == [
        SecFundamentalQuery(
            known_at=_KNOWN_AT,
            frequency=DataFrequency.QUARTERLY,
            limit=8,
        )
    ]


def test_trend_preserves_empty_history_without_fabricating_periods() -> None:
    trend = AaplFundamentalTrendService(_FakeFundamentals(_result(populated=False))).query(
        AaplFundamentalTrendRequest(
            known_at=_KNOWN_AT,
            frequency=DataFrequency.QUARTERLY,
            period_limit=8,
        )
    )

    assert trend.periods == ()
    assert trend.coverage.periods_returned == 0
    assert not trend.coverage.latest_period_complete
    assert trend.traceability_verified


def test_trend_translates_sec_query_failure_without_leaking_detail() -> None:
    request = AaplFundamentalTrendRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.QUARTERLY,
        period_limit=8,
    )

    with pytest.raises(AaplFundamentalTrendQueryError) as captured:
        AaplFundamentalTrendService(_ExplodingFundamentals()).query(request)

    assert str(captured.value) == "stored SEC fundamentals could not be presented"
    assert "secret" not in str(captured.value)


def test_trend_request_rejects_unsupported_frequency_and_boolean_limit() -> None:
    with pytest.raises(ValidationError, match="annual or quarterly"):
        AaplFundamentalTrendRequest(
            known_at=_KNOWN_AT,
            frequency=DataFrequency.DAY_1,
            period_limit=8,
        )
    with pytest.raises(ValidationError, match="integer"):
        AaplFundamentalTrendRequest(
            known_at=_KNOWN_AT,
            frequency=DataFrequency.QUARTERLY,
            period_limit=True,
        )
