"""Tests for the versioned evidence-first company classification."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from investment_analyst.analytics.fundamentals.company_classification import (
    classify_company,
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
from investment_analyst.providers.fundamentals.sec_fact_models import get_sec_fact_definition

_KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
_EPS_KEY = "fundamental.research.diluted_eps"
_REVENUE_KEY = "fundamental.research.revenue_per_diluted_share"


class _Research:
    def __init__(self, result: AaplFundamentalResearchResult) -> None:
        self._result = result

    def query(self, request: AaplFundamentalResearchRequest) -> AaplFundamentalResearchResult:
        assert request == self._result.request
        return self._result


def _history(
    eps_values: tuple[str, ...],
    revenue_values: tuple[str, ...],
):
    request = AaplFundamentalResearchRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )
    period_ends = tuple(
        datetime(year, 9, 30, tzinfo=UTC) for year in range(2022, 2022 + len(eps_values))
    )
    periods: list[AaplFundamentalResearchPeriod] = []
    observation_index = 1
    for period_end, eps, revenue in zip(
        period_ends,
        eps_values,
        revenue_values,
        strict=True,
    ):
        metrics: list[FundamentalResearchMetricValue] = []
        for metric_key, value in ((_EPS_KEY, eps), (_REVENUE_KEY, revenue)):
            definition = get_fundamental_research_metric_definition(metric_key)
            inputs: list[FundamentalResearchMetricInput] = []
            for field in definition.input_fields:
                inputs.append(
                    FundamentalResearchMetricInput(
                        role=field.role,
                        field_name=field.field_name,
                        observation_id=UUID(int=observation_index),
                        value=Decimal("1"),
                        unit=get_sec_fact_definition(field.field_name).unit,
                        available_at=period_end,
                    )
                )
                observation_index += 1
            metrics.append(
                FundamentalResearchMetricValue(
                    metric_key=metric_key,
                    display_name_es=definition.display_name_es,
                    value=Decimal(value),
                    unit=definition.unit,
                    frequency=DataFrequency.ANNUAL,
                    period_end=period_end,
                    available_at=period_end,
                    formula=definition.formula,
                    algorithm_version=definition.algorithm_version,
                    inputs=tuple(inputs),
                    limitations=definition.limitations,
                )
            )
        periods.append(
            AaplFundamentalResearchPeriod(
                period_end=period_end,
                frequency=DataFrequency.ANNUAL,
                metrics=tuple(sorted(metrics, key=lambda item: item.metric_key)),
            )
        )
    metric_counts = {
        definition.metric_key: (
            len(periods) if definition.metric_key in {_EPS_KEY, _REVENUE_KEY} else 0
        )
        for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
    }
    result = AaplFundamentalResearchResult(
        request=request,
        periods=tuple(periods),
        coverage=AaplFundamentalResearchCoverage(
            observations_examined=observation_index - 1,
            observations_eligible=observation_index - 1,
            observations_selected=observation_index - 1,
            observations_superseded=0,
            source_periods=len(periods),
            output_periods=len(periods),
            metrics_returned=len(periods) * 2,
            metric_counts=metric_counts,
            skipped_counts={},
            earliest_period_end=period_ends[0],
            latest_period_end=period_ends[-1],
        ),
    )
    return AaplFundamentalResearchHistoryService(_Research(result)).query(request)


def test_classification_selects_one_published_growth_band_with_exact_evidence() -> None:
    history = _history(
        ("4", "4.6", "5.3", "6.1"),
        ("10", "10.8", "11.7", "12.6"),
    )

    classification = classify_company(history)

    assert classification.status == "classified"
    assert classification.selected_category == "stalwart"
    assert classification.algorithm_version == "peter-lynch-company-category-v1-decimal34"
    assert tuple(item.metric_key for item in classification.evidence) == (
        _EPS_KEY,
        _REVENUE_KEY,
    )
    assert all(item.point_count == 4 for item in classification.evidence)
    assert all(item.input_observation_ids for item in classification.evidence)
    assert tuple(
        item.category_key for item in classification.assessments if item.status == "matched"
    ) == ("stalwart",)
    assert classification.evaluated_known_at == _KNOWN_AT


def test_classification_refuses_a_label_when_growth_indicators_diverge() -> None:
    history = _history(
        ("1", "1.3", "1.7", "2.2"),
        ("10", "10.2", "10.4", "10.6"),
    )

    classification = classify_company(history)

    assert classification.status == "insufficient_evidence"
    assert classification.selected_category is None
    assert classification.evidence
    assert "no coinciden" in classification.explanation_es
    assert not any(item.status == "matched" for item in classification.assessments)


def test_classification_requires_four_comparable_annual_points() -> None:
    history = _history(("4", "4.6", "5.3"), ("10", "10.8", "11.7"))

    classification = classify_company(history)

    assert classification.status == "insufficient_evidence"
    assert classification.selected_category is None
    assert classification.evidence == ()
    assert all(item.status == "insufficient_evidence" for item in classification.assessments[:3])
