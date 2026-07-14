"""Integration tests for persisted Apple fundamental diagnostics."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.core.models import DataFrequency, DataQuality, MetricResult
from investment_analyst.providers.fundamentals.sec_diagnostic_engine import (
    SecFundamentalDiagnosticEngine,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_models import (
    SecFundamentalDiagnosticRequest,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_pipeline import (
    SecAaplFundamentalDiagnosticPipeline,
    SecFundamentalDiagnosticPipelineTraceabilityError,
)
from investment_analyst.providers.fundamentals.sec_diagnostic_selection import (
    SecFundamentalDiagnosticSelector,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricCandidate,
    SecFundamentalMetricInput,
    get_sec_fundamental_metric_definition,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    sec_fundamental_metric_result_id,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ROLES = {
    "fundamental.net_margin": ("current_net_income", "current_revenue"),
    "fundamental.liabilities_to_assets": ("current_assets", "current_liabilities"),
    "fundamental.liabilities_to_equity": ("current_equity", "current_liabilities"),
    "fundamental.revenue_yoy_growth": ("current_revenue", "previous_revenue"),
    "fundamental.net_income_yoy_change_rate": (
        "current_net_income",
        "previous_net_income",
    ),
}
_VALUES = {
    "fundamental.net_margin": "0.20",
    "fundamental.liabilities_to_assets": "0.50",
    "fundamental.liabilities_to_equity": "1.00",
    "fundamental.revenue_yoy_growth": "0.10",
    "fundamental.net_income_yoy_change_rate": "0.20",
}


def _metric_result(
    metric_name: str,
    value: str,
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
    period_end: datetime | None = None,
    available_at: datetime | None = None,
) -> MetricResult:
    definition = get_sec_fundamental_metric_definition(metric_name)
    period_end = period_end or datetime(2025, 9, 27, tzinfo=UTC)
    available_at = available_at or datetime(2025, 10, 31, tzinfo=UTC)
    inputs = tuple(
        SecFundamentalMetricInput(role=role, observation_id=uuid4()) for role in _ROLES[metric_name]
    )
    candidate = SecFundamentalMetricCandidate(
        asset_id="equity:us:aapl",
        metric_name=metric_name,
        value=Decimal(value),
        unit="ratio",
        frequency=frequency,
        period_end=period_end,
        available_at=available_at,
        input_roles=inputs,
        formula=definition.formula,
        algorithm_version=definition.algorithm_version,
        comparison=definition.comparison,
        fiscal_year="2025",
        fiscal_period="FY" if frequency is DataFrequency.ANNUAL else "Q1",
        quality=DataQuality.VALID,
    )
    return MetricResult(
        result_id=sec_fundamental_metric_result_id(candidate),
        asset_id=candidate.asset_id,
        metric_key=metric_name,
        value=candidate.value,
        unit="ratio",
        as_of=period_end,
        available_at=available_at,
        computed_at=max(datetime(2025, 11, 1, tzinfo=UTC), available_at),
        parameters={
            "source_id": "sec-edgar:aapl:companyfacts",
            "frequency": frequency.value,
            "period_end": period_end.isoformat(),
            "comparison": definition.comparison.value,
            "formula": definition.formula,
            "input_roles": [
                {"role": item.role, "observation_id": str(item.observation_id)} for item in inputs
            ],
            "fiscal_year": "2025",
            "fiscal_period": "FY" if frequency is DataFrequency.ANNUAL else "Q1",
        },
        input_observation_ids=list(candidate.input_observation_ids()),
        algorithm_version=definition.algorithm_version,
        quality=DataQuality.VALID,
    )


def _seed_metrics(
    storage: LocalStorage,
    *,
    frequency: DataFrequency = DataFrequency.ANNUAL,
) -> tuple[MetricResult, ...]:
    results = tuple(
        _metric_result(name, value, frequency=frequency) for name, value in _VALUES.items()
    )
    for result in results:
        storage.metric_results.save(result)
    return results


class _CountingSelector(SecFundamentalDiagnosticSelector):
    def __init__(self, storage: LocalStorage) -> None:
        super().__init__(storage)
        self.calls = 0

    def select(self, request):
        self.calls += 1
        return super().select(request)


class _CountingEngine(SecFundamentalDiagnosticEngine):
    def __init__(self) -> None:
        self.calls = 0

    def compute(self, request, selection, *, computed_at):
        self.calls += 1
        return super().compute(request, selection, computed_at=computed_at)


class _InvalidEngine(SecFundamentalDiagnosticEngine):
    def compute(self, request, selection, *, computed_at):
        computation = super().compute(request, selection, computed_at=computed_at)
        diagnostic = computation.diagnostic.model_copy(
            update={"final_score": computation.diagnostic.final_score + Decimal("1")}
        )
        return computation.model_copy(update={"diagnostic": diagnostic})


def test_pipeline_creates_then_reuses_without_input_side_effects(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_metrics(storage)
        selector = _CountingSelector(storage)
        engine = _CountingEngine()
        pipeline = SecAaplFundamentalDiagnosticPipeline(
            storage,
            selector,
            engine,
            clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        )
        request = SecFundamentalDiagnosticRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
        )
        metric_count = len(storage.metric_results.list(asset_id="equity:us:aapl"))

        first = pipeline.run(request)
        second = pipeline.run(request)

        assert first.diagnostics_created == 1
        assert first.diagnostics_reused == 0
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1
        assert first.diagnostic_id == second.diagnostic_id
        assert first.computed_at == second.computed_at
        assert selector.calls == 2
        assert engine.calls == 2
        assert len(storage.metric_results.list(asset_id="equity:us:aapl")) == metric_count
        assert storage.observations.list() == []
        assert storage.raw_records.list() == []
        assert len(storage.diagnostics.list()) == 1


def test_other_fresh_known_at_reuses_same_selected_inputs(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_metrics(storage)
        pipeline = SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            SecFundamentalDiagnosticEngine(),
            clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
        )
        first = pipeline.run(
            SecFundamentalDiagnosticRequest(
                known_at=datetime(2026, 1, 1, tzinfo=UTC),
                frequency=DataFrequency.ANNUAL,
            )
        )
        second = pipeline.run(
            SecFundamentalDiagnosticRequest(
                known_at=datetime(2026, 1, 15, tzinfo=UTC),
                frequency=DataFrequency.ANNUAL,
            )
        )

        assert first.diagnostic_id == second.diagnostic_id
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1


def test_revised_metric_creates_new_diagnostic_and_preserves_previous(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_metrics(storage)
        pipeline = SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            SecFundamentalDiagnosticEngine(),
            clock=lambda: datetime(2026, 2, 1, tzinfo=UTC),
        )
        request = SecFundamentalDiagnosticRequest(
            known_at=datetime(2026, 1, 31, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
        )
        first = pipeline.run(request)
        storage.metric_results.save(
            _metric_result(
                "fundamental.net_margin",
                "0.22",
                available_at=datetime(2026, 1, 15, tzinfo=UTC),
            )
        )

        second = pipeline.run(request)

        assert second.diagnostic_id != first.diagnostic_id
        assert second.diagnostics_created == 1
        assert len(storage.diagnostics.list()) == 2


def test_invalid_generated_diagnostic_is_not_persisted(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _seed_metrics(storage)
        pipeline = SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            _InvalidEngine(),
            clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        )
        request = SecFundamentalDiagnosticRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
        )

        with pytest.raises(SecFundamentalDiagnosticPipelineTraceabilityError):
            pipeline.run(request)

        assert storage.diagnostics.list() == []


def test_empty_metric_store_persists_coherent_insufficient_diagnostic(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = SecAaplFundamentalDiagnosticPipeline(
            storage,
            SecFundamentalDiagnosticSelector(storage),
            SecFundamentalDiagnosticEngine(),
            clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
        )

        summary = pipeline.run(
            SecFundamentalDiagnosticRequest(
                known_at=datetime(2026, 1, 1, tzinfo=UTC),
                frequency=DataFrequency.QUARTERLY,
            )
        )

        assert summary.verdict.value == "insufficient_data"
        assert summary.final_score == 0
        assert summary.confidence == 0
        assert summary.diagnostics_created == 1
