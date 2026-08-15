"""Focused PIT and Decimal tests for descriptive valuation rules."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from investment_analyst.analytics.valuation.history_service import CorporateValuationHistoryService
from investment_analyst.analytics.valuation.rule_models import (
    CorporateValuationHistoryRule,
    CorporateValuationHistoryRuleRequest,
)
from investment_analyst.analytics.valuation.rule_service import (
    CorporateValuationHistoryRuleError,
    CorporateValuationHistoryRuleService,
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


def _result(identifier: int, value: str, day: int, *, known_day: int | None = None) -> MetricResult:
    return MetricResult(
        result_id=UUID(int=identifier),
        asset_id="equity:us:aapl",
        metric_key="valuation.corporate.price_to_book",
        value=Decimal(value),
        unit="ratio",
        as_of=datetime(2026, 1, day, tzinfo=UTC),
        available_at=datetime(2026, 1, day, tzinfo=UTC),
        computed_at=datetime(2026, 1, day, 1, tzinfo=UTC),
        parameters={
            "category": "valuation",
            "basis": "latest_annual",
            "known_at": f"2026-01-{known_day or day:02d}T00:00:00Z",
            "valuation_date": f"2026-01-{day:02d}",
            "annual_period_end": "2025-09-30T00:00:00Z",
            "security_basis_version": "v1",
        },
        input_observation_ids=[UUID(int=999)],
        algorithm_version="corporate-valuation-latest-annual-v1-decimal34",
        quality=DataQuality.VALID,
    )


def _request(**updates: object) -> CorporateValuationHistoryRuleRequest:
    rule = CorporateValuationHistoryRule(
        rule_id="valuation.price-to-book.low",
        rule_version="v1",
        name="Percentil histórico",
        limitations=("Contexto descriptivo.",),
        metric_key="valuation.corporate.price_to_book",
        operator="at_or_below_empirical_percentile",
        threshold=Decimal("0.7"),
        minimum_prior_points=3,
    )
    return CorporateValuationHistoryRuleRequest(
        asset_id="equity:us:aapl",
        known_at=datetime(2026, 1, 10, tzinfo=UTC),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 9),
        rule=rule,
    ).model_copy(update=updates)


def _service(results: list[MetricResult]) -> CorporateValuationHistoryRuleService:
    return CorporateValuationHistoryRuleService(CorporateValuationHistoryService(_Storage(results)))


def test_midrank_uses_only_prior_points_and_is_repeatable() -> None:
    service = _service(
        [_result(1, "1", 1), _result(2, "2", 2), _result(3, "2", 3), _result(4, "2", 4)]
    )
    first = service.query(_request())
    second = service.query(_request())
    assert first.status == "met"
    assert first.empirical_percentile == Decimal("0.6666666666666666666666666666666667")
    assert (first.lower_count, first.equal_count, first.greater_count) == (1, 2, 0)
    assert [point.result_id for point in first.reference_points] == [
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
    ]
    assert first.result_id == second.result_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_insufficient_history_is_not_evaluable_without_imputation() -> None:
    result = _service([_result(1, "1", 1), _result(2, "2", 2), _result(3, "3", 3)]).query(
        _request()
    )
    assert result.status == "not_evaluable"
    assert result.empirical_percentile is None
    assert result.coverage.prior_points == 2


def test_future_revision_is_not_used_at_known_cut() -> None:
    older = _result(1, "1", 1)
    revised = _result(2, "9", 1, known_day=11)
    result = _service(
        [older, revised, _result(3, "2", 2), _result(4, "3", 3), _result(5, "4", 4)]
    ).query(_request())
    assert result.reference_points[0].result_id == UUID(int=1)


def test_multiple_semantics_for_requested_metric_fail_closed() -> None:
    second = _result(2, "2", 2)
    second.unit = "USD"
    with pytest.raises(CorporateValuationHistoryRuleError, match="ambiguous"):
        _service([_result(1, "1", 1), second, _result(3, "3", 3), _result(4, "4", 4)]).query(
            _request()
        )


@pytest.mark.parametrize("threshold", (True, 0.5, "NaN", "1.1"))
def test_rule_rejects_non_exact_or_invalid_threshold(threshold: object) -> None:
    with pytest.raises(ValueError):
        CorporateValuationHistoryRule.model_validate(
            {
                "rule_id": "r",
                "rule_version": "v1",
                "name": "n",
                "limitations": ("l",),
                "metric_key": "valuation.corporate.price_to_book",
                "operator": "at_or_below_empirical_percentile",
                "threshold": threshold,
                "minimum_prior_points": 3,
            }
        )
