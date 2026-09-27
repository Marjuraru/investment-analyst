"""Integration tests for consolidated read-only diagnostics in one local database."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    ALGORITHM_VERSION as MARKET_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.statistics_definitions import SIMPLE_RETURN_KEY
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def _metric(key: str, as_of: datetime, frequency: DataFrequency | None = None) -> MetricResult:
    definition = next(
        (item for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS if item.metric_name == key),
        None,
    )
    parameters: dict[str, object] = {}
    algorithm = "market-simple-return-1d-v1-decimal34"
    if definition is not None:
        parameters = {
            "frequency": frequency.value,
            "period_end": as_of.isoformat(),
            "formula": definition.formula,
            "input_roles": [],
        }
        algorithm = definition.algorithm_version
    return MetricResult(
        asset_id="equity:us:aapl",
        metric_key=key,
        value=Decimal("0.1"),
        unit="ratio",
        as_of=as_of,
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, tzinfo=UTC),
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithm,
        quality=DataQuality.VALID,
    )


def _diagnostic(
    mode: DiagnosticMode,
    metric: MetricResult,
    algorithm: str,
) -> DiagnosticResult:
    score = Decimal("68") if mode is DiagnosticMode.MARKET else Decimal("72")
    return DiagnosticResult(
        asset_id="equity:us:aapl",
        mode=mode,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=score,
        confidence=Decimal("0.7"),
        as_of=metric.as_of,
        available_at=metric.available_at,
        computed_at=datetime(2026, 7, 20, tzinfo=UTC),
        components=[
            DiagnosticComponent(
                component_key=f"{mode.value}_test",
                score=score,
                weight=Decimal("1"),
                weighted_contribution=score,
                metric_result_ids=[metric.result_id],
                explanation="Independent descriptive component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.3"),
                reason="Independent descriptive evidence.",
            )
        ],
        algorithm_version=algorithm,
        summary=f"Independent {mode.value} diagnostic.",
        quality=DataQuality.VALID,
    )


def _counts(storage: LocalStorage) -> tuple[int, int, int, int]:
    connection = storage.store.connection
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "raw_record_index",
            "normalized_observations",
            "metric_results",
            "diagnostic_results",
        )
    )


def test_both_modes_coexist_without_combination_or_writes(tmp_path) -> None:
    market_metric = _metric(SIMPLE_RETURN_KEY, datetime(2026, 7, 10, tzinfo=UTC))
    fundamental_metric = _metric(
        "fundamental.net_margin",
        datetime(2026, 6, 30, tzinfo=UTC),
        DataFrequency.QUARTERLY,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(market_metric)
        storage.metric_results.save(fundamental_metric)
        market = _diagnostic(
            DiagnosticMode.MARKET,
            market_metric,
            MARKET_ALGORITHM_VERSION,
        )
        fundamental = _diagnostic(
            DiagnosticMode.FUNDAMENTAL,
            fundamental_metric,
            FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
        )
        storage.diagnostics.save(market)
        storage.diagnostics.save(fundamental)
        before = _counts(storage)
        view = AaplConsolidatedDiagnosticService(storage).query(
            ConsolidatedDiagnosticRequest(
                known_at=datetime(2026, 7, 14, tzinfo=UTC),
                fundamental_frequency=DataFrequency.QUARTERLY,
            )
        )
        after = _counts(storage)

    assert view.status is ConsolidatedDiagnosticStatus.COMPLETE
    assert view.market.diagnostic is not None
    assert view.fundamental.diagnostic is not None
    assert view.market.diagnostic.final_score == Decimal("68")
    assert view.fundamental.diagnostic.final_score == Decimal("72")
    assert before == after
    payload = view.to_json_dict()
    assert "combined_score" not in payload
    assert "combined_verdict" not in payload


class _RecordingMetricResults:
    """Observe metric hydration without changing persisted behavior."""

    def __init__(self, repository) -> None:  # type: ignore[no-untyped-def]
        self._repository = repository
        self.list_calls = 0
        self.get_calls = 0
        self.get_existing_calls = 0

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self._repository, name)

    def list(self, **kwargs):  # type: ignore[no-untyped-def]
        self.list_calls += 1
        return self._repository.list(**kwargs)

    def get(self, result_id):  # type: ignore[no-untyped-def]
        self.get_calls += 1
        return self._repository.get(result_id)

    def get_existing(self, result_ids):  # type: ignore[no-untyped-def]
        self.get_existing_calls += 1
        return self._repository.get_existing(result_ids)


def test_consolidated_query_skips_unrelated_metric_history(tmp_path) -> None:
    market_metric = _metric(SIMPLE_RETURN_KEY, datetime(2026, 7, 10, tzinfo=UTC))
    fundamental_metric = _metric(
        "fundamental.net_margin",
        datetime(2026, 6, 30, tzinfo=UTC),
        DataFrequency.QUARTERLY,
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.metric_results.save(market_metric)
        storage.metric_results.save(fundamental_metric)
        storage.diagnostics.save(
            _diagnostic(DiagnosticMode.MARKET, market_metric, MARKET_ALGORITHM_VERSION)
        )
        storage.diagnostics.save(
            _diagnostic(
                DiagnosticMode.FUNDAMENTAL,
                fundamental_metric,
                FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
            )
        )
        for index in range(40):
            storage.metric_results.save(
                MetricResult(
                    asset_id="equity:us:aapl",
                    metric_key="market.history.simple_return_1d",
                    value=Decimal(f"0.{index + 10}"),
                    unit="ratio",
                    as_of=datetime(2025, 1, 1, tzinfo=UTC),
                    available_at=datetime(2025, 2, 1, tzinfo=UTC),
                    computed_at=datetime(2025, 2, 2, tzinfo=UTC),
                    parameters={},
                    input_observation_ids=[uuid4()],
                    algorithm_version="market-simple-return-1d-v1-decimal34",
                    quality=DataQuality.VALID,
                )
            )
            storage.metric_results.save(
                MetricResult(
                    asset_id="equity:us:aapl",
                    metric_key="fundamental.noise_family",
                    value=Decimal("0.01"),
                    unit="ratio",
                    as_of=datetime(2025, 1, 1, tzinfo=UTC),
                    available_at=datetime(2025, 2, 1, tzinfo=UTC),
                    computed_at=datetime(2025, 2, 2, tzinfo=UTC),
                    parameters={},
                    input_observation_ids=[uuid4()],
                    algorithm_version="sec-fundamental-metric-noise-v1",
                    quality=DataQuality.VALID,
                )
            )
        baseline = AaplConsolidatedDiagnosticService(storage).query(
            ConsolidatedDiagnosticRequest(
                known_at=datetime(2026, 7, 14, tzinfo=UTC),
                fundamental_frequency=DataFrequency.QUARTERLY,
            )
        )
        recorder = _RecordingMetricResults(storage.metric_results)
        storage.metric_results = recorder  # type: ignore[method-assign]

        view = AaplConsolidatedDiagnosticService(storage).query(
            ConsolidatedDiagnosticRequest(
                known_at=datetime(2026, 7, 14, tzinfo=UTC),
                fundamental_frequency=DataFrequency.QUARTERLY,
            )
        )

        assert view.to_json_dict() == baseline.to_json_dict()
        assert view.status is ConsolidatedDiagnosticStatus.COMPLETE
        assert view.metric_results_examined == 2
        assert recorder.list_calls == 0
        assert recorder.get_calls == 0
        assert recorder.get_existing_calls == 1
