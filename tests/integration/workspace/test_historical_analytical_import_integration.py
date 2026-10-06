"""End-to-end tests for portable historical analytical migration."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb

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
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.historical_analytical_import import HistoricalAnalyticalImporter
from investment_analyst.storage.observation_v2_import import ObservationV2Importer
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer
from investment_analyst.workspace.raw_v2_backup import RawV2StagingBackupService
from investment_analyst.workspace.service import WorkspaceService


def test_complete_historical_archive_survives_backup_restore_and_clock_change(
    tmp_path: Path,
) -> None:
    initialized = WorkspaceService().initialize(tmp_path / "source-workspace")
    paths = StoragePaths.from_root(initialized.paths.storage_root)
    available_at = datetime(2026, 8, 1, tzinfo=UTC)
    computed_at = available_at + timedelta(hours=1)
    raw = RawRecord(
        record_id=UUID(int=9101),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="test:historical",
            record_key="historical-record",
            retrieved_at=available_at,
        ),
        event_time=available_at,
        available_at=available_at,
        received_at=available_at,
        payload={"close": "210.50"},
        schema_version="integration-v1",
    )
    observation = NormalizedObservation(
        observation_id=UUID(int=9102),
        raw_record_id=raw.record_id,
        asset_id="equity:us:aapl",
        field_name="close",
        value=Decimal("210.5000"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=available_at,
        available_at=available_at,
        normalized_at=computed_at,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    metric = MetricResult(
        result_id=UUID(int=9103),
        asset_id="equity:us:aapl",
        metric_key="market.close_copy",
        value=Decimal("210.5000"),
        unit="USD",
        as_of=available_at,
        available_at=available_at,
        computed_at=computed_at,
        parameters={"known_at": "retained", "run_id": "original-run"},
        input_observation_ids=[observation.observation_id],
        algorithm_version="legacy-v1",
        quality=DataQuality.VALID,
    )
    diagnostic = DiagnosticResult(
        diagnostic_id=UUID(int=9104),
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("80.00"),
        confidence=Decimal("0.75"),
        as_of=available_at,
        available_at=available_at,
        computed_at=computed_at,
        components=[
            DiagnosticComponent(
                component_key="market-test",
                score=Decimal("80.00"),
                weight=Decimal("1.00"),
                weighted_contribution=Decimal("80.00"),
                metric_result_ids=[metric.result_id],
                explanation="Legacy component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("1.00"),
                reason="Legacy evidence.",
            )
        ],
        algorithm_version="legacy-v1",
        summary="Legacy diagnostic.",
        quality=DataQuality.VALID,
    )
    with LocalStorage(paths) as writer:
        writer.raw_records.save(raw)
        writer.observations.save(observation)
        writer.metric_results.save(metric)
        writer.diagnostics.save(diagnostic)
    source_manifest = (paths.root.parent / "manifest.json").read_bytes()

    backup_service = RawV2StagingBackupService()
    with LocalStorage(paths, read_only=True) as source:
        destination = (tmp_path / "staging").absolute()
        destination.mkdir()
        connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
        staging = RawV2Staging(destination, connection)
        with staging:
            workspace_id = str(initialized.manifest.workspace_id)
            fingerprint = "integration-source-fingerprint"
            raw_summary = RawV2Importer(
                source,
                staging,
                source_workspace_id=workspace_id,
                source_fingerprint=fingerprint,
            ).run(page_limit=2)
            ObservationV2Importer(
                source,
                staging,
                source_workspace_id=workspace_id,
                source_fingerprint=fingerprint,
                raw_digest=raw_summary.corpus_digest,
            ).run(page_limit=2)
            imported = HistoricalAnalyticalImporter(
                source,
                staging,
                page_limit=2,
                clock=lambda: datetime(2026, 8, 2, tzinfo=UTC),
            ).run()
            assert imported.complete is True
            assert imported.metric_count == 1
            assert imported.diagnostic_count == 1
            archive = staging.historical_analytical_archive()
            assert archive.get_metrics([metric.result_id])[metric.result_id] == metric
            assert (
                archive.get_diagnostics([diagnostic.diagnostic_id])[diagnostic.diagnostic_id]
                == diagnostic
            )
            rerun = HistoricalAnalyticalImporter(
                source,
                staging,
                page_limit=2,
                clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
            ).run()
            assert rerun.metric_created_count == 0
            assert rerun.diagnostic_created_count == 0
            manifest = backup_service.create(
                staging, staging._connection, tmp_path / "archive-backup"
            )
            assert manifest.schema_version == "raw-v2-staging-backup-manifest-v6"
            assert manifest.historical_analytical_counts is not None
            assert manifest.historical_analytical_counts.complete is True

        backup_service.restore(tmp_path / "archive-backup", tmp_path / "restored")
        restored_connection = duckdb.connect(str(tmp_path / "restored" / "raw-v2-index.duckdb"))
        restored = RawV2Staging(tmp_path / "restored", restored_connection)
        with restored:
            assert restored.historical_analytical_archive().verify_complete().complete is True
            assert restored.historical_analytical_archive().get_metrics([metric.result_id]) == {
                metric.result_id: metric
            }
    assert (paths.root.parent / "manifest.json").read_bytes() == source_manifest
