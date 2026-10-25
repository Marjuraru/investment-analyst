"""Integration coverage for selective historical recovery selection and import."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

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
    HistoricalRecoveryImporter,
    HistoricalRecoverySelectionBuilder,
    load_historical_recovery_manifest,
    verify_historical_recovery_selection,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_KNOWN_AT = datetime(2026, 10, 22, 12, tzinfo=UTC)
_ASSET = "equity:us:aapl"
_MARKET_SOURCE = "fixture:integration-market"
_FUNDAMENTAL_SOURCE = "sec-edgar:aapl:companyfacts"
_DOCUMENT_SOURCE = "sec-edgar:primary-documents"
_DOCUMENT_BYTES = b"integration primary document bytes"


def _source(source_id: str, key: str, checksum: str | None = None) -> SourceReference:
    return SourceReference(
        source_id=source_id,
        record_key=key,
        checksum_sha256=checksum,
        retrieved_at=_KNOWN_AT,
    )


def _raw(
    *,
    source_id: str,
    key: str,
    schema_version: str,
    available_at: datetime,
    payload: dict[str, object] | None = None,
    checksum: str | None = None,
    identity: str,
) -> RawRecord:
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"integration-raw-{identity}"),
        asset_id=_ASSET,
        source=_source(source_id, key, checksum),
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload=payload or {"identity": identity},
        schema_version=schema_version,
    )


def _observation(
    raw: RawRecord,
    *,
    field_name: str,
    value: Decimal,
    frequency: DataFrequency,
    observed_at: datetime | None,
    period_end: datetime | None,
    available_at: datetime,
    identity: str,
) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"integration-observation-{identity}"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name=field_name,
        value=value,
        unit="USD",
        frequency=frequency,
        observed_at=observed_at,
        period_end=period_end,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="integration-fixture-v1",
    )


def _metric(observation: NormalizedObservation, *, identity: str) -> MetricResult:
    return MetricResult(
        result_id=uuid5(NAMESPACE_URL, f"integration-metric-{identity}"),
        asset_id=_ASSET,
        metric_key="market.technical.sma",
        value=Decimal("101.2500"),
        unit="USD",
        as_of=observation.available_at,
        available_at=observation.available_at,
        computed_at=observation.available_at + timedelta(minutes=2),
        parameters={"source_id": _MARKET_SOURCE, "window": 3},
        input_observation_ids=[observation.observation_id],
        algorithm_version="integration-fixture-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic(metric: MetricResult, *, identity: str) -> DiagnosticResult:
    return DiagnosticResult(
        diagnostic_id=uuid5(NAMESPACE_URL, f"integration-diagnostic-{identity}"),
        asset_id=_ASSET,
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("60.00"),
        confidence=Decimal("0.75"),
        as_of=metric.as_of,
        available_at=metric.available_at,
        computed_at=metric.computed_at + timedelta(minutes=1),
        components=[
            DiagnosticComponent(
                component_key="trend",
                score=Decimal("60.00"),
                weight=Decimal("1.00"),
                weighted_contribution=Decimal("60.00"),
                metric_result_ids=[metric.result_id],
                explanation="Integration fixture component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("0.75"),
                reason="Integration fixture evidence.",
            )
        ],
        algorithm_version="integration-fixture-v1",
        summary="Integration fixture diagnostic.",
        quality=DataQuality.VALID,
    )


def _seed_source(root: Path) -> Path:
    service = WorkspaceService(environ={}, home=root / "home")
    initialized = service.initialize(root / "v1", format_version=1)
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        document_receipt = storage.documents.put(_DOCUMENT_BYTES)
        market_raw = _raw(
            source_id=_MARKET_SOURCE,
            key="AAPL:2026-10-20",
            schema_version="fixture-market-v1",
            available_at=datetime(2026, 10, 20, tzinfo=UTC),
            identity="market",
        )
        market_observation = _observation(
            market_raw,
            field_name="close",
            value=Decimal("101.25"),
            frequency=DataFrequency.DAY_1,
            observed_at=datetime(2026, 10, 20, tzinfo=UTC),
            period_end=None,
            available_at=datetime(2026, 10, 20, 20, tzinfo=UTC),
            identity="market",
        )
        fundamental_raw = _raw(
            source_id=_FUNDAMENTAL_SOURCE,
            key="AAPL:FY2025",
            schema_version="fixture-companyfacts-v1",
            available_at=datetime(2025, 10, 30, tzinfo=UTC),
            identity="fundamental",
        )
        fundamental_observation = _observation(
            fundamental_raw,
            field_name="Revenues",
            value=Decimal("400000000000"),
            frequency=DataFrequency.ANNUAL,
            observed_at=None,
            period_end=datetime(2025, 9, 27, tzinfo=UTC),
            available_at=datetime(2025, 10, 30, tzinfo=UTC),
            identity="fundamental",
        )
        document_raw = _raw(
            source_id=_DOCUMENT_SOURCE,
            key="AAPL:document:1",
            schema_version="fixture-document-v1",
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            payload={"content_sha256": document_receipt.sha256},
            checksum=document_receipt.sha256,
            identity="document",
        )
        for raw, observation in (
            (market_raw, market_observation),
            (fundamental_raw, fundamental_observation),
            (document_raw, None),
        ):
            storage.raw_records.save(raw)
            if observation is not None:
                storage.observations.save(observation)
        metric = _metric(market_observation, identity="market")
        diagnostic = _diagnostic(metric, identity="market")
        storage.metric_results.save(metric)
        storage.diagnostics.save(diagnostic)
    return initialized.paths.root


def _seed_destination(root: Path) -> Path:
    service = WorkspaceService(environ={}, home=root / "home")
    return service.initialize(root / "v2", format_version=2).paths.root


def _build(root: Path) -> tuple[Path, Path, Path]:
    source_root = _seed_source(root)
    destination_root = _seed_destination(root)
    service = WorkspaceService(environ={}, home=root / "home")
    source_paths = service.resolve(source_root)
    destination_paths = service.resolve(destination_root)
    builder = HistoricalRecoverySelectionBuilder(
        service=service,
        source_paths=source_paths,
        destination_paths=destination_paths,
        source_inspection=service.inspect(source_paths.root),
        destination_inspection=service.inspect(destination_paths.root),
    )
    output = root / "selection"
    builder.build(known_at=_KNOWN_AT, output_root=output)
    return source_root, destination_root, output


def test_selection_and_import_round_trip(tmp_path: Path) -> None:
    source_root, destination_root, output = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    assert manifest.totals.raw_records == 3
    assert manifest.totals.observations == 2
    assert manifest.totals.metric_results == 1
    assert manifest.totals.diagnostic_results == 1
    assert manifest.totals.document_contents == 1
    assert manifest.totals.import_required == manifest.totals.items
    verify_historical_recovery_selection(manifest, output)

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    checkpoint = tmp_path / "checkpoint.json"
    source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(
        service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
    )
    with source, destination:
        summary = HistoricalRecoveryImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            manifest_root=output,
            checkpoint_path=checkpoint,
            source_fingerprint=manifest.source_fingerprint,
        ).run()
    assert summary.created_raw == 3
    assert summary.created_observations == 2
    assert summary.created_metrics == 1
    assert summary.created_diagnostics == 1
    assert summary.created_documents == 1
    inspection = service.inspect(destination_root)
    assert inspection.raw_record_count == 3
    assert inspection.observation_count == 2
    assert inspection.metric_result_count == 1
    assert inspection.diagnostic_result_count == 1


def test_import_replay_is_idempotent(tmp_path: Path) -> None:
    source_root, destination_root, output = _build(tmp_path)
    manifest = load_historical_recovery_manifest(output / "selection.json")
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    checkpoint = tmp_path / "checkpoint.json"
    for expected_replay in (False, True):
        source = service.open_storage(service.resolve(source_root), WorkspaceAccessMode.READ_ONLY)
        destination = service.open_storage(
            service.resolve(destination_root), WorkspaceAccessMode.READ_WRITE
        )
        with source, destination:
            summary = HistoricalRecoveryImporter(
                source=source,
                destination=destination,
                manifest=manifest,
                manifest_root=output,
                checkpoint_path=checkpoint,
                source_fingerprint=manifest.source_fingerprint,
            ).run()
        assert summary.replay is expected_replay
        if expected_replay:
            assert summary.created_raw == 0
            assert summary.reused_raw == 3
