"""Tests for read-only consolidated diagnostic selection and traceability."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
    ConsolidatedDiagnosticStatus,
    ConsolidatedSectionStatus,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    FUNDAMENTAL_DIAGNOSTIC_ALGORITHM_VERSION,
    AaplConsolidatedDiagnosticService,
    AmbiguousStoredDiagnosticRevisionError,
    ConsolidatedDiagnosticTraceabilityError,
    MissingReferencedMetricResultError,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    ALGORITHM_VERSION as MARKET_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.statistics_definitions import (
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
)
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
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
)
from investment_analyst.storage import LocalStorage, StoragePaths

AAPL_ASSET_ID = "equity:us:aapl"
AAPL_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
METRIC_AVAILABLE_AT = datetime(2026, 7, 1, tzinfo=UTC)


def _metric(
    *,
    key: str,
    as_of: datetime,
    frequency: DataFrequency | None = None,
    result_id: UUID | None = None,
    asset_id: str = AAPL_ASSET_ID,
    value: Decimal = Decimal("0.12"),
    available_at: datetime = METRIC_AVAILABLE_AT,
    computed_at: datetime = datetime(2026, 7, 12, tzinfo=UTC),
    parameters: dict[str, JsonValue] | None = None,
    input_observation_ids: tuple[UUID, ...] | None = None,
) -> MetricResult:
    definition = next(
        (item for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS if item.metric_name == key),
        None,
    )
    inputs = list(input_observation_ids or (uuid4(),))
    default_parameters: dict[str, JsonValue] = {}
    algorithm = "market-simple-return-1d-v1-decimal34"
    unit = "ratio"
    if definition is not None:
        if input_observation_ids is None:
            inputs.append(uuid4())
        default_parameters = {
            "frequency": frequency.value if frequency else DataFrequency.QUARTERLY.value,
            "period_end": as_of.isoformat(),
            "formula": definition.formula,
            "input_roles": [
                {"role": "current_net_income", "observation_id": str(inputs[0])},
                {"role": "current_revenue", "observation_id": str(inputs[-1])},
            ],
        }
        algorithm = definition.algorithm_version
    return MetricResult(
        result_id=result_id or uuid4(),
        asset_id=asset_id,
        metric_key=key,
        value=value,
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at,
        parameters=parameters if parameters is not None else default_parameters,
        input_observation_ids=inputs,
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
    diagnostic_id: UUID | None = None,
    computed_at: datetime = datetime(2026, 7, 20, tzinfo=UTC),
) -> DiagnosticResult:
    available = available_at or datetime(2026, 7, 1, tzinfo=UTC)
    return DiagnosticResult(
        diagnostic_id=diagnostic_id or uuid4(),
        asset_id=AAPL_ASSET_ID,
        mode=mode,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=score,
        confidence=Decimal("0.8"),
        as_of=as_of,
        available_at=available,
        computed_at=computed_at,
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


def _persist_observation(
    storage: LocalStorage,
    *,
    as_of: datetime,
    value: Decimal = Decimal("102"),
    field_name: str = "close",
    record_id: UUID | None = None,
    observation_id: UUID | None = None,
    execution_offset: timedelta = timedelta(),
) -> NormalizedObservation:
    raw_id = record_id or uuid4()
    identifier = observation_id or uuid4()
    available_at = METRIC_AVAILABLE_AT
    execution_time = available_at + execution_offset
    reference = SourceReference(
        source_id=AAPL_SOURCE_ID,
        record_key=f"AAPL:{as_of.isoformat()}:{field_name}",
        retrieved_at=execution_time,
    )
    storage.raw_records.save(
        RawRecord(
            record_id=raw_id,
            asset_id=AAPL_ASSET_ID,
            source=reference,
            event_time=as_of,
            available_at=available_at,
            received_at=execution_time,
            payload={field_name: str(value)},
            schema_version="semantic-revision-test-v1",
        )
    )
    observation = NormalizedObservation(
        observation_id=identifier,
        raw_record_id=raw_id,
        asset_id=AAPL_ASSET_ID,
        field_name=field_name,
        value=value,
        unit="USD" if field_name == "close" else "shares",
        frequency=DataFrequency.DAY_1,
        observed_at=as_of,
        available_at=available_at,
        normalized_at=execution_time,
        source=reference,
        quality=DataQuality.VALID,
        transformation_version="semantic-revision-test-v1",
    )
    storage.observations.save(observation)
    return observation


def _query(storage: LocalStorage, *, known_at: datetime | None = None):
    return AaplConsolidatedDiagnosticService(storage).query(
        ConsolidatedDiagnosticRequest(
            known_at=known_at or datetime(2026, 7, 14, tzinfo=UTC),
            fundamental_frequency=DataFrequency.QUARTERLY,
        )
    )


def _two_component_diagnostic(
    *,
    metric_ids: tuple[UUID, UUID],
    as_of: datetime,
    reverse: bool,
    diagnostic_id: UUID | None = None,
    computed_at: datetime = datetime(2026, 7, 20, tzinfo=UTC),
) -> DiagnosticResult:
    specifications = (
        ("price", metric_ids[0], EvidenceDirection.SUPPORTS),
        ("volume", metric_ids[1], EvidenceDirection.NEUTRAL),
    )
    ordered = tuple(reversed(specifications)) if reverse else specifications
    return DiagnosticResult(
        diagnostic_id=diagnostic_id or uuid4(),
        asset_id=AAPL_ASSET_ID,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("70"),
        confidence=Decimal("0.8"),
        as_of=as_of,
        available_at=METRIC_AVAILABLE_AT,
        computed_at=computed_at,
        components=[
            DiagnosticComponent(
                component_key=key,
                score=Decimal("70"),
                weight=Decimal("0.5"),
                weighted_contribution=Decimal("35"),
                metric_result_ids=[metric_id],
                explanation=f"Descriptive {key} component.",
            )
            for key, metric_id, _direction in ordered
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric_id,
                direction=direction,
                contribution=Decimal("0.2"),
                reason=f"Descriptive {key} evidence.",
            )
            for key, metric_id, direction in ordered
        ],
        algorithm_version=MARKET_ALGORITHM_VERSION,
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
        assert view.fundamental.revisions_superseded == 0
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
        observation = _persist_observation(storage, as_of=as_of)
        metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            input_observation_ids=(observation.observation_id,),
        )
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


def test_equivalent_recomputations_select_earliest_computed_revision(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_observation = _persist_observation(
            storage,
            as_of=as_of,
            execution_offset=timedelta(minutes=1),
        )
        second_observation = _persist_observation(
            storage,
            as_of=as_of,
            execution_offset=timedelta(minutes=2),
        )
        first_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            computed_at=datetime(2026, 7, 12, tzinfo=UTC),
            parameters={
                "known_at": datetime(2026, 7, 13, tzinfo=UTC).isoformat(),
                "periods": 1,
                "source_id": AAPL_SOURCE_ID,
            },
            input_observation_ids=(first_observation.observation_id,),
        )
        second_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            computed_at=datetime(2026, 7, 13, tzinfo=UTC),
            parameters={
                "known_at": datetime(2026, 7, 14, tzinfo=UTC).isoformat(),
                "periods": 1,
                "source_id": AAPL_SOURCE_ID,
            },
            input_observation_ids=(second_observation.observation_id,),
        )
        first_diagnostic = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=first_metric.result_id,
            as_of=as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            diagnostic_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
            computed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        second_diagnostic = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=second_metric.result_id,
            as_of=as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            diagnostic_id=UUID("00000000-0000-0000-0000-000000000001"),
            computed_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        storage.metric_results.save(first_metric)
        storage.metric_results.save(second_metric)
        storage.diagnostics.save(first_diagnostic)
        storage.diagnostics.save(second_diagnostic)

        view = _query(storage)

        assert view.market.diagnostic == first_diagnostic
        assert view.market.selected_metric_result_ids == (first_metric.result_id,)
        assert view.market.revisions_superseded == 1
        assert len(storage.metric_results.list(asset_id=AAPL_ASSET_ID)) == 2
        assert len(storage.diagnostics.list(asset_id=AAPL_ASSET_ID)) == 2


def test_equivalent_recomputations_break_computed_at_tie_by_diagnostic_id(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        observation = _persist_observation(storage, as_of=as_of)
        metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            input_observation_ids=(observation.observation_id,),
        )
        storage.metric_results.save(metric)
        smaller = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=metric.result_id,
            as_of=as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            diagnostic_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
        larger = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=metric.result_id,
            as_of=as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            diagnostic_id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"),
        )
        storage.diagnostics.save(larger)
        storage.diagnostics.save(smaller)

        view = _query(storage)

        assert view.market.diagnostic == smaller
        assert view.market.revisions_superseded == 1


@pytest.mark.parametrize(
    ("second_value", "second_periods"),
    (
        (Decimal("0.13"), 1),
        (Decimal("0.12"), 2),
    ),
)
def test_metric_value_or_analytical_parameter_difference_remains_ambiguous(
    tmp_path,
    second_value: Decimal,
    second_periods: int,
) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        observation = _persist_observation(storage, as_of=as_of)
        common = {
            "known_at": datetime(2026, 7, 14, tzinfo=UTC).isoformat(),
            "source_id": AAPL_SOURCE_ID,
        }
        first_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            parameters={**common, "periods": 1},
            input_observation_ids=(observation.observation_id,),
        )
        second_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            value=second_value,
            parameters={**common, "periods": second_periods},
            input_observation_ids=(observation.observation_id,),
        )
        storage.metric_results.save(first_metric)
        storage.metric_results.save(second_metric)
        for metric in (first_metric, second_metric):
            storage.diagnostics.save(
                _diagnostic(
                    mode=DiagnosticMode.MARKET,
                    metric_id=metric.result_id,
                    as_of=as_of,
                    algorithm=MARKET_ALGORITHM_VERSION,
                )
            )

        with pytest.raises(AmbiguousStoredDiagnosticRevisionError):
            _query(storage)


def test_later_available_revision_still_supersedes_earlier_revision(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        metric = _metric(key=SIMPLE_RETURN_KEY, as_of=as_of)
        earlier = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=metric.result_id,
            as_of=as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            available_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        later = _diagnostic(
            mode=DiagnosticMode.MARKET,
            metric_id=metric.result_id,
            as_of=as_of,
            algorithm=MARKET_ALGORITHM_VERSION,
            available_at=datetime(2026, 7, 2, tzinfo=UTC),
        )
        storage.metric_results.save(metric)
        storage.diagnostics.save(earlier)
        storage.diagnostics.save(later)

        view = _query(storage)

        assert view.market.diagnostic == later
        assert view.market.revisions_superseded == 1


def test_metric_from_another_asset_is_rejected(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            asset_id="equity:us:msft",
        )
        storage.metric_results.save(metric)
        storage.diagnostics.save(
            _diagnostic(
                mode=DiagnosticMode.MARKET,
                metric_id=metric.result_id,
                as_of=as_of,
                algorithm=MARKET_ALGORITHM_VERSION,
            )
        )

        with pytest.raises(ConsolidatedDiagnosticTraceabilityError):
            _query(storage)


def test_semantically_different_observations_remain_ambiguous(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_observation = _persist_observation(storage, as_of=as_of, value=Decimal("102"))
        second_observation = _persist_observation(storage, as_of=as_of, value=Decimal("103"))
        first_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            input_observation_ids=(first_observation.observation_id,),
        )
        second_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            input_observation_ids=(second_observation.observation_id,),
        )
        storage.metric_results.save(first_metric)
        storage.metric_results.save(second_metric)
        for metric in (first_metric, second_metric):
            storage.diagnostics.save(
                _diagnostic(
                    mode=DiagnosticMode.MARKET,
                    metric_id=metric.result_id,
                    as_of=as_of,
                    algorithm=MARKET_ALGORITHM_VERSION,
                )
            )

        with pytest.raises(AmbiguousStoredDiagnosticRevisionError):
            _query(storage)


def test_component_and_evidence_order_does_not_create_false_conflict(tmp_path) -> None:
    as_of = datetime(2026, 7, 10, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        close = _persist_observation(storage, as_of=as_of, field_name="close")
        volume = _persist_observation(
            storage,
            as_of=as_of,
            field_name="volume",
            value=Decimal("1000000"),
        )
        price_metric = _metric(
            key=SIMPLE_RETURN_KEY,
            as_of=as_of,
            input_observation_ids=(close.observation_id,),
        )
        volume_metric = _metric(
            key=RELATIVE_VOLUME_KEY,
            as_of=as_of,
            input_observation_ids=(volume.observation_id,),
        )
        storage.metric_results.save(price_metric)
        storage.metric_results.save(volume_metric)
        first = _two_component_diagnostic(
            metric_ids=(price_metric.result_id, volume_metric.result_id),
            as_of=as_of,
            reverse=False,
            computed_at=datetime(2026, 7, 20, tzinfo=UTC),
        )
        second = _two_component_diagnostic(
            metric_ids=(price_metric.result_id, volume_metric.result_id),
            as_of=as_of,
            reverse=True,
            computed_at=datetime(2026, 7, 21, tzinfo=UTC),
        )
        storage.diagnostics.save(first)
        storage.diagnostics.save(second)

        view = _query(storage)

        assert view.market.diagnostic == first
        assert view.market.revisions_superseded == 1
