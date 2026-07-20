"""Tests for deterministic historical fundamental research statistics."""

from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.fundamentals.research_history_models import (
    HISTORY_ALGORITHM_VERSION,
    AaplFundamentalResearchHistoryResult,
)
from investment_analyst.analytics.fundamentals.research_history_service import (
    AaplFundamentalResearchHistoryService,
)
from investment_analyst.analytics.fundamentals.research_models import (
    FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS,
    AaplFundamentalResearchCoverage,
    AaplFundamentalResearchPeriod,
    AaplFundamentalResearchRequest,
    AaplFundamentalResearchResult,
    FundamentalResearchMetricInput,
    FundamentalResearchMetricValue,
    get_fundamental_research_metric_definition,
)
from investment_analyst.core.models import DataFrequency


class _Research:
    def __init__(self, result: AaplFundamentalResearchResult) -> None:
        self.result = result
        self.requests: list[AaplFundamentalResearchRequest] = []

    def query(
        self,
        request: AaplFundamentalResearchRequest,
    ) -> AaplFundamentalResearchResult:
        self.requests.append(request)
        assert request == self.result.request
        return self.result


def _metric(
    metric_key: str,
    value: str,
    period_end: datetime,
    frequency: DataFrequency,
) -> FundamentalResearchMetricValue:
    definition = get_fundamental_research_metric_definition(metric_key)
    available_at = period_end + timedelta(days=35)
    inputs = tuple(
        FundamentalResearchMetricInput(
            role=field.role,
            field_name=field.field_name,
            observation_id=uuid5(
                NAMESPACE_URL,
                f"{metric_key}:{period_end.isoformat()}:{field.role}",
            ),
            value=Decimal("1"),
            available_at=available_at,
        )
        for field in definition.input_fields
    )
    return FundamentalResearchMetricValue(
        metric_key=metric_key,
        display_name_es=definition.display_name_es,
        value=Decimal(value),
        unit=definition.unit,
        frequency=frequency,
        period_end=period_end,
        available_at=available_at,
        formula=definition.formula,
        algorithm_version=definition.algorithm_version,
        inputs=inputs,
        limitations=definition.limitations,
    )


def _research_result(
    specs: tuple[tuple[str, str, datetime], ...],
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
) -> AaplFundamentalResearchResult:
    request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=frequency,
        limit=5,
    )
    grouped: dict[datetime, list[FundamentalResearchMetricValue]] = {}
    metric_counts = {
        definition.metric_key: 0 for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
    }
    for metric_key, value, period_end in specs:
        grouped.setdefault(period_end, []).append(_metric(metric_key, value, period_end, frequency))
        metric_counts[metric_key] += 1
    periods = tuple(
        AaplFundamentalResearchPeriod(
            period_end=period_end,
            frequency=frequency,
            metrics=tuple(sorted(metrics, key=lambda item: item.metric_key)),
        )
        for period_end, metrics in sorted(grouped.items())
    )
    return AaplFundamentalResearchResult(
        request=request,
        periods=periods,
        coverage=AaplFundamentalResearchCoverage(
            observations_examined=30,
            observations_eligible=len(specs),
            observations_selected=len(specs),
            observations_superseded=0,
            source_periods=len(periods),
            output_periods=len(periods),
            metrics_returned=len(specs),
            metric_counts=metric_counts,
            skipped_counts={},
            earliest_period_end=periods[0].period_end if periods else None,
            latest_period_end=periods[-1].period_end if periods else None,
        ),
    )


def test_annual_history_calculates_changes_dispersion_and_elapsed_day_cagr() -> None:
    dates = (
        datetime(2022, 9, 24, tzinfo=UTC),
        datetime(2023, 9, 30, tzinfo=UTC),
        datetime(2024, 9, 28, tzinfo=UTC),
    )
    free_cash_flow = "fundamental.research.free_cash_flow"
    gross_margin = "fundamental.research.gross_margin"
    research = _research_result(
        (
            (free_cash_flow, "100", dates[0]),
            (gross_margin, "0.40", dates[0]),
            (free_cash_flow, "121", dates[1]),
            (gross_margin, "0.42", dates[1]),
            (free_cash_flow, "146.41", dates[2]),
            (gross_margin, "0.45", dates[2]),
        )
    )

    result = AaplFundamentalResearchHistoryService(_Research(research)).query(research.request)
    histories = {item.metric_key: item for item in result.series}
    cash_statistics = histories[free_cash_flow].statistics
    margin_statistics = histories[gross_margin].statistics
    elapsed_days = (dates[-1].date() - dates[0].date()).days
    with localcontext(Context(prec=34)):
        expected_cagr = (Decimal("146.41") / Decimal("100")) ** (
            Decimal("365.2425") / Decimal(elapsed_days)
        ) - 1

    assert cash_statistics.latest_change_from_previous_available == Decimal("25.41")
    assert cash_statistics.latest_change_rate_from_previous_available == Decimal("0.21")
    assert cash_statistics.horizon_change == Decimal("46.41")
    assert cash_statistics.horizon_change_rate == Decimal("0.4641")
    assert cash_statistics.compound_annual_growth_rate == expected_cagr
    assert cash_statistics.arithmetic_mean == Decimal("122.47")
    assert cash_statistics.range == Decimal("46.41")
    assert cash_statistics.elapsed_days == elapsed_days
    assert margin_statistics.latest_change_from_previous_available == Decimal("0.03")
    assert margin_statistics.horizon_change == Decimal("0.05")
    assert margin_statistics.latest_change_rate_from_previous_available is None
    assert margin_statistics.horizon_change_rate is None
    assert margin_statistics.compound_annual_growth_rate is None
    assert result.coverage.series_returned == 2
    assert result.coverage.points_returned == 6
    assert result.coverage.series_with_previous_comparison == 2
    assert result.coverage.series_with_cagr == 1
    assert result.traceability_verified


def test_quarterly_usd_history_does_not_annualize_seasonal_values() -> None:
    metric_key = "fundamental.research.free_cash_flow"
    research = _research_result(
        (
            (metric_key, "100", datetime(2025, 3, 29, tzinfo=UTC)),
            (metric_key, "110", datetime(2025, 6, 28, tzinfo=UTC)),
            (metric_key, "121", datetime(2025, 9, 27, tzinfo=UTC)),
        ),
        frequency=DataFrequency.QUARTERLY,
    )

    result = AaplFundamentalResearchHistoryService(_Research(research)).query(research.request)
    statistics = result.series[0].statistics

    assert statistics.latest_change_rate_from_previous_available == Decimal("0.1")
    assert statistics.horizon_change_rate == Decimal("0.21")
    assert statistics.compound_annual_growth_rate is None
    assert result.coverage.series_with_cagr == 0


def test_non_positive_currency_comparisons_keep_absolute_changes_only() -> None:
    metric_key = "fundamental.research.working_capital"
    research = _research_result(
        (
            (metric_key, "-10", datetime(2023, 9, 30, tzinfo=UTC)),
            (metric_key, "-5", datetime(2024, 9, 28, tzinfo=UTC)),
        )
    )

    result = AaplFundamentalResearchHistoryService(_Research(research)).query(research.request)
    statistics = result.series[0].statistics

    assert statistics.latest_change_from_previous_available == Decimal("5")
    assert statistics.horizon_change == Decimal("5")
    assert statistics.latest_change_rate_from_previous_available is None
    assert statistics.horizon_change_rate is None
    assert statistics.compound_annual_growth_rate is None


def test_single_point_history_has_no_comparison_and_preserves_exact_json() -> None:
    metric_key = "fundamental.research.current_ratio"
    research = _research_result(((metric_key, "0.893200", datetime(2025, 9, 27, tzinfo=UTC)),))

    result = AaplFundamentalResearchHistoryService(_Research(research)).query(research.request)
    statistics = result.series[0].statistics
    payload = result.to_json_dict()

    assert statistics.previous_period_end is None
    assert statistics.latest_change_from_previous_available is None
    assert statistics.horizon_change is None
    assert statistics.elapsed_days == 0
    assert payload["schema_version"] == "aapl-fundamental-research-history-v1"
    assert payload["series"][0]["points"][0]["value"] == "0.893200"
    assert payload["series"][0]["statistics"]["arithmetic_mean"] == "0.893200"
    assert payload["series"][0]["statistics"]["algorithm_version"] == (HISTORY_ALGORITHM_VERSION)
    assert payload["research"]["traceability_verified"] is True


def test_history_contract_rejects_tampered_statistics() -> None:
    metric_key = "fundamental.research.free_cash_flow"
    research = _research_result(
        (
            (metric_key, "100", datetime(2024, 9, 28, tzinfo=UTC)),
            (metric_key, "121", datetime(2025, 9, 27, tzinfo=UTC)),
        )
    )
    result = AaplFundamentalResearchHistoryService(_Research(research)).query(research.request)
    payload = result.to_json_dict()
    payload["series"][0]["statistics"]["arithmetic_mean"] = "110"

    with pytest.raises(ValidationError, match="calculations are inconsistent"):
        AaplFundamentalResearchHistoryResult.model_validate(payload)


def test_history_revalidation_is_independent_of_ambient_decimal_precision() -> None:
    metric_key = "fundamental.research.free_cash_flow"
    research = _research_result(
        (
            (
                metric_key,
                "0.1234567890123456789012345678901234",
                datetime(2024, 9, 28, tzinfo=UTC),
            ),
            (
                metric_key,
                "0.9876543210987654321098765432109876",
                datetime(2025, 9, 27, tzinfo=UTC),
            ),
        )
    )

    with localcontext(Context(prec=28)):
        result = AaplFundamentalResearchHistoryService(_Research(research)).query(research.request)

    assert result.series[0].statistics.latest_change_from_previous_available == Decimal(
        "0.8641975320864197532086419753208642"
    )
