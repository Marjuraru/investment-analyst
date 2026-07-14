"""Tests for read-only consolidated diagnostic selection and traceability."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticStatus,
    ConsolidatedSectionStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
    AaplConsolidatedDiagnosticService,
    AmbiguousStoredDiagnosticRevisionError,
    MissingReferencedMetricResultError,
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


def _metric(
    *,
    key: str,
    as_of: datetime,
    frequency: DataFrequency | None = None,
) -> MetricResult:
    definition = next(
        (item for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS if item.metric_name == key),
        None,
    )
    parameters: dict[str, object] = {}
    algorithm = "market-simple-return-1d-v1-decimal34"
    unit = "ratio"
    if definition is not None:
        parameters = {
            "frequency": frequency.value if frequency else DataFrequency.QUARTERLY.value,
            "period_end": as_of.isoformat(),
            "formula": definition.formula,
            "input_roles": [
                {"role": "current_net_income", "observation_id": str(uuid4())},
                {"role": "current_revenue", "observation_id": str(uuid4())},
            ],
        }
        algorithm = definition.algorithm_version
    return MetricResult(
        asset_id="equity:us:aapl",
        metric_key=key,
        value=Decimal("0.12"),
        unit=unit,
        as_of=as_of,
        available_at=datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, tzinfo=UTC),
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithm,
        quality=DataQuality.VALID,
    )


def _diagnostic(
    *,
    mode: DiagnosticMode,
    metric_id,
    as_of: datetime,
    algorithm: str,
    available_at: datetime | None = None,
    score: Decimal = Decimal("70"),
) -> DiagnosticResult:
    available = available_at or datetime(2026, 7, 1, tzinfo=UTC)
    return DiagnosticResult(
        asset_id="equity:us:aapl",
        mode=mode,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=score,
        confidence=Decimal("0.8"),
        as_of=as_of,
        available_at=available,
        computed_at=datetime(2026, 7, 20, tzinfo=UTC),
        components=[
            DiagnosticComponent(
                component_key="test",
                score=score,
                weight=Decimal("1"),
                weighted_contribution=score,
                metric_result_ids=[metric_id],
                explanation="Descriptive component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.4"),
                reason="Descriptive evidence.",
            )
        ],
        algorithm_version=algorithm,
        summary="Descriptive diagnostic without recommendation.",
        quality=DataQuality.VALID,
    )


def _insufficient_market_diagnostic(
    *,
    as_of: datetime,
    available_at: datetime | None = None,
) -> DiagnosticResult:
    return DiagnosticResult(
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
        final_score=Decimal("0"),
        confidence=Decimal("0"),
        as_of=as_of,
        available_at=available_at or datetime(2026, 7, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 20, tzinfo=UTC),
        components=[],
        evidence=[],
        algorithm_version=MARKET_ALGORITHM_VERSION,
        summary="Insufficient persisted market metrics for a descriptive diagnostic.",
        quality=DataQuality.PARTIAL,
    )


def test_insufficient_market_diagnostic_without_metrics_is_valid_and_read_only(
    tmp_path,
    monkeypatch,
) -> None:
    known_at = datetime(2026, 7, 14, tzinfo=UTC)
    as_of = datetime(2026, 7, 9, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        insufficient = _insufficient_market_diagnostic(as_of=as_of)
        storage.diagnostics.save(insufficient)
        diagnostics_before = tuple(storage.diagnostics.list(asset_id="equity:us:aapl"))
        metrics_before = tuple(storage.metric_results.list(asset_id="equity:us:aapl"))

        def forbidden_access(*_args, **_kwargs):
            raise AssertionError("consolidated query must remain read-only")

        for repository, method_name in (
            (storage.raw_records, "get"),
            (storage.raw_records, "list"),
            (storage.raw_records, "save"),
            (storage.observations, "get"),
            (storage.observations, "list"),
            (storage.observations, "save"),
            (storage.metric_results, "save"),
            (storage.diagnostics, "save"),
        ):
            monkeypatch.setattr(repository, method_name, forbidden_access)

        view = AaplConsolidatedDiagnosticService(storage).query(
            ConsolidatedDiagnosticRequest(
                known_at=known_at,
                fundamental_frequency=DataFrequency.QUARTERLY,
            )
        )

        assert view.status is ConsolidatedDiagnosticStatus.PARTIAL
        assert view.market.status is ConsolidatedSectionStatus.AVAILABLE
        assert view.market.diagnostic == insufficient
        assert view.market.selected_metric_result_ids == ()
        assert view.market.candidates_eligible == 1
        assert tuple(storage.diagnostics.list(asset_id="equity:us:aapl")) == diagnostics_before
        assert tuple(storage.metric_results.list(asset_id="equity:us:aapl")) == metrics_before


def test_newer_normal_market_diagnostic_wins_over_prior_insufficient_data(tmp_path) -> None:
    known_at = datetime(2026, 7, 14, tzinfo=UTC)
    insufficient_as_of = datetime(2026, 7, 9, tzinfo=UTC)
    normal_as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.diagnostics.save(
            _insufficient_market_diagnostic(
                as_of=insufficient_as_of,
                available_at=datetime(2026, 7, 10, tzinfo=UTC),
            )
        )
        metric = _metric(key=SIMPLE_RETURN_KEY, as_of=normal_as_of)
        normal = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=metric.result_id,
            as_of=normal_as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            available_at=datetime(2026, 7, 11, tzinfo=UTC),
        )
        storage.metric_results.save(metric)
        storage.diagnostics.save(normal)

        view = AaplConsolidatedDiagnosticService(storage).query(
            ConsolidatedDiagnosticRequest(
                known_at=known_at,
                fundamental_frequency=DataFrequency.QUARTERLY,
            )
        )

        assert view.market.status is ConsolidatedSectionStatus.AVAILABLE
        assert view.market.diagnostic == normal
        assert view.market.selected_metric_result_ids == (metric.result_id,)
        assert view.market.candidates_eligible == 2


def test_complete_partial_unavailable_and_versions(tmp_path) -> None:
    known_at = datetime(2026, 7, 14, tzinfo=UTC)
    market_as_of = datetime(2026, 7, 10, tzinfo=UTC)
    fundamental_as_of = datetime(2026, 6, 30, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        market_metric = _metric(key=SIMPLE_RETURN_KEY, as_of=market_as_of)
        fundamental_metric = _metric(
            key="fundamental.net_margin",
            as_of=fundamental_as_of,
            frequency=DataFrequency.QUARTERLY,
        )
        storage.metric_results.save(market_metric)
        storage.metric_results.save(fundamental_metric)
        storage.diagnostics.save(
            _diagnostic(
                mode=DiagnosticMode.MARKET,
                metric_id=market_metric.result_id,
                as_of=market_as_of,
                algorithm=MARKET_ALGORITHM_VERSION,
            )
        )
        storage.diagnostics.save(
            _diagnostic(
                mode=DiagnosticMode.FUNDAMENTAL,
                metric_id=fundamental_metric.result_id,
                as_of=fundamental_as_of,
                algorithm="sec-aapl-fundamental-diagnostic-v1-decimal34",
            )
        )
        storage.diagnostics.save(
            _diagnostic(
                mode=DiagnosticMode.FUNDAMENTAL,
                metric_id=fundamental_metric.result_id,
                as_of=fundamental_as_of,
                algorithm=FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
            )
        )
        service = AaplConsolidatedDiagnosticService(storage)
        view = service.query(
            ConsolidatedDiagnosticRequest(
                known_at=known_at,
                fundamental_frequency=DataFrequency.QUARTERLY,
            )
        )
        assert view.status is ConsolidatedDiagnosticStatus.COMPLETE
        assert view.fundamental.diagnostic is not None
        assert view.fundamental.diagnostic.algorithm_version.endswith("v1.1-decimal34")
        assert view.ignored_algorithm_versions == 1
        assert view.market.computed_after_known_at

    with LocalStorage(StoragePaths.from_root(tmp_path / "empty")) as empty:
        view = AaplConsolidatedDiagnosticService(empty).query(
            ConsolidatedDiagnosticRequest(
                known_at=known_at,
                fundamental_frequency=DataFrequency.ANNUAL,
            )
        )
        assert view.status is ConsolidatedDiagnosticStatus.UNAVAILABLE
        assert view.market.status is ConsolidatedSectionStatus.NOT_FOUND


def test_exact_period_does_not_fall_back(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        metric = _metric(key=SIMPLE_RETURN_KEY, as_of=as_of)
        storage.metric_results.save(metric)
        storage.diagnostics.save(
            _diagnostic(
                mode=DiagnosticMode.MARKET,
                metric_id=metric.result_id,
                as_of=as_of,
                algorithm=MARKET_ALGORITHM_VERSION,
            )
        )
        view = AaplConsolidatedDiagnosticService(storage).query(
            ConsolidatedDiagnosticRequest(
                known_at=datetime(2026, 7, 14, tzinfo=UTC),
                fundamental_frequency=DataFrequency.QUARTERLY,
                market_as_of=datetime(2026, 7, 9, tzinfo=UTC).date(),
            )
        )
        assert view.status is ConsolidatedDiagnosticStatus.UNAVAILABLE
        assert view.market.status is ConsolidatedSectionStatus.NOT_FOUND


def test_missing_metric_and_ambiguous_revision_are_rejected(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path / "missing")) as storage:
        storage.diagnostics.save(
            _diagnostic(
                mode=DiagnosticMode.MARKET,
                metric_id=uuid4(),
                as_of=as_of,
                algorithm=MARKET_ALGORITHM_VERSION,
            )
        )
        with pytest.raises(MissingReferencedMetricResultError):
            AaplConsolidatedDiagnosticService(storage).query(
                ConsolidatedDiagnosticRequest(
                    known_at=datetime(2026, 7, 14, tzinfo=UTC),
                    fundamental_frequency=DataFrequency.QUARTERLY,
                )
            )

    with LocalStorage(StoragePaths.from_root(tmp_path / "ambiguous")) as storage:
        metric = _metric(key=SIMPLE_RETURN_KEY, as_of=as_of)
        storage.metric_results.save(metric)
        for score in (Decimal("60"), Decimal("70")):
            storage.diagnostics.save(
                _diagnostic(
                    mode=DiagnosticMode.MARKET,
                    metric_id=metric.result_id,
                    as_of=as_of,
                    algorithm=MARKET_ALGORITHM_VERSION,
                    score=score,
                )
            )
        with pytest.raises(AmbiguousStoredDiagnosticRevisionError):
            AaplConsolidatedDiagnosticService(storage).query(
                ConsolidatedDiagnosticRequest(
                    known_at=datetime(2026, 7, 14, tzinfo=UTC),
                    fundamental_frequency=DataFrequency.QUARTERLY,
                )
            )
