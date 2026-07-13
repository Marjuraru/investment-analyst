"""Shared model factories for storage tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_analyst.core.models import (
    Asset,
    AssetClass,
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricCategory,
    MetricDefinition,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.storage import LocalStorage, StoragePaths


@pytest.fixture
def storage(tmp_path):
    with LocalStorage(StoragePaths.from_root(tmp_path)) as local_storage:
        yield local_storage


def make_asset(asset_id: str = "equity:us:aapl") -> Asset:
    return Asset(
        asset_id=asset_id,
        symbol="AAPL",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
        provider_symbols={"alpaca": "AAPL"},
        is_active=True,
    )


def make_source_definition() -> SourceDefinition:
    return SourceDefinition(
        source_id="alpaca:bars",
        provider_name="Alpaca",
        dataset_name="Historical bars",
        source_type=SourceType.MARKET,
        base_url="https://example.invalid",
        is_official=True,
        coverage_notes="Market prices.",
    )


def make_source_reference(source_id: str = "alpaca:bars") -> SourceReference:
    return SourceReference(
        source_id=source_id,
        record_key="AAPL:2026-07-10",
        retrieved_at=datetime(2026, 7, 10, 16, 3, tzinfo=UTC),
    )


def make_raw_record(
    *,
    record_id: UUID | None = None,
    source_id: str = "alpaca:bars",
    received_at: datetime | None = None,
) -> RawRecord:
    received = received_at or datetime(2026, 7, 10, 16, 3, tzinfo=UTC)
    return RawRecord(
        record_id=record_id or uuid4(),
        asset_id="equity:us:aapl",
        source=make_source_reference(source_id),
        event_time=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        received_at=received,
        payload={"close": "210.50", "sequence": 1},
        schema_version="1",
    )


def make_observation(
    *,
    raw_record_id: UUID,
    observation_id: UUID | None = None,
    asset_id: str = "equity:us:aapl",
    available_at: datetime | None = None,
    value: Decimal = Decimal("210.50"),
) -> NormalizedObservation:
    available = available_at or datetime(2026, 7, 10, 16, 1, tzinfo=UTC)
    return NormalizedObservation(
        observation_id=observation_id or uuid4(),
        raw_record_id=raw_record_id,
        asset_id=asset_id,
        field_name="close",
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        available_at=available,
        normalized_at=datetime(2026, 7, 10, 16, 4, tzinfo=UTC),
        source=make_source_reference(),
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )


def make_metric_definition() -> MetricDefinition:
    return MetricDefinition(
        metric_key="market.close_copy",
        display_name="Close copy",
        category=MetricCategory.MARKET,
        description="Test metric preserving the close value.",
        formula="close",
        unit="USD",
        default_parameters={"window": 1},
        limitations=["Test-only definition."],
        references=["Internal test."],
        definition_version="1.0.0",
    )


def make_metric_result(
    *,
    observation_id: UUID,
    result_id: UUID | None = None,
    asset_id: str = "equity:us:aapl",
    as_of: datetime | None = None,
    metric_key: str = "market.close_copy",
) -> MetricResult:
    timestamp = as_of or datetime(2026, 7, 10, 16, 0, tzinfo=UTC)
    return MetricResult(
        result_id=result_id or uuid4(),
        asset_id=asset_id,
        metric_key=metric_key,
        value=Decimal("210.50"),
        unit="USD",
        as_of=timestamp,
        available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, 16, 5, tzinfo=UTC),
        parameters={"window": 1},
        input_observation_ids=[observation_id],
        algorithm_version="1.0.0",
        quality=DataQuality.VALID,
    )


def make_diagnostic_result(
    *,
    metric_result_id: UUID,
    diagnostic_id: UUID | None = None,
    asset_id: str = "equity:us:aapl",
    as_of: datetime | None = None,
    mode: DiagnosticMode = DiagnosticMode.MARKET,
) -> DiagnosticResult:
    timestamp = as_of or datetime(2026, 7, 10, 16, 0, tzinfo=UTC)
    component = DiagnosticComponent(
        component_key="market_test",
        score=Decimal("80"),
        weight=Decimal("1"),
        weighted_contribution=Decimal("80"),
        metric_result_ids=[metric_result_id],
        explanation="Test component.",
    )
    evidence = DiagnosticEvidence(
        metric_result_id=metric_result_id,
        direction=EvidenceDirection.SUPPORTS,
        contribution=Decimal("1"),
        reason="Test evidence.",
    )
    return DiagnosticResult(
        diagnostic_id=diagnostic_id or uuid4(),
        asset_id=asset_id,
        mode=mode,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("80"),
        confidence=Decimal("0.75"),
        as_of=timestamp,
        available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, 16, 6, tzinfo=UTC),
        components=[component],
        evidence=[evidence],
        algorithm_version="1.0.0",
        summary="Test diagnostic.",
        quality=DataQuality.VALID,
    )
