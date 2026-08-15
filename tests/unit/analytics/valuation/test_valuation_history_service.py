"""Focused read-only contracts for materialized corporate valuation history."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from investment_analyst.analytics.valuation.history_models import CorporateValuationHistoryRequest
from investment_analyst.analytics.valuation.history_service import (
    CorporateValuationHistoryError,
    CorporateValuationHistoryService,
)
from investment_analyst.core.models import DataQuality, MetricResult


class _Results:
    def __init__(self, results: list[MetricResult]) -> None:
        self._results = results

    def list(self, *, asset_id: str | None = None) -> list[MetricResult]:
        return [item for item in self._results if asset_id is None or item.asset_id == asset_id]


class _Storage:
    def __init__(self, results: list[MetricResult]) -> None:
        self.metric_results = _Results(results)

    def require_open(self) -> None:
        return None


def _result(
    *, result_id: int, known_at: str, value: str, valuation_date: str = "2026-01-02"
) -> MetricResult:
    return MetricResult(
        result_id=UUID(int=result_id),
        asset_id="equity:us:aapl",
        metric_key="valuation.corporate.price_to_book",
        value=Decimal(value),
        unit="ratio",
        as_of=datetime.fromisoformat(f"{valuation_date}T00:00:00+00:00"),
        available_at=datetime(2026, 1, 3, tzinfo=UTC),
        computed_at=datetime(2026, 1, 4, tzinfo=UTC),
        parameters={
            "category": "valuation",
            "basis": "latest_annual",
            "known_at": known_at,
            "valuation_date": valuation_date,
            "annual_period_end": "2025-09-30T00:00:00Z",
            "security_basis_version": "v1",
        },
        input_observation_ids=[UUID(int=99)],
        algorithm_version="corporate-valuation-latest-annual-v1-decimal34",
        quality=DataQuality.VALID,
    )


def _request() -> CorporateValuationHistoryRequest:
    return CorporateValuationHistoryRequest(
        asset_id="equity:us:aapl",
        known_at=datetime(2026, 1, 10, tzinfo=UTC),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 3),
    )


def test_history_selects_the_latest_eligible_revision_without_lookahead() -> None:
    history = CorporateValuationHistoryService(
        _Storage(
            [
                _result(result_id=1, known_at="2026-01-04T00:00:00Z", value="2"),
                _result(result_id=2, known_at="2026-01-05T00:00:00Z", value="3"),
                _result(result_id=3, known_at="2026-01-11T00:00:00Z", value="4"),
            ]
        )
    ).query(_request())

    point = history.series[0].points[0]
    assert point.result_id == UUID(int=2)
    assert point.value == Decimal("3")
    assert history.coverage.superseded_revisions == 1
    assert history.series[0].statistics.horizon_change is None


def test_history_keeps_sparse_dates_and_decimal_statistics() -> None:
    history = CorporateValuationHistoryService(
        _Storage(
            [
                _result(
                    result_id=1,
                    known_at="2026-01-04T00:00:00Z",
                    value="2",
                    valuation_date="2026-01-01",
                ),
                _result(
                    result_id=2,
                    known_at="2026-01-05T00:00:00Z",
                    value="5",
                    valuation_date="2026-01-02",
                ),
                _result(
                    result_id=3,
                    known_at="2026-01-06T00:00:00Z",
                    value="8",
                    valuation_date="2026-01-03",
                ),
            ]
        )
    ).query(_request())

    series = history.series[0]
    assert [point.valuation_date for point in series.points] == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    ]
    assert series.statistics.arithmetic_mean == Decimal("5")
    assert series.statistics.previous_change == Decimal("3")
    assert series.statistics.horizon_change == Decimal("6")
    assert history.coverage.returned_points == 3


def test_history_rejects_equally_latest_semantic_conflicts() -> None:
    with pytest.raises(CorporateValuationHistoryError, match="ambiguous"):
        CorporateValuationHistoryService(
            _Storage(
                [
                    _result(result_id=1, known_at="2026-01-05T00:00:00Z", value="2"),
                    _result(result_id=2, known_at="2026-01-05T00:00:00Z", value="3"),
                ]
            )
        ).query(_request())


def test_history_rejects_malformed_persisted_evidence() -> None:
    malformed = _result(result_id=1, known_at="2026-01-05T00:00:00Z", value="2")
    malformed.parameters["known_at"] = "2026-01-05T00:00:00"

    with pytest.raises(CorporateValuationHistoryError):
        CorporateValuationHistoryService(_Storage([malformed])).query(_request())


@pytest.mark.parametrize(
    "payload",
    (
        {"known_at": "2026-01-01T00:00:00", "limit": 3},
        {"known_at": "2026-01-01T00:00:00Z", "limit": True},
    ),
)
def test_request_rejects_naive_cut_and_boolean_limit(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CorporateValuationHistoryRequest.model_validate(
            {
                "asset_id": "equity:us:aapl",
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                **payload,
            }
        )
