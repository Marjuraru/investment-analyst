"""Contract tests for unified fundamental-analysis sections."""

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.fundamentals.analysis_models import (
    COMPANY_CATEGORY_DEFINITIONS,
    FUNDAMENTAL_ANALYSIS_LIMITATIONS,
    FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS,
    CompanyClassificationView,
    FundamentalAnalysisCoverage,
    FundamentalAnalysisMetricReference,
    FundamentalAnalysisSectionDefinition,
    FundamentalAnalysisSectionView,
)


def test_analysis_catalog_is_complete_ordered_and_non_overlapping() -> None:
    assert tuple(item.section_key for item in FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS) == (
        "growth_and_per_share",
        "profitability",
        "returns_and_efficiency",
        "earnings_quality",
        "liquidity_and_balance",
        "debt_and_solvency",
        "cash_and_reinvestment",
        "capital_allocation",
    )
    assert tuple(
        len(item.metric_references) for item in FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS
    ) == (5, 5, 6, 2, 4, 11, 5, 2)
    keys = tuple(
        reference.metric_key
        for section in FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS
        for reference in section.metric_references
    )
    assert len(keys) == len(set(keys)) == 40
    assert all(
        item.algorithm_version.endswith("-v1") for item in FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS
    )
    assert "Cada métrica aparece en una sola sección" in FUNDAMENTAL_ANALYSIS_LIMITATIONS[1]


def test_company_category_catalog_is_visible_without_assigning_a_category() -> None:
    assert tuple(item.category_key for item in COMPANY_CATEGORY_DEFINITIONS) == (
        "slow_grower",
        "stalwart",
        "fast_grower",
        "cyclical",
        "turnaround",
        "asset_play",
    )
    assert all(
        item.display_name_es and item.description_es for item in COMPANY_CATEGORY_DEFINITIONS
    )
    with pytest.raises(ValidationError, match="require a selected category"):
        CompanyClassificationView(status="classified")
    with pytest.raises(ValidationError, match="cannot select"):
        CompanyClassificationView(
            status="insufficient_evidence",
            selected_category="stalwart",
        )


def test_analysis_contract_rejects_unknown_or_duplicate_metric_references() -> None:
    with pytest.raises(ValueError, match="unsupported fundamental research metric"):
        FundamentalAnalysisMetricReference(
            metric_key="fundamental.research.unknown",
            relevance_es="No debe aceptarse.",
        )

    source = FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS[0]
    with pytest.raises(ValidationError, match="must be unique"):
        FundamentalAnalysisSectionDefinition(
            **{
                **source.model_dump(),
                "metric_references": (
                    source.metric_references[0],
                    source.metric_references[0],
                ),
            }
        )


def test_analysis_coverage_and_view_reject_inconsistent_availability() -> None:
    with pytest.raises(ValidationError, match="latest analysis metrics exceed"):
        FundamentalAnalysisCoverage(
            expected_metrics=1,
            latest_period_metrics=2,
            historical_series=0,
            series_with_previous_comparison=0,
        )
    with pytest.raises(ValidationError, match="comparisons exceed"):
        FundamentalAnalysisCoverage(
            expected_metrics=2,
            latest_period_metrics=0,
            historical_series=1,
            series_with_previous_comparison=2,
        )

    definition = FUNDAMENTAL_ANALYSIS_SECTION_DEFINITIONS[0]
    expected = tuple(item.metric_key for item in definition.metric_references)
    with pytest.raises(ValidationError, match="must partition"):
        FundamentalAnalysisSectionView(
            definition=definition,
            available_metric_keys=(expected[0],),
            missing_metric_keys=expected,
            historical_metric_keys=(),
            coverage=FundamentalAnalysisCoverage(
                expected_metrics=len(expected),
                latest_period_metrics=1,
                historical_series=0,
                series_with_previous_comparison=0,
            ),
        )
    with pytest.raises(ValidationError, match="require a latest period"):
        FundamentalAnalysisSectionView(
            definition=definition,
            available_metric_keys=(expected[0],),
            missing_metric_keys=expected[1:],
            historical_metric_keys=(),
            coverage=FundamentalAnalysisCoverage(
                expected_metrics=len(expected),
                latest_period_metrics=1,
                historical_series=0,
                series_with_previous_comparison=0,
            ),
        )
