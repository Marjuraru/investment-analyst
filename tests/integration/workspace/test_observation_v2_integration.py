"""Integration tests for observation v2 import, backup and PIT."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb
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
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.historical_analytical_import import (
    HistoricalAnalyticalImporter,
    HistoricalAnalyticalImportError,
)
from investment_analyst.storage.observation_v2_import import (
    ObservationV2Importer,
    ObservationV2ImportError,
)
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer, RawV2ImportState
from investment_analyst.workspace.raw_v2_backup import (
    RawV2StagingBackupService,
)
from investment_analyst.workspace.service import WorkspaceService

_TIMESTAMP = datetime(2026, 8, 1, tzinfo=UTC)
_FUTURE = datetime(2026, 9, 1, tzinfo=UTC)
_RECEIVED_FUTURE = datetime(2026, 9, 2, tzinfo=UTC)
_NORMALIZED_FUTURE = datetime(2026, 9, 3, tzinfo=UTC)


def _raw_record(index: int, *, available_at: datetime | None = None) -> RawRecord:
    future = available_at is not None
    return RawRecord(
        record_id=UUID(int=index + 1),
        asset_id="equity:us:aapl" if index % 3 else "crypto:btc-usd",
        source=SourceReference(
            source_id="alpaca:bars" if index % 2 else "sec:submissions",
            record_key=f"import-{index}",
            retrieved_at=_RECEIVED_FUTURE if future else _TIMESTAMP,
        ),
        event_time=_TIMESTAMP,
        available_at=available_at or _TIMESTAMP,
        received_at=_RECEIVED_FUTURE if future else _TIMESTAMP,
        payload={"value": str(index)},
        schema_version="import-v1",
    )


def _observation(index: int, raw: RawRecord) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=UUID(int=1000 + index + 1),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id or "equity:us:aapl",
        field_name="close",
        value=__import__("decimal").Decimal(f"{100 + index}.50"),
        unit="USD",
        frequency=DataFrequency.DAY_1 if index % 2 else DataFrequency.HOUR_1,
        observed_at=_TIMESTAMP,
        available_at=raw.available_at,
        normalized_at=_NORMALIZED_FUTURE,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )


def _seed_v1(source_root: Path, count: int) -> None:
    with LocalStorage(StoragePaths.from_root(source_root)) as writer:
        for index in range(count):
            raw = _raw_record(
                index,
                available_at=_FUTURE if index == count - 1 else _TIMESTAMP,
            )
            writer.raw_records.save(raw)
            writer.observations.save(_observation(index, raw))


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "obs-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _fingerprint(source: LocalStorage) -> str:
    import hashlib

    rows = source.store.connection.execute(
        "SELECT observation_id, document_json FROM normalized_observations ORDER BY observation_id"
    ).fetchall()
    digest = hashlib.sha256()
    for observation_id, document in rows:
        digest.update(str(observation_id).encode("utf-8"))
        digest.update(str(document).encode("utf-8"))
    return digest.hexdigest()


def _raw_digest(staging: RawV2Staging) -> str:
    return RawV2ImportState.model_validate_json(
        (staging.destination / "raw-v2-import-state.json").read_bytes()
    ).accumulated_digest


def _import_raw(
    source: LocalStorage, staging: RawV2Staging, *, page_limit: int = 2
) -> tuple[str, str]:
    fingerprint = _fingerprint(source)
    count = source.store.connection.execute("SELECT count(*) FROM raw_record_index").fetchone()
    assert count is not None
    raw_importer = RawV2Importer(
        source,
        staging,
        source_workspace_id=f"workspace-{count[0]}",
        source_fingerprint=fingerprint,
    )
    summary = raw_importer.run(page_limit=page_limit)
    assert summary.complete is True
    return f"workspace-{count[0]}", fingerprint


def _importer(
    source: LocalStorage,
    staging: RawV2Staging,
    workspace_id: str,
    fingerprint: str,
    raw_digest: str,
) -> ObservationV2Importer:
    return ObservationV2Importer(
        source,
        staging,
        source_workspace_id=workspace_id,
        source_fingerprint=fingerprint,
        raw_digest=raw_digest,
    )


def test_observation_v2_rejects_missing_or_foreign_raw(tmp_path: Path) -> None:
    from investment_analyst.storage.observation_v2 import ObservationV2Error

    source_root = tmp_path / "source"
    _seed_v1(source_root, 3)
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            workspace_id, _ = _import_raw(source, staging)
            fingerprint = _fingerprint(source)
            raw_digest = _raw_digest(staging)
            observations = source.observations.get_many(
                [UUID(int=1001 + index) for index in range(3)]
            )
            assert len(observations) == 3
            orphan = next(iter(observations.values())).model_copy(
                update={"raw_record_id": UUID(int=999999)}
            )
            with pytest.raises(ObservationV2Error, match="missing raw|foreign"):
                staging.save_observations([orphan])
            complete_first = _importer(source, staging, workspace_id, fingerprint, raw_digest).run(
                page_limit=2
            )
            assert complete_first.complete is True
            raw_state_before = (staging.destination / "raw-v2-import-state.json").read_bytes()
            observation_state_before = (
                staging.destination / "observation-v2-import-state.json"
            ).read_bytes()
            verified = _importer(
                source, staging, workspace_id, fingerprint, raw_digest
            ).verify_complete()
            assert verified.complete is True
            assert verified.imported_count == 3
            assert (staging.destination / "raw-v2-import-state.json").read_bytes() == (
                raw_state_before
            )
            assert (
                staging.destination / "observation-v2-import-state.json"
            ).read_bytes() == observation_state_before
            with pytest.raises(ObservationV2ImportError, match="another source"):
                _importer(source, staging, workspace_id, "0" * 64, raw_digest).run(page_limit=2)
            foreign_raw = _raw_record(999, available_at=_TIMESTAMP).model_copy(
                update={
                    "source": next(iter(observations.values())).source.model_copy(
                        update={"source_id": "foreign:source"}
                    )
                }
            )
            staging.save(foreign_raw)
            from uuid import uuid4 as _uuid4

            foreign_observation = next(iter(observations.values())).model_copy(
                update={"observation_id": _uuid4(), "raw_record_id": foreign_raw.record_id}
            )
            with pytest.raises(ObservationV2Error, match="missing raw|foreign"):
                staging.save_observations([foreign_observation])


def test_observation_import_relocates_and_resumes_without_duplicate_ids(
    tmp_path: Path,
) -> None:
    service = RawV2StagingBackupService()
    source_root = tmp_path / "source"
    _seed_v1(source_root, 5)
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            workspace_id, _ = _import_raw(source, staging)
            fingerprint = _fingerprint(source)
            raw_digest = _raw_digest(staging)
            importer = _importer(source, staging, workspace_id, fingerprint, raw_digest)
            with pytest.raises(ObservationV2ImportError, match="interrupted"):
                importer.run(page_limit=2, fail_after_page=0)
            partial = service.create(staging, staging._connection, tmp_path / "partial")
            assert partial.schema_version == "raw-v2-staging-backup-manifest-v2"
            assert partial.observation_counts is not None
            assert partial.observation_counts.observations == 2
        service.restore(tmp_path / "partial", tmp_path / "relocated")
        connection = duckdb.connect(str(tmp_path / "relocated" / "obs-v2-index.duckdb"))
        relocated = RawV2Staging(tmp_path / "relocated", connection)
        with relocated:
            resumed = _importer(source, relocated, workspace_id, fingerprint, raw_digest).run(
                page_limit=2
            )
            assert resumed.complete is True
            assert resumed.imported_count == 5
            rerun = _importer(source, relocated, workspace_id, fingerprint, raw_digest).run(
                page_limit=2
            )
            assert rerun.complete is True
            assert rerun.imported_count == 5
            hydrated = relocated.get_observations([UUID(int=1001 + index) for index in range(5)])
            assert len(hydrated) == 5
            assert len({item.observation_id for item in hydrated.values()}) == 5


def test_observation_import_rejects_corrupt_prefix_or_source_fingerprint(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 4)
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            workspace_id, _ = _import_raw(source, staging)
            fingerprint = _fingerprint(source)
            raw_digest = _raw_digest(staging)
            first = _importer(source, staging, workspace_id, fingerprint, raw_digest).run(
                page_limit=2
            )
            assert first.complete is True
            with pytest.raises(ObservationV2ImportError, match="another source|fingerprint"):
                _importer(source, staging, workspace_id, "0" * 64, raw_digest).run(page_limit=2)
            staging._connection.execute(
                "UPDATE normalized_observations_v2 SET value_text = ? WHERE observation_id = ?",
                ["9999.00", str(UUID(int=1001))],
            )
            with pytest.raises(Exception, match="differs|diverged|prefix|corrupt"):
                _importer(source, staging, workspace_id, fingerprint, raw_digest).run(page_limit=2)


def test_historical_analytical_backup_v6_restores_and_resumes_partial_import(
    tmp_path: Path,
) -> None:
    initialization = WorkspaceService().initialize(tmp_path / "source-workspace")
    source_paths = StoragePaths.from_root(initialization.paths.storage_root)
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    raw = RawRecord(
        record_id=UUID(int=5001),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="test:history", record_key="history-1", retrieved_at=timestamp
        ),
        event_time=timestamp,
        available_at=timestamp,
        received_at=timestamp,
        payload={"close": "210.50"},
        schema_version="history-v1",
    )
    observation = NormalizedObservation(
        observation_id=UUID(int=6001),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id or "equity:us:aapl",
        field_name="close",
        value=Decimal("210.50"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=timestamp,
        available_at=timestamp,
        normalized_at=timestamp,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    metric = MetricResult(
        result_id=UUID(int=7001),
        asset_id="equity:us:aapl",
        metric_key="market.close_copy",
        value=Decimal("210.50"),
        unit="USD",
        as_of=timestamp,
        available_at=timestamp,
        computed_at=timestamp,
        parameters={"window": 1},
        input_observation_ids=[observation.observation_id],
        algorithm_version="1.0.0",
        quality=DataQuality.VALID,
    )
    diagnostic = DiagnosticResult(
        diagnostic_id=UUID(int=8001),
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("80"),
        confidence=Decimal("0.75"),
        as_of=timestamp,
        available_at=timestamp,
        computed_at=timestamp,
        components=[
            DiagnosticComponent(
                component_key="market_test",
                score=Decimal("80"),
                weight=Decimal("1"),
                weighted_contribution=Decimal("80"),
                metric_result_ids=[metric.result_id],
                explanation="Test component.",
            )
        ],
        evidence=[
            DiagnosticEvidence(
                metric_result_id=metric.result_id,
                direction=EvidenceDirection.SUPPORTS,
                contribution=Decimal("1"),
                reason="Test evidence.",
            )
        ],
        algorithm_version="1.0.0",
        summary="Test diagnostic.",
        quality=DataQuality.VALID,
    )
    with LocalStorage(source_paths) as writer:
        writer.raw_records.save(raw)
        writer.observations.save(observation)
        writer.metric_results.save(metric)
        writer.diagnostics.save(diagnostic)

    service = RawV2StagingBackupService()
    with LocalStorage(source_paths, read_only=True) as source:
        staging = _staging(tmp_path, "history-staging")
        with staging:
            workspace_id = str(initialization.manifest.workspace_id)
            fingerprint = "historical-backup-source-fingerprint"
            raw_summary = RawV2Importer(
                source,
                staging,
                source_workspace_id=workspace_id,
                source_fingerprint=fingerprint,
            ).run(page_limit=1)
            ObservationV2Importer(
                source,
                staging,
                source_workspace_id=workspace_id,
                source_fingerprint=fingerprint,
                raw_digest=raw_summary.corpus_digest,
            ).run(page_limit=1)

            def interrupt_after_metric_commit(point: str) -> None:
                if point == "after_metric_page_commit_before_state":
                    raise HistoricalAnalyticalImportError("interrupted for backup test")

            importer = HistoricalAnalyticalImporter(
                source,
                staging,
                page_limit=1,
                failure_injector=interrupt_after_metric_commit,
            )
            with pytest.raises(HistoricalAnalyticalImportError, match="interrupted"):
                importer.run()
            partial = service.create(staging, staging._connection, tmp_path / "history-partial")
            assert partial.schema_version == "raw-v2-staging-backup-manifest-v6"
            assert partial.historical_analytical_counts is not None
            assert partial.historical_analytical_counts.complete is False
            assert partial.historical_analytical_counts.confirmed_metrics == 0
            assert partial.historical_analytical_counts.metrics == 1

        service.restore(tmp_path / "history-partial", tmp_path / "history-restored")
        restored_connection = duckdb.connect(
            str(tmp_path / "history-restored" / "obs-v2-index.duckdb")
        )
        restored = RawV2Staging(tmp_path / "history-restored", restored_connection)
        with restored:
            resumed = HistoricalAnalyticalImporter(
                source,
                restored,
                page_limit=1,
            ).run()
            assert resumed.complete is True
            assert resumed.metric_count == 1
            assert resumed.diagnostic_count == 1
            assert restored.historical_analytical_archive().verify_complete().complete is True
            complete = service.create(restored, restored._connection, tmp_path / "history-complete")
            assert complete.historical_analytical_counts is not None
            assert complete.historical_analytical_counts.complete is True

        service.restore(tmp_path / "history-complete", tmp_path / "history-final")
