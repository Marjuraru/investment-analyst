"""Integration coverage for PIT admission before historical writes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

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
from investment_analyst.workspace.historical_recovery import (
    HistoricalRecoveryClassification,
    HistoricalRecoveryEntityKind,
    HistoricalRecoveryError,
    HistoricalRecoveryImporter,
    HistoricalRecoveryItem,
    HistoricalRecoverySelectionBuilder,
    load_historical_recovery_manifest,
    verify_historical_recovery_selection,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 1, 15, tzinfo=UTC)
_ASSET = "equity:us:aapl"
_SOURCE_ID = "fixture:pit-integration"
_INVALID_METRIC = uuid5(NAMESPACE_URL, "pit-integration-metric-invalid")
_INVALID_DIAGNOSTIC = uuid5(NAMESPACE_URL, "pit-integration-diagnostic-invalid")
_VALID_METRIC = uuid5(NAMESPACE_URL, "pit-integration-metric-valid")


def _source(key: str) -> SourceReference:
    return SourceReference(source_id=_SOURCE_ID, record_key=key, retrieved_at=_BASE)


def _raw(key: str) -> RawRecord:
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"pit-integration-raw-{key}"),
        asset_id=_ASSET,
        source=_source(key),
        event_time=_BASE,
        available_at=_BASE,
        received_at=_BASE,
        payload={"key": key},
        schema_version="pit-integration-v1",
    )


def _observation(raw: RawRecord, *, key: str, available: datetime) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"pit-integration-observation-{key}"),
        raw_record_id=raw.record_id,
        asset_id=_ASSET,
        field_name="close",
        value=Decimal("100.00"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_BASE,
        available_at=available,
        normalized_at=available,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="pit-integration-v1",
    )


def _metric(observation: NormalizedObservation, *, key: str, available: datetime) -> MetricResult:
    return MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"pit-integration-metric-{key}"),
        asset_id=_ASSET,
        metric_key="market.technical.sma",
        value=Decimal("100.00"),
        unit="USD",
        as_of=available,
        available_at=available,
        computed_at=available,
        parameters={"source_id": _SOURCE_ID},
        input_observation_ids=[observation.observation_id],
        algorithm_version="pit-integration-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic(metric: MetricResult, *, key: str, available: datetime) -> DiagnosticResult:
    return DiagnosticResult(
        diagnostic_id=uuid5(NAMESPACE_URL, f"pit-integration-diagnostic-{key}"),
        asset_id=_ASSET,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("60.00"),
        confidence=Decimal("0.75"),
        as_of=available,
        available_at=available,
        computed_at=available,
        components=[
            DiagnosticComponent(
                component_key="trend",
                score=Decimal("60.00"),
                weight=Decimal("1.00"),
                weighted_contribution=Decimal("60.00"),
                metric_result_ids=[metric.result_id],
                explanation="Fixture component",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.75"),
                reason="Fixture evidence",
            )
        ],
        algorithm_version="pit-integration-v1",
        summary="Fixture diagnostic",
        quality=DataQuality.VALID,
    )


def _seed(tmp_path: Path):
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    v1 = service.initialize(tmp_path / "v1", format_version=1)
    v2 = service.initialize(tmp_path / "v2", format_version=2)
    valid_raw = _raw("valid")
    invalid_raw = _raw("invalid")
    valid_observation = _observation(valid_raw, key="valid", available=_BASE)
    invalid_observation = _observation(invalid_raw, key="invalid", available=_BASE)
    valid_metric = _metric(valid_observation, key="valid", available=_BASE + timedelta(days=1))
    invalid_metric = _metric(
        invalid_observation, key="invalid", available=_BASE + timedelta(days=1)
    )
    valid_diagnostic = _diagnostic(valid_metric, key="valid", available=_BASE + timedelta(days=2))
    invalid_diagnostic = _diagnostic(
        invalid_metric, key="invalid", available=_BASE + timedelta(days=2)
    )
    with service.open_storage(v1.paths, WorkspaceAccessMode.READ_WRITE) as source:
        source.raw_records.save(valid_raw)
        source.raw_records.save(invalid_raw)
        source.observations.save(valid_observation)
        source.observations.save(invalid_observation)
        source.metric_results.save(valid_metric)
        source.metric_results.save(invalid_metric)
        source.diagnostics.save(valid_diagnostic)
        source.diagnostics.save(invalid_diagnostic)
    with service.open_storage(v2.paths, WorkspaceAccessMode.READ_WRITE) as destination:
        destination.raw_records.save(valid_raw)
        destination.raw_records.save(invalid_raw)
        destination.observations.save(valid_observation)
        destination.observations.save(
            invalid_observation.model_copy(
                update={
                    "available_at": _BASE + timedelta(days=10),
                    "normalized_at": _BASE + timedelta(days=10),
                }
            )
        )
    source_paths = v1.paths
    destination_paths = v2.paths
    builder = HistoricalRecoverySelectionBuilder(
        service=service,
        source_paths=source_paths,
        destination_paths=destination_paths,
        source_inspection=service.inspect(source_paths.root),
        destination_inspection=service.inspect(destination_paths.root),
    )
    output = tmp_path / "selection"
    builder.build(known_at=_KNOWN_AT, output_root=output)
    return service, source_paths, destination_paths, output, invalid_metric


def _items(manifest, output: Path) -> list[HistoricalRecoveryItem]:
    items: list[HistoricalRecoveryItem] = []
    for page in manifest.pages:
        data = (output / page.relative_path).read_text()
        items.extend(
            HistoricalRecoveryItem.model_validate_json(line) for line in data.splitlines() if line
        )
    return items


def test_selector_archives_pit_conflict_and_dependent_diagnostic(tmp_path: Path) -> None:
    _, _, _, output, _ = _seed(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    verify_historical_recovery_selection(manifest, output)
    items = _items(manifest, output)
    invalid_metric = next(item for item in items if item.entity_id == _INVALID_METRIC)
    invalid_diagnostic = next(item for item in items if item.entity_id == _INVALID_DIAGNOSTIC)
    valid_metric = next(item for item in items if item.entity_id == _VALID_METRIC)
    assert invalid_metric.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY
    assert invalid_metric.admission_reason == "PIT_AVAILABLE_AT"
    assert invalid_diagnostic.classification is HistoricalRecoveryClassification.ARCHIVE_ONLY
    assert invalid_diagnostic.admission_reason == "PIT_DEPENDENCY"
    assert valid_metric.classification is HistoricalRecoveryClassification.IMPORTAR_FALTANTE
    assert manifest.totals.archive_only == 2
    assert manifest.totals.import_required == 2
    assert manifest.totals.metric_results == 2
    assert manifest.totals.diagnostic_results == 2


def test_importer_rejects_present_later_input_before_write(tmp_path: Path) -> None:
    service, source_paths, destination_paths, output, _ = _seed(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    item = next(
        item
        for item in _items(manifest, output)
        if item.entity_kind is HistoricalRecoveryEntityKind.METRIC_RESULT
        and item.entity_id == _INVALID_METRIC
    )
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(destination_paths, WorkspaceAccessMode.READ_WRITE)
    with source, destination:
        importer = HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=tmp_path / "checkpoint.json",
            source_fingerprint=manifest.source_fingerprint,
        )
        with pytest.raises(HistoricalRecoveryError, match="PIT admission"):
            importer._admit_pit(HistoricalRecoveryEntityKind.METRIC_RESULT, [item])
