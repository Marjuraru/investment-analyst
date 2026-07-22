"""Tests for point-in-time unified fundamental analysis."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from investment_analyst.analytics.fundamentals.analysis_service import (
    AaplFundamentalAnalysisService,
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
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_fact_models import get_sec_fact_definition

_KNOWN_AT = datetime(2026, 7, 20, tzinfo=UTC)
_PERIOD_END = datetime(2025, 9, 27, tzinfo=UTC)
_AVAILABLE_AT = datetime(2025, 10, 31, tzinfo=UTC)


class _FakeResearch:
    def __init__(self, result: AaplFundamentalResearchResult) -> None:
        self._result = result

    def query(self, request: AaplFundamentalResearchRequest) -> AaplFundamentalResearchResult:
        return self._result.model_copy(update={"request": request})


def _research_result(metric_keys: set[str]) -> AaplFundamentalResearchResult:
    request = AaplFundamentalResearchRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )
    metrics: list[FundamentalResearchMetricValue] = []
    metric_counts: dict[str, int] = {}
    observation_index = 1
    for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS:
        included = definition.metric_key in metric_keys
        metric_counts[definition.metric_key] = int(included)
        if not included:
            continue
        inputs: list[FundamentalResearchMetricInput] = []
        for field in definition.input_fields:
            inputs.append(
                FundamentalResearchMetricInput(
                    role=field.role,
                    field_name=field.field_name,
                    observation_id=UUID(int=observation_index),
                    value=Decimal(observation_index),
                    unit=get_sec_fact_definition(field.field_name).unit,
                    available_at=_AVAILABLE_AT,
                )
            )
            observation_index += 1
        metrics.append(
            FundamentalResearchMetricValue(
                metric_key=definition.metric_key,
                display_name_es=definition.display_name_es,
                value=Decimal("1.25"),
                unit=definition.unit,
                frequency=DataFrequency.ANNUAL,
                period_end=_PERIOD_END,
                available_at=_AVAILABLE_AT,
                formula=definition.formula,
                algorithm_version=definition.algorithm_version,
                inputs=tuple(inputs),
                limitations=definition.limitations,
            )
        )
    periods = (
        (
            AaplFundamentalResearchPeriod(
                period_end=_PERIOD_END,
                frequency=DataFrequency.ANNUAL,
                metrics=tuple(metrics),
            ),
        )
        if metrics
        else ()
    )
    return AaplFundamentalResearchResult(
        request=request,
        periods=periods,
        coverage=AaplFundamentalResearchCoverage(
            observations_examined=observation_index - 1,
            observations_eligible=observation_index - 1,
            observations_selected=observation_index - 1,
            observations_superseded=0,
            source_periods=int(bool(metrics)),
            output_periods=len(periods),
            metrics_returned=len(metrics),
            metric_counts=metric_counts,
            skipped_counts={},
            earliest_period_end=_PERIOD_END if metrics else None,
            latest_period_end=_PERIOD_END if metrics else None,
        ),
    )


def _service(result: AaplFundamentalResearchResult) -> AaplFundamentalAnalysisService:
    history = AaplFundamentalResearchHistoryService(_FakeResearch(result))
    return AaplFundamentalAnalysisService(history)


def test_analysis_reuses_exact_evidence_once_without_scores() -> None:
    metric_keys = {definition.metric_key for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS}
    request = AaplFundamentalResearchRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )

    result = _service(_research_result(metric_keys)).query(request)

    assert result.schema_version == "aapl-fundamental-analysis-v1"
    assert result.request == request
    assert tuple(item.coverage.latest_period_metrics for item in result.sections) == (
        5,
        5,
        6,
        2,
        4,
        11,
        5,
        2,
    )
    keys = tuple(
        reference.metric_key
        for section in result.sections
        for reference in section.definition.metric_references
    )
    assert len(keys) == len(set(keys)) == 40
    assert result.coverage.latest_period_metrics == 40
    assert all(not item.missing_metric_keys for item in result.sections)
    assert result.classification.status == "insufficient_evidence"
    assert result.classification.selected_category is None
    assert len(result.classification.categories) == 6
    payload = result.to_json_dict()
    assert "score" not in payload
    assert "verdict" not in payload
    assert payload["history"]["research"]["periods"][0]["metrics"][0]["value"] == "1.25"


def test_analysis_discloses_missing_metrics_in_their_only_section() -> None:
    request = AaplFundamentalResearchRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )

    result = _service(_research_result({"fundamental.research.current_ratio"})).query(request)

    by_key = {item.definition.section_key: item for item in result.sections}
    assert by_key["liquidity_and_balance"].available_metric_keys == (
        "fundamental.research.current_ratio",
    )
    assert by_key["liquidity_and_balance"].coverage.latest_period_metrics == 1
    assert all(
        item.coverage.latest_period_metrics == 0
        for key, item in by_key.items()
        if key != "liquidity_and_balance"
    )
    assert result.coverage.latest_period_metrics == 1


def test_analysis_preserves_an_empty_point_in_time_history() -> None:
    request = AaplFundamentalResearchRequest(
        known_at=_KNOWN_AT,
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )

    result = _service(_research_result(set())).query(request)

    assert result.history.research.periods == ()
    assert all(item.latest_period_end is None for item in result.sections)
    assert all(item.coverage.latest_period_metrics == 0 for item in result.sections)
    assert result.coverage.latest_period_metrics == 0
