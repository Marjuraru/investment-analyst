"""Integration tests for raw v2 staging backup, restore and portable resume."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.storage.raw_v2_import import RawV2Importer
from investment_analyst.workspace.raw_v2_backup import (
    RawV2BackupError,
    RawV2StagingBackupService,
)

_TIMESTAMP = datetime(2026, 8, 1, tzinfo=UTC)
_FUTURE = datetime(2026, 9, 1, tzinfo=UTC)
_RECEIVED_FUTURE = datetime(2026, 9, 2, tzinfo=UTC)


def _raw_record(index: int, *, available_at: datetime | None = None) -> RawRecord:
    future = available_at is not None
    return RawRecord(
        record_id=UUID(int=index + 1),
        asset_id="equity:us:aapl" if index % 3 else "crypto:btc-usd",
        source=SourceReference(
            source_id="test:import",
            record_key=f"import-{index}",
            retrieved_at=_RECEIVED_FUTURE if future else _TIMESTAMP,
        ),
        event_time=_TIMESTAMP,
        available_at=available_at or _TIMESTAMP,
        received_at=_RECEIVED_FUTURE if future else _TIMESTAMP,
        payload=(
            {"report": {"manager_cik": "0001067983"}} if index % 3 == 2 else {"value": str(index)}
        ),
        schema_version="import-v1",
    )


def _seed_v1(source_root: Path, count: int) -> None:
    with LocalStorage(StoragePaths.from_root(source_root)) as writer:
        for index in range(count):
            writer.raw_records.save(
                _raw_record(
                    index,
                    available_at=_FUTURE if index == count - 1 else _TIMESTAMP,
                )
            )


def _staging(tmp_path: Path, name: str) -> RawV2Staging:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def _fingerprint(source: LocalStorage) -> str:
    import hashlib

    rows = source.store.connection.execute(
        "SELECT record_id, checksum_sha256 FROM raw_record_index ORDER BY record_id"
    ).fetchall()
    digest = hashlib.sha256()
    for record_id, checksum in rows:
        digest.update(str(record_id).encode("utf-8"))
        digest.update(str(checksum).encode("utf-8"))
    return digest.hexdigest()


def _importer(source: LocalStorage, staging: RawV2Staging) -> RawV2Importer:
    inspection = source.store.connection.execute("SELECT count(*) FROM raw_record_index").fetchone()
    assert inspection is not None
    return RawV2Importer(
        source,
        staging,
        source_workspace_id=f"workspace-{inspection[0]}",
        source_fingerprint=_fingerprint(source),
    )


def test_staging_backup_restore_preserves_raw_index_blobs_and_pit(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 6)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            manifest = service.create(staging, staging._connection, tmp_path / "backup")
            assert manifest.counts.records == 6
        restored_manifest = service.restore(tmp_path / "backup", tmp_path / "restored")
        assert restored_manifest.backup_id == manifest.backup_id
        second = service.restore(tmp_path / "backup", tmp_path / "restored-again")
        assert second.counts.corpus_digest == manifest.counts.corpus_digest
        connection = duckdb.connect(str(tmp_path / "restored" / "raw-v2-index.duckdb"))
        restored_staging = RawV2Staging(tmp_path / "restored", connection)
        with restored_staging:
            assert sorted(restored_staging.list_record_ids()) == sorted(
                staging.list_record_ids() if staging.is_open else restored_staging.list_record_ids()
            )
            cutoff = datetime(2026, 8, 15, tzinfo=UTC)
            current_cut = restored_staging.list_record_ids(available_to=cutoff)
            assert len(current_cut) == 5
            assert len(restored_staging.list_record_ids()) == 6


def test_relocated_partial_import_replays_unconfirmed_batch_once(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 5)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            with pytest.raises(Exception, match="interrupted"):
                importer.run(page_limit=2, fail_after_page=0)
            partial = service.create(staging, staging._connection, tmp_path / "partial")
            assert partial.counts.records == 2
        service.restore(tmp_path / "partial", tmp_path / "relocated")
        connection = duckdb.connect(str(tmp_path / "relocated" / "raw-v2-index.duckdb"))
        relocated = RawV2Staging(tmp_path / "relocated", connection)
        with relocated:
            resumed = _importer(source, relocated).run(page_limit=2)
            assert resumed.complete is True
            assert resumed.imported_count == 5
            rerun = _importer(source, relocated).run(page_limit=2)
            assert rerun.complete is True
            assert rerun.imported_count == 5


def test_restored_staging_preserves_two_pit_cuts_and_13f_projection(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 6)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            assert importer.run(page_limit=2).complete is True
            service.create(staging, staging._connection, tmp_path / "backup")
        service.restore(tmp_path / "backup", tmp_path / "restored")
        connection = duckdb.connect(str(tmp_path / "restored" / "raw-v2-index.duckdb"))
        restored = RawV2Staging(tmp_path / "restored", connection)
        with restored:
            early = restored.list_record_ids(available_to=datetime(2026, 8, 15, tzinfo=UTC))
            full = restored.list_record_ids()
            assert len(early) == 5
            assert len(full) == 6
            assert set(early) < set(full)
            assert len(restored.list_record_ids(manager_cik="0001067983")) == 2


def test_backup_rejects_active_writer_wal_symlink_and_overlap(tmp_path: Path) -> None:
    service = RawV2StagingBackupService()
    staging = _staging(tmp_path, "staging")
    with staging:
        staging.save(_raw_record(0))
        with pytest.raises(RawV2BackupError, match="outside the staging"):
            service.create(staging, staging._connection, staging.destination / "nested")
        foreign = _staging(tmp_path, "foreign")
        with foreign, pytest.raises(RawV2BackupError, match="already exists"):
            service.create(staging, staging._connection, foreign.destination)
    link = tmp_path / "link"
    try:
        link.symlink_to(staging.destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not supported here")
    with pytest.raises(RawV2BackupError, match="absolute|symbolic link"):
        service.create(
            RawV2Staging(link.absolute(), duckdb.connect(str(tmp_path / "x.duckdb"))),
            duckdb.connect(":memory:"),
            tmp_path / "backup-link",
        )


def test_restore_rejects_corrupt_or_foreign_inventory_without_promotion(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 4)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            importer = _importer(source, staging)
            assert importer.run(page_limit=2).complete is True
            manifest = service.create(staging, staging._connection, tmp_path / "backup")
            assert manifest.counts.records == 4
    victim = tmp_path / "backup" / "raw"
    blob: Path | None = None
    for candidate in victim.rglob("*"):
        if candidate.is_file() and not candidate.is_symlink():
            blob = candidate
            break
    assert blob is not None
    blob.write_bytes(b"corrupt")
    with pytest.raises(RawV2BackupError, match="hash verification|does not match"):
        service.restore(tmp_path / "backup", tmp_path / "restored-corrupt")
    assert not (tmp_path / "restored-corrupt").exists()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing.txt").write_text("busy", encoding="utf-8")
    with pytest.raises(RawV2BackupError, match="new or empty"):
        service.restore(tmp_path / "backup", occupied)


def test_observation_backup_restore_binds_full_inventory_and_pit(tmp_path: Path) -> None:
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from investment_analyst.core.models import (
        DataFrequency as _Frequency,
    )
    from investment_analyst.core.models import (
        DataQuality as _Quality,
    )
    from investment_analyst.core.models import (
        NormalizedObservation as _Observation,
    )
    from investment_analyst.storage.observation_v2_import import (
        ObservationV2Importer as _ObsImporter,
    )

    source_root = tmp_path / "source"
    _seed_v1(source_root, 6)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            assert _importer(source, staging).run(page_limit=2).complete is True
            raw_digest = (staging.destination / "raw-v2-import-state.json").read_bytes().hex()[:64]
            rows = source.store.connection.execute(
                "SELECT observation_id, document_json FROM normalized_observations "
                "ORDER BY observation_id"
            ).fetchall()
            import hashlib as _hashlib

            digest = _hashlib.sha256()
            for observation_id, document in rows:
                digest.update(str(observation_id).encode("utf-8"))
                digest.update(str(document).encode("utf-8"))
            fingerprint = digest.hexdigest()
            inspection = source.store.connection.execute(
                "SELECT count(*) FROM normalized_observations"
            ).fetchone()
            assert inspection is not None
            importer = _ObsImporter(
                source,
                staging,
                source_workspace_id=f"obs-workspace-{inspection[0]}",
                source_fingerprint=fingerprint,
                raw_digest=raw_digest,
            )
            summary = importer.run(page_limit=2)
            assert summary.complete is True
            assert summary.imported_count == 0
            manifest = service.create(staging, staging._connection, tmp_path / "backup")
            assert manifest.schema_version == "raw-v2-staging-backup-manifest-v2"
            assert manifest.observation_counts is not None
            assert manifest.observation_counts.observations == 0
            assert manifest.counts.records == 6
        restored_manifest = service.restore(tmp_path / "backup", tmp_path / "restored")
        assert restored_manifest.backup_id == manifest.backup_id
        connection = duckdb.connect(str(tmp_path / "restored" / "raw-v2-index.duckdb"))
        restored = RawV2Staging(tmp_path / "restored", connection)
        with restored:
            assert len(restored.list_record_ids()) == 6
            cutoff = _datetime(2026, 8, 15, tzinfo=_UTC)
            assert len(restored.list_record_ids(available_to=cutoff)) == 5
            assert _Frequency.DAY_1 is not None
            assert _Quality.VALID is not None
            assert _Observation is not None


def test_observation_restore_rejects_corrupt_index_or_checkpoint(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _seed_v1(source_root, 4)
    service = RawV2StagingBackupService()
    with LocalStorage(StoragePaths.from_root(source_root), read_only=True) as source:
        staging = _staging(tmp_path, "staging")
        with staging:
            assert _importer(source, staging).run(page_limit=2).complete is True
            manifest = service.create(staging, staging._connection, tmp_path / "backup")
            assert manifest.counts.records == 4
    manifest_path = tmp_path / "backup" / "raw-v2-staging-backup-manifest.json"
    document = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(document[:-10], encoding="utf-8")
    with pytest.raises(RawV2BackupError, match="truncated|incompatible"):
        service.restore(tmp_path / "backup", tmp_path / "restored-truncated")
    assert not (tmp_path / "restored-truncated").exists()


def test_metric_backup_restore_v3_preserves_pit_and_lineage(tmp_path: Path) -> None:
    from datetime import timedelta
    from decimal import Decimal
    from uuid import uuid4

    from investment_analyst.analytics.evidence_set import (
        build_evidence_segments as _segments,
    )
    from investment_analyst.analytics.evidence_set import (
        build_evidence_set as _build_set,
    )
    from investment_analyst.analytics.metric_identity_v2 import (
        metric_result_id_from_model_v2 as _metric_id,
    )
    from investment_analyst.core.models import (
        DataFrequency as _MetricFrequency,
    )
    from investment_analyst.core.models import (
        DataQuality as _MetricQuality,
    )
    from investment_analyst.core.models import (
        MetricResult as _Metric,
    )
    from investment_analyst.core.models import (
        NormalizedObservation as _MetricObservation,
    )
    from investment_analyst.core.models import (
        SourceReference as _MetricSource,
    )

    staging = _staging(tmp_path, "metric-staging")
    base = datetime(2026, 8, 1, tzinfo=UTC)
    with staging:
        observations: list[_MetricObservation] = []
        for index in range(48):
            moment = base + timedelta(hours=index)
            raw = RawRecord(
                record_id=uuid4(),
                asset_id="crypto:btc-usd",
                source=_MetricSource(
                    source_id="deribit:funding", record_key=f"v3-{index}", retrieved_at=moment
                ),
                event_time=moment,
                available_at=moment,
                received_at=moment,
                payload={"v": str(index)},
                schema_version="metric-v3-backup",
            )
            staging.save(raw)
            observations.append(
                _MetricObservation(
                    observation_id=uuid4(),
                    raw_record_id=raw.record_id,
                    asset_id="crypto:btc-usd",
                    field_name="funding_rate",
                    value=Decimal(f"0.00{index % 10}"),
                    unit="rate",
                    frequency=_MetricFrequency.HOUR_1,
                    observed_at=moment,
                    available_at=moment,
                    normalized_at=moment,
                    source=raw.source,
                    quality=_MetricQuality.VALID,
                    transformation_version="1.0.0",
                )
            )
        assert staging.save_observations(observations).created_count == 48
        segments = _segments(observations)
        assert staging.save_evidence_segments(segments) == 2
        evidence_set = _build_set(observations, segments=segments)
        assert staging.save_evidence_set(evidence_set) is True
        window_end = base + timedelta(hours=47)

        def _metric(key: str, value: Decimal) -> _Metric:
            candidate = _Metric(
                result_id=uuid4(),
                asset_id="crypto:btc-usd",
                metric_key=key,
                value=value,
                unit="rate",
                as_of=window_end,
                available_at=evidence_set.available_at,
                computed_at=evidence_set.available_at,
                parameters={
                    "window": len(observations),
                    "evidence_set_id": str(evidence_set.evidence_set_id),
                },
                input_observation_ids=[item.observation_id for item in observations],
                algorithm_version="metric-v3-backup",
                quality=_MetricQuality.VALID,
            )
            return candidate.model_copy(update={"result_id": _metric_id(candidate)})

        total = _metric("funding.sum_1h", Decimal("2.5"))
        mean = _metric("funding.mean_1h", Decimal("0.05"))
        assert staging.save_metrics([total, mean]).created_count == 2
        service = RawV2StagingBackupService()
        manifest = service.create(staging, staging._connection, tmp_path / "metric-backup")
        assert manifest.schema_version == "raw-v2-staging-backup-manifest-v3"
        assert manifest.metric_counts is not None
        assert manifest.metric_counts.metrics == 2
        assert manifest.metric_counts.evidence_sets == 1
        assert manifest.metric_counts.evidence_segments == 2
    restored_manifest = service.restore(tmp_path / "metric-backup", tmp_path / "metric-restored")
    assert restored_manifest.backup_id == manifest.backup_id
    connection = duckdb.connect(str(tmp_path / "metric-restored" / "raw-v2-index.duckdb"))
    restored = RawV2Staging(tmp_path / "metric-restored", connection)
    with restored:
        both = restored.get_metrics([total.result_id, mean.result_id])
        assert both[total.result_id] == total
        assert both[mean.result_id] == mean
        assert restored.get_evidence_set(evidence_set.evidence_set_id) == evidence_set
        assert restored.list_metrics(available_to=base) == []
        assert len(restored.list_metrics(available_to=evidence_set.available_at)) == 2


def test_metric_restore_rejects_corrupt_lineage_or_missing_observation(
    tmp_path: Path,
) -> None:
    from datetime import timedelta
    from decimal import Decimal
    from uuid import uuid4

    from investment_analyst.analytics.evidence_set import (
        build_evidence_segments as _lineage_segments,
    )
    from investment_analyst.analytics.evidence_set import (
        build_evidence_set as _lineage_set,
    )
    from investment_analyst.analytics.metric_identity_v2 import (
        metric_result_id_from_model_v2 as _lineage_id,
    )
    from investment_analyst.core.models import (
        DataFrequency as _LineageFrequency,
    )
    from investment_analyst.core.models import (
        DataQuality as _LineageQuality,
    )
    from investment_analyst.core.models import (
        MetricResult as _LineageMetric,
    )
    from investment_analyst.core.models import (
        NormalizedObservation as _LineageObservation,
    )
    from investment_analyst.core.models import (
        SourceReference as _LineageSource,
    )

    staging = _staging(tmp_path, "lineage-staging")
    base = datetime(2026, 8, 1, tzinfo=UTC)
    with staging:
        observations: list[_LineageObservation] = []
        for index in range(24):
            moment = base + timedelta(hours=index)
            raw = RawRecord(
                record_id=uuid4(),
                asset_id="crypto:btc-usd",
                source=_LineageSource(
                    source_id="deribit:funding",
                    record_key=f"neg-{index}",
                    retrieved_at=moment,
                ),
                event_time=moment,
                available_at=moment,
                received_at=moment,
                payload={"v": str(index)},
                schema_version="metric-v3-negative",
            )
            staging.save(raw)
            observations.append(
                _LineageObservation(
                    observation_id=uuid4(),
                    raw_record_id=raw.record_id,
                    asset_id="crypto:btc-usd",
                    field_name="funding_rate",
                    value=Decimal("0.01"),
                    unit="rate",
                    frequency=_LineageFrequency.HOUR_1,
                    observed_at=moment,
                    available_at=moment,
                    normalized_at=moment,
                    source=raw.source,
                    quality=_LineageQuality.VALID,
                    transformation_version="1.0.0",
                )
            )
        assert staging.save_observations(observations).created_count == 24
        segments = _lineage_segments(observations)
        staging.save_evidence_segments(segments)
        evidence_set = _lineage_set(observations, segments=segments)
        staging.save_evidence_set(evidence_set)
        candidate = _LineageMetric(
            result_id=uuid4(),
            asset_id="crypto:btc-usd",
            metric_key="funding.sum_1h",
            value=Decimal("1.0"),
            unit="rate",
            as_of=base + timedelta(hours=23),
            available_at=evidence_set.available_at,
            computed_at=evidence_set.available_at,
            parameters={
                "window": len(observations),
                "evidence_set_id": str(evidence_set.evidence_set_id),
            },
            input_observation_ids=[item.observation_id for item in observations],
            algorithm_version="metric-v3-negative",
            quality=_LineageQuality.VALID,
        )
        fixed = candidate.model_copy(update={"result_id": _lineage_id(candidate)})
        assert staging.save_metrics([fixed]).created_count == 1
        service = RawV2StagingBackupService()
        manifest = service.create(staging, staging._connection, tmp_path / "lineage-backup")
        assert manifest.schema_version == "raw-v2-staging-backup-manifest-v3"
    manifest_path = tmp_path / "lineage-backup" / "raw-v2-staging-backup-manifest.json"
    document = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(document[:-12], encoding="utf-8")
    with pytest.raises(RawV2BackupError, match="truncated|incompatible"):
        service.restore(tmp_path / "lineage-backup", tmp_path / "lineage-corrupt")
    assert not (tmp_path / "lineage-corrupt").exists()


def test_analysis_backup_v4_restores_diagnostics_and_snapshots(tmp_path: Path) -> None:
    """A3/A4: backup v4 binds diagnostics and snapshots, and restore preserves IDs, Decimal, PIT."""
    from decimal import Decimal
    from uuid import uuid4

    from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
    from investment_analyst.analytics.evidence_set import (
        build_evidence_segments as _lineage_segments,
    )
    from investment_analyst.analytics.evidence_set import (
        build_evidence_set as _lineage_set,
    )
    from investment_analyst.analytics.metric_identity_v2 import (
        metric_result_id_from_model_v2 as _lineage_id,
    )
    from investment_analyst.core.models import (
        DataFrequency as _LineageFrequency,
    )
    from investment_analyst.core.models import (
        DataQuality as _LineageQuality,
    )
    from investment_analyst.core.models import (
        DiagnosticComponent,
        DiagnosticEvidence,
        DiagnosticMode,
        DiagnosticResult,
        DiagnosticVerdict,
        EvidenceDirection,
    )
    from investment_analyst.core.models import (
        MetricResult as _LineageMetric,
    )
    from investment_analyst.core.models import (
        NormalizedObservation as _LineageObservation,
    )
    from investment_analyst.core.models import (
        SourceReference as _LineageSource,
    )
    from investment_analyst.workspace.raw_v2_backup import (
        RAW_V2_BACKUP_MANIFEST_SCHEMA_V4,
    )

    staging = _staging(tmp_path, "analysis-staging")
    base = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "crypto:btc-usd"
    with staging:
        observations: list[_LineageObservation] = []
        for index in range(24):
            moment = base + timedelta(hours=index)
            raw = RawRecord(
                record_id=uuid4(),
                asset_id=asset_id,
                source=_LineageSource(
                    source_id="deribit:funding",
                    record_key=f"an-obs-{index}",
                    retrieved_at=moment,
                ),
                event_time=moment,
                available_at=moment,
                received_at=moment,
                payload={"v": str(index)},
                schema_version="analysis-v4-test",
            )
            staging.save(raw)
            observations.append(
                _LineageObservation(
                    observation_id=uuid4(),
                    raw_record_id=raw.record_id,
                    asset_id=asset_id,
                    field_name="funding_rate",
                    value=Decimal("0.01"),
                    unit="rate",
                    frequency=_LineageFrequency.HOUR_1,
                    observed_at=moment,
                    available_at=moment,
                    normalized_at=moment,
                    source=raw.source,
                    quality=_LineageQuality.VALID,
                    transformation_version="1.0.0",
                )
            )
        staging.save_observations(observations)
        segments = _lineage_segments(observations)
        staging.save_evidence_segments(segments)
        evidence_set = _lineage_set(observations, segments=segments)
        staging.save_evidence_set(evidence_set)

        candidate = _LineageMetric(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="crypto.derivatives.funding.sum_1h",
            value=Decimal("1.2345"),
            unit="rate",
            as_of=base + timedelta(hours=23),
            available_at=evidence_set.available_at,
            computed_at=evidence_set.available_at,
            parameters={
                "window": len(observations),
                "evidence_set_id": str(evidence_set.evidence_set_id),
            },
            input_observation_ids=[item.observation_id for item in observations],
            algorithm_version="v1",
            quality=_LineageQuality.VALID,
        )
        metric = candidate.model_copy(update={"result_id": _lineage_id(candidate)})
        staging.save_metrics([metric])

        diagnostic = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.95"),
            as_of=metric.as_of,
            available_at=metric.available_at,
            computed_at=metric.computed_at,
            components=[
                DiagnosticComponent(
                    component_key="funding_pressure",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[metric.result_id],
                    explanation="Elevated funding rate",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=metric.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="Funding is positive",
                )
            ],
            algorithm_version="v1",
            summary="Market diagnostic positive",
            quality=_LineageQuality.VALID,
        )
        staging.save_diagnostics([diagnostic])

        snapshot = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=metric.available_at,
            policy_version="v1",
            metric_ids=[metric.result_id],
            diagnostic_ids=[diagnostic.diagnostic_id],
            evidence_set_hashes=[evidence_set.canonical_hash],
            created_at=datetime(2026, 8, 2, 10, 0, tzinfo=UTC),
        )
        staging.save_analysis_snapshots([snapshot])

        service = RawV2StagingBackupService()
        manifest = service.create(staging, staging._connection, tmp_path / "analysis-backup")
        assert manifest.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V4
        assert manifest.analysis_counts is not None
        assert manifest.analysis_counts.diagnostics == 1
        assert manifest.analysis_counts.snapshots == 1

    restored_manifest = service.restore(
        tmp_path / "analysis-backup", tmp_path / "analysis-restored"
    )
    assert restored_manifest.backup_id == manifest.backup_id
    connection = duckdb.connect(str(tmp_path / "analysis-restored" / "raw-v2-index.duckdb"))
    restored = RawV2Staging(tmp_path / "analysis-restored", connection)
    with restored:
        diag_map = restored.get_diagnostics([diagnostic.diagnostic_id])
        assert diag_map[diagnostic.diagnostic_id] == diagnostic
        restored_snap = restored.get_analysis_snapshot(snapshot.snapshot_id)
        assert restored_snap == snapshot
        assert restored.list_diagnostics(as_of=metric.as_of) == [diagnostic]
        assert restored.list_analysis_snapshots(known_to=metric.available_at) == [snapshot]


def test_analysis_v4_restore_verifies_full_references_in_bounded_pages(tmp_path: Path) -> None:
    """A4: Smoke test raw -> obs -> >256 metrics -> diags -> snaps across 2 assets and 2 cuts."""
    import shutil
    from decimal import Decimal
    from uuid import uuid4

    from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
    from investment_analyst.analytics.evidence_set import (
        build_evidence_segments,
        build_evidence_set,
    )
    from investment_analyst.analytics.metric_identity_v2 import (
        metric_result_id_from_model_v2,
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
    from investment_analyst.workspace.raw_v2_backup import (
        RAW_V2_BACKUP_MANIFEST_SCHEMA_V4,
        RawV2BackupError,
        RawV2StagingBackupService,
    )

    staging = _staging(tmp_path, "smoke-staging")
    asset_equity = "equity:us:aapl"
    asset_crypto = "crypto:btc-usd"
    cut1 = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    cut2 = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)

    with staging:
        # 1. Raw records and normalized observations (2 assets, 2 cuts)
        raw_records = []
        observations = []
        es_map = {}

        for asset, source_name, field in (
            (asset_equity, "alpaca:bars", "close"),
            (asset_crypto, "deribit:funding", "funding_rate"),
        ):
            es_map[asset] = {}
            for cut_idx, cut in enumerate((cut1, cut2)):
                raw = RawRecord(
                    record_id=uuid4(),
                    asset_id=asset,
                    source=SourceReference(
                        source_id=source_name,
                        record_key=f"rec-{asset}-{cut_idx}",
                        retrieved_at=cut,
                    ),
                    event_time=cut,
                    available_at=cut,
                    received_at=cut,
                    payload={"idx": cut_idx},
                    schema_version="v1",
                )
                staging.save(raw)
                raw_records.append(raw)

                obs = NormalizedObservation(
                    observation_id=uuid4(),
                    raw_record_id=raw.record_id,
                    asset_id=asset,
                    field_name=field,
                    value=Decimal("150.00") if asset == asset_equity else Decimal("0.01"),
                    unit="USD" if asset == asset_equity else "rate",
                    frequency=DataFrequency.HOUR_1,
                    observed_at=cut,
                    available_at=cut,
                    normalized_at=cut,
                    source=raw.source,
                    quality=DataQuality.VALID,
                    transformation_version="1.0.0",
                )
                staging.save_observations([obs])
                observations.append(obs)

                segs = build_evidence_segments([obs])
                staging.save_evidence_segments(segs)
                es = build_evidence_set([obs], segments=segs)
                staging.save_evidence_set(es)
                es_map[asset][cut] = es

        # 2. >256 metrics across assets and cuts (130 for equity, 130 for crypto = 260 metrics)
        all_metrics = []
        equity_cut1_m = []
        equity_cut2_m = []
        crypto_cut1_m = []
        crypto_cut2_m = []

        for i in range(130):
            cut = cut1 if i < 65 else cut2
            es = es_map[asset_equity][cut]
            obs = [o for o in observations if o.asset_id == asset_equity and o.observed_at == cut][
                0
            ]
            cand = MetricResult(
                result_id=uuid4(),
                asset_id=asset_equity,
                metric_key=f"market.close.idx_{i}",
                value=Decimal(str(100 + i)),
                unit="USD",
                as_of=cut,
                available_at=cut,
                computed_at=cut,
                parameters={"idx": i, "evidence_set_id": str(es.evidence_set_id)},
                input_observation_ids=[obs.observation_id],
                input_metric_result_ids=[],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            m = cand.model_copy(update={"result_id": metric_result_id_from_model_v2(cand)})
            all_metrics.append(m)
            if i < 65:
                equity_cut1_m.append(m)
            else:
                equity_cut2_m.append(m)

        for i in range(130):
            cut = cut1 if i < 65 else cut2
            es = es_map[asset_crypto][cut]
            obs = [o for o in observations if o.asset_id == asset_crypto and o.observed_at == cut][
                0
            ]
            cand = MetricResult(
                result_id=uuid4(),
                asset_id=asset_crypto,
                metric_key=f"crypto.derivatives.funding.idx_{i}",
                value=Decimal(str(i)) / Decimal("1000"),
                unit="rate",
                as_of=cut,
                available_at=cut,
                computed_at=cut,
                parameters={"idx": i, "evidence_set_id": str(es.evidence_set_id)},
                input_observation_ids=[obs.observation_id],
                input_metric_result_ids=[],
                algorithm_version="v1",
                quality=DataQuality.VALID,
            )
            m = cand.model_copy(update={"result_id": metric_result_id_from_model_v2(cand)})
            all_metrics.append(m)
            if i < 65:
                crypto_cut1_m.append(m)
            else:
                crypto_cut2_m.append(m)

        assert len(all_metrics) == 260
        staging.save_metrics(all_metrics)

        # 3. Diagnostics referencing metrics
        all_diags = []
        diag_eq_1 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_equity,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=cut1,
            available_at=cut1,
            computed_at=cut1,
            components=[
                DiagnosticComponent(
                    component_key="market_trend",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[equity_cut1_m[0].result_id],
                    explanation="AAPL positive trend",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=equity_cut1_m[0].result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="AAPL price supported",
                )
            ],
            algorithm_version="v1",
            summary="AAPL cut1 diag",
            quality=DataQuality.VALID,
        )
        diag_eq_2 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_equity,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.9"),
            as_of=cut2,
            available_at=cut2,
            computed_at=cut2,
            components=[
                DiagnosticComponent(
                    component_key="market_trend",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[equity_cut2_m[0].result_id],
                    explanation="AAPL positive trend cut2",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=equity_cut2_m[0].result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="AAPL cut2 price supported",
                )
            ],
            algorithm_version="v1",
            summary="AAPL cut2 diag",
            quality=DataQuality.VALID,
        )
        diag_cr_1 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_crypto,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("70.0"),
            confidence=Decimal("0.85"),
            as_of=cut1,
            available_at=cut1,
            computed_at=cut1,
            components=[
                DiagnosticComponent(
                    component_key="deriv_trend",
                    score=Decimal("70.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("70.0"),
                    metric_result_ids=[crypto_cut1_m[0].result_id],
                    explanation="BTC funding positive",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=crypto_cut1_m[0].result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("70.0"),
                    reason="BTC funding supported",
                )
            ],
            algorithm_version="v1",
            summary="BTC cut1 diag",
            quality=DataQuality.VALID,
        )
        diag_cr_2 = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_crypto,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("70.0"),
            confidence=Decimal("0.85"),
            as_of=cut2,
            available_at=cut2,
            computed_at=cut2,
            components=[
                DiagnosticComponent(
                    component_key="deriv_trend",
                    score=Decimal("70.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("70.0"),
                    metric_result_ids=[crypto_cut2_m[0].result_id],
                    explanation="BTC funding positive cut2",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=crypto_cut2_m[0].result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("70.0"),
                    reason="BTC cut2 funding supported",
                )
            ],
            algorithm_version="v1",
            summary="BTC cut2 diag",
            quality=DataQuality.VALID,
        )
        all_diags.extend([diag_eq_1, diag_eq_2, diag_cr_1, diag_cr_2])
        staging.save_diagnostics(all_diags)

        # 4. Snapshots referencing metrics, diagnostics, and evidence sets
        all_snaps = []
        snap_eq_1 = build_analysis_snapshot(
            asset_id=asset_equity,
            domain="market",
            known_at=cut1,
            policy_version="v1",
            metric_ids=[m.result_id for m in equity_cut1_m],
            diagnostic_ids=[diag_eq_1.diagnostic_id],
            evidence_set_hashes=[es_map[asset_equity][cut1].canonical_hash],
            created_at=cut1,
        )
        snap_eq_2 = build_analysis_snapshot(
            asset_id=asset_equity,
            domain="market",
            known_at=cut2,
            policy_version="v1",
            metric_ids=[m.result_id for m in equity_cut2_m],
            diagnostic_ids=[diag_eq_2.diagnostic_id],
            evidence_set_hashes=[es_map[asset_equity][cut2].canonical_hash],
            created_at=cut2,
        )
        snap_cr_1 = build_analysis_snapshot(
            asset_id=asset_crypto,
            domain="derivatives",
            known_at=cut1,
            policy_version="v1",
            metric_ids=[m.result_id for m in crypto_cut1_m],
            diagnostic_ids=[diag_cr_1.diagnostic_id],
            evidence_set_hashes=[es_map[asset_crypto][cut1].canonical_hash],
            created_at=cut1,
        )
        snap_cr_2 = build_analysis_snapshot(
            asset_id=asset_crypto,
            domain="derivatives",
            known_at=cut2,
            policy_version="v1",
            metric_ids=[m.result_id for m in crypto_cut2_m],
            diagnostic_ids=[diag_cr_2.diagnostic_id],
            evidence_set_hashes=[es_map[asset_crypto][cut2].canonical_hash],
            created_at=cut2,
        )
        all_snaps.extend([snap_eq_1, snap_eq_2, snap_cr_1, snap_cr_2])
        staging.save_analysis_snapshots(all_snaps)

        # 5. Create backup v4
        service = RawV2StagingBackupService()
        backup_path = tmp_path / "smoke-backup-v4"
        manifest = service.create(staging, staging._connection, backup_path)
        assert manifest.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V4
        assert manifest.counts.records == 4
        assert manifest.observation_counts.observations == 4
        assert manifest.metric_counts.metrics == 260
        assert manifest.analysis_counts.diagnostics == 4
        assert manifest.analysis_counts.snapshots == 4

    # 6. Reject restore from corrupted backup without promoting destination
    corrupt_backup = tmp_path / "corrupt-backup"
    shutil.copytree(backup_path, corrupt_backup)
    (corrupt_backup / "raw-v2-index.duckdb").write_bytes(b"corrupt-database-bytes")
    corrupt_dest = tmp_path / "corrupt-restored"
    with pytest.raises(RawV2BackupError):
        service.restore(corrupt_backup, corrupt_dest)
    assert not corrupt_dest.exists()

    # 7. Restore to separate destination path
    restored_path = tmp_path / "restored-staging"
    restored_manifest = service.restore(backup_path, restored_path)
    assert restored_manifest.backup_id == manifest.backup_id

    # 8. Reopen restored staging and verify full references
    connection = duckdb.connect(str(restored_path / "raw-v2-index.duckdb"))
    restored = RawV2Staging(restored_path, connection)
    with restored:
        # Keyset pagination on restored diagnostics (pages of 2)
        paged_diags = []
        cur_at = None
        cur_id = None
        while True:
            page = restored.list_diagnostics(limit=2, cursor_at=cur_at, cursor_id=cur_id)
            if not page:
                break
            paged_diags.extend(page)
            cur_at = page[-1].available_at
            cur_id = page[-1].diagnostic_id
        assert len(paged_diags) == 4
        assert {d.diagnostic_id for d in paged_diags} == {d.diagnostic_id for d in all_diags}

        # Keyset pagination on restored snapshots (pages of 2)
        paged_snaps = []
        cur_at = None
        cur_id = None
        while True:
            page = restored.list_analysis_snapshots(limit=2, cursor_at=cur_at, cursor_id=cur_id)
            if not page:
                break
            paged_snaps.extend(page)
            cur_at = page[-1].known_at
            cur_id = page[-1].snapshot_id
        assert len(paged_snaps) == 4
        assert {s.snapshot_id for s in paged_snaps} == {s.snapshot_id for s in all_snaps}

        # Direct rehydration and references
        hydrated_m = restored.get_metrics([m.result_id for m in all_metrics])
        assert len(hydrated_m) == 260
        for orig in all_metrics:
            rec = hydrated_m[orig.result_id]
            assert rec.metric_key == orig.metric_key
            assert rec.value == orig.value
            assert rec.input_observation_ids == orig.input_observation_ids

        hydrated_d = restored.get_diagnostics([d.diagnostic_id for d in all_diags])
        assert len(hydrated_d) == 4
        for orig in all_diags:
            rec = hydrated_d[orig.diagnostic_id]
            assert rec == orig

        hydrated_s = restored.get_analysis_snapshots([s.snapshot_id for s in all_snaps])
        assert len(hydrated_s) == 4
        for orig in all_snaps:
            rec = hydrated_s[orig.snapshot_id]
            assert rec == orig

        # 9. Idempotence: re-saving already existing data produces zero creates
        assert restored.save_metrics(all_metrics).created_count == 0
        assert restored.save_diagnostics(all_diags).created_count == 0
        assert restored.save_analysis_snapshots(all_snaps).created_count == 0

    # 10. Second restore to clean destination is idempotent
    restored_path_2 = tmp_path / "restored-staging-2"
    second_manifest = service.restore(backup_path, restored_path_2)
    assert second_manifest.backup_id == manifest.backup_id


def test_analysis_restore_rejects_corrupt_links_without_promotion(tmp_path: Path) -> None:
    """X1: corruption of diagnostic/snapshot links rejects restore without promotion."""
    from decimal import Decimal
    from uuid import uuid4

    from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
    from investment_analyst.analytics.evidence_set import (
        build_evidence_segments as _lineage_segments,
    )
    from investment_analyst.analytics.evidence_set import (
        build_evidence_set as _lineage_set,
    )
    from investment_analyst.analytics.metric_identity_v2 import (
        metric_result_id_from_model_v2 as _lineage_id,
    )
    from investment_analyst.core.models import (
        DataFrequency as _LineageFrequency,
    )
    from investment_analyst.core.models import (
        DataQuality as _LineageQuality,
    )
    from investment_analyst.core.models import (
        DiagnosticComponent,
        DiagnosticEvidence,
        DiagnosticMode,
        DiagnosticResult,
        DiagnosticVerdict,
        EvidenceDirection,
    )
    from investment_analyst.core.models import (
        MetricResult as _LineageMetric,
    )
    from investment_analyst.core.models import (
        NormalizedObservation as _LineageObservation,
    )
    from investment_analyst.core.models import (
        SourceReference as _LineageSource,
    )

    staging = _staging(tmp_path, "corrupt-source-staging")
    base = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "crypto:btc-usd"
    with staging:
        observations: list[_LineageObservation] = []
        for index in range(24):
            moment = base + timedelta(hours=index)
            raw = RawRecord(
                record_id=uuid4(),
                asset_id=asset_id,
                source=_LineageSource(
                    source_id="deribit:funding",
                    record_key=f"corrupt-obs-{index}",
                    retrieved_at=moment,
                ),
                event_time=moment,
                available_at=moment,
                received_at=moment,
                payload={"v": str(index)},
                schema_version="corrupt-test",
            )
            staging.save(raw)
            observations.append(
                _LineageObservation(
                    observation_id=uuid4(),
                    raw_record_id=raw.record_id,
                    asset_id=asset_id,
                    field_name="funding_rate",
                    value=Decimal("0.01"),
                    unit="rate",
                    frequency=_LineageFrequency.HOUR_1,
                    observed_at=moment,
                    available_at=moment,
                    normalized_at=moment,
                    source=raw.source,
                    quality=_LineageQuality.VALID,
                    transformation_version="1.0.0",
                )
            )
        staging.save_observations(observations)
        segments = _lineage_segments(observations)
        staging.save_evidence_segments(segments)
        evidence_set = _lineage_set(observations, segments=segments)
        staging.save_evidence_set(evidence_set)

        candidate = _LineageMetric(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="crypto.derivatives.funding.sum_1h",
            value=Decimal("1.0"),
            unit="rate",
            as_of=base + timedelta(hours=23),
            available_at=evidence_set.available_at,
            computed_at=evidence_set.available_at,
            parameters={
                "window": len(observations),
                "evidence_set_id": str(evidence_set.evidence_set_id),
            },
            input_observation_ids=[item.observation_id for item in observations],
            algorithm_version="v1",
            quality=_LineageQuality.VALID,
        )
        metric = candidate.model_copy(update={"result_id": _lineage_id(candidate)})
        staging.save_metrics([metric])

        diagnostic = DiagnosticResult(
            diagnostic_id=uuid4(),
            asset_id=asset_id,
            mode=DiagnosticMode.MARKET,
            verdict=DiagnosticVerdict.POSITIVE,
            final_score=Decimal("80.0"),
            confidence=Decimal("0.95"),
            as_of=metric.as_of,
            available_at=metric.available_at,
            computed_at=metric.computed_at,
            components=[
                DiagnosticComponent(
                    component_key="funding_pressure",
                    score=Decimal("80.0"),
                    weight=Decimal("1.0"),
                    weighted_contribution=Decimal("80.0"),
                    metric_result_ids=[metric.result_id],
                    explanation="Elevated funding rate",
                )
            ],
            evidence=[
                DiagnosticEvidence(
                    metric_result_id=metric.result_id,
                    direction=EvidenceDirection.SUPPORTS,
                    contribution=Decimal("80.0"),
                    reason="Funding is positive",
                )
            ],
            algorithm_version="v1",
            summary="Market diagnostic positive",
            quality=_LineageQuality.VALID,
        )
        staging.save_diagnostics([diagnostic])

        snapshot = build_analysis_snapshot(
            asset_id=asset_id,
            domain="derivatives",
            known_at=metric.available_at,
            policy_version="v1",
            metric_ids=[metric.result_id],
            diagnostic_ids=[diagnostic.diagnostic_id],
            evidence_set_hashes=[evidence_set.canonical_hash],
            created_at=datetime(2026, 8, 2, 10, 0, tzinfo=UTC),
        )
        staging.save_analysis_snapshots([snapshot])

        service = RawV2StagingBackupService()
        service.create(staging, staging._connection, tmp_path / "valid-analysis-backup")

    # Corrupt index db inside backup by altering diagnostic links to a non-existent metric
    backup_db_path = tmp_path / "valid-analysis-backup" / "raw-v2-index.duckdb"
    corrupt_conn = duckdb.connect(str(backup_db_path))
    corrupt_conn.execute(
        f"UPDATE diagnostic_v2_component_metric_links SET metric_result_id = '{uuid4()}'"
    )
    corrupt_conn.close()

    # Attempting restore must fail closed without creating the promoted directory
    with pytest.raises(RawV2BackupError):
        service.restore(tmp_path / "valid-analysis-backup", tmp_path / "corrupt-promoted")
    assert not (tmp_path / "corrupt-promoted").exists()


def test_restore_rejects_tampered_metric_or_position_gap_without_promotion(
    tmp_path: Path,
) -> None:
    """Restore rejects tampered metric key and member position gaps before promotion."""
    import hashlib
    import shutil

    from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
    from investment_analyst.analytics.evidence_set import (
        build_evidence_segments,
        build_evidence_set,
    )
    from investment_analyst.analytics.metric_identity_v2 import (
        metric_result_id_from_model_v2,
    )
    from investment_analyst.core.models import (
        DataFrequency,
        DataQuality,
        MetricResult,
        NormalizedObservation,
    )
    from investment_analyst.workspace.raw_v2_backup import (
        BACKUP_MANIFEST_NAME,
        RawV2StagingBackupManifest,
    )

    staging = _staging(tmp_path, "source-staging-tamper")
    base = datetime(2026, 8, 1, tzinfo=UTC)
    asset_id = "equity:us:aapl"

    with staging:
        obs_list = []
        for i in range(48):
            moment = base + timedelta(hours=i)
            raw = RawRecord(
                record_id=UUID(int=100 + i),
                asset_id=asset_id,
                source=SourceReference(
                    source_id="alpaca:test",
                    record_key=f"obs-{i}",
                    retrieved_at=moment,
                ),
                event_time=moment,
                available_at=moment,
                received_at=moment,
                payload={"v": i},
                schema_version="test-v1",
            )
            staging.save(raw)
            obs = NormalizedObservation(
                observation_id=UUID(int=200 + i),
                raw_record_id=raw.record_id,
                asset_id=asset_id,
                field_name="close",
                value=Decimal("150.00"),
                unit="USD",
                frequency=DataFrequency.HOUR_1,
                observed_at=moment,
                available_at=moment,
                normalized_at=moment,
                source=raw.source,
                quality=DataQuality.VALID,
                transformation_version="1.0.0",
            )
            obs_list.append(obs)
        staging.save_observations(obs_list)
        segs = build_evidence_segments(obs_list)
        staging.save_evidence_segments(segs)
        es = build_evidence_set(obs_list, segments=segs)
        staging.save_evidence_set(es)

        metric_time = obs_list[-1].available_at
        cand = MetricResult(
            result_id=uuid4(),
            asset_id=asset_id,
            metric_key="market.close.price",
            value=Decimal("150.00"),
            unit="USD",
            as_of=metric_time,
            available_at=metric_time,
            computed_at=metric_time,
            parameters={"evidence_set_id": str(es.evidence_set_id)},
            input_observation_ids=[o.observation_id for o in obs_list],
            algorithm_version="v1",
            quality=DataQuality.VALID,
        )
        metric = cand.model_copy(update={"result_id": metric_result_id_from_model_v2(cand)})
        staging.save_metrics([metric])

        snap = build_analysis_snapshot(
            asset_id=asset_id,
            domain="market",
            known_at=metric_time,
            policy_version="v1",
            metric_ids=[metric.result_id],
            diagnostic_ids=[],
            evidence_set_hashes=[es.canonical_hash],
            created_at=metric_time,
        )
        staging.save_analysis_snapshots([snap])

        service = RawV2StagingBackupService()
        valid_backup = tmp_path / "valid-backup-semantic"
        service.create(staging, staging._connection, valid_backup)

    def _update_manifest_hash(backup_dir: Path) -> None:
        from investment_analyst.workspace.raw_v2_backup import _backup_id

        manifest_file = backup_dir / BACKUP_MANIFEST_NAME
        manifest_data = RawV2StagingBackupManifest.model_validate_json(
            manifest_file.read_text(encoding="utf-8")
        )
        db_path = backup_dir / "raw-v2-index.duckdb"
        new_sha = hashlib.sha256(db_path.read_bytes()).hexdigest()
        new_size = db_path.stat().st_size
        updated_files = tuple(
            item.model_copy(update={"sha256": new_sha, "size_bytes": new_size})
            if item.path == "raw-v2-index.duckdb"
            else item
            for item in manifest_data.files
        )
        new_bid = _backup_id(
            manifest_data.staging_id,
            updated_files,
            manifest_data.counts,
            manifest_data.observation_counts,
            manifest_data.schema_version,
            manifest_data.metric_counts,
            manifest_data.analysis_counts,
        )
        manifest_file.write_text(
            manifest_data.model_copy(
                update={"files": updated_files, "backup_id": new_bid}
            ).model_dump_json(),
            encoding="utf-8",
        )

    # 1. Tamper metric_key in backup database and update manifest file hash
    backup_tampered_key = tmp_path / "backup-tampered-key"
    shutil.copytree(valid_backup, backup_tampered_key)
    conn = duckdb.connect(str(backup_tampered_key / "raw-v2-index.duckdb"))
    conn.execute("UPDATE metric_results_v2 SET metric_key = 'market.tampered.price'")
    conn.close()
    _update_manifest_hash(backup_tampered_key)

    dest_key = tmp_path / "promoted-tampered-key"
    with pytest.raises(RawV2BackupError, match="restored metric row is corrupt"):
        service.restore(backup_tampered_key, dest_key)
    assert not dest_key.exists()

    # 2. Tamper evidence_set_v2_members position to create a gap [0, 5]
    backup_tampered_pos = tmp_path / "backup-tampered-pos"
    shutil.copytree(valid_backup, backup_tampered_pos)
    conn = duckdb.connect(str(backup_tampered_pos / "raw-v2-index.duckdb"))
    conn.execute("UPDATE evidence_set_v2_members SET position = 5 WHERE position = 1")
    conn.close()
    _update_manifest_hash(backup_tampered_pos)

    dest_pos = tmp_path / "promoted-tampered-pos"
    with pytest.raises(
        RawV2BackupError, match="restored evidence set members have non-contiguous positions"
    ):
        service.restore(backup_tampered_pos, dest_pos)
    assert not dest_pos.exists()

    # 3. Tamper metric_v2_observation_links position to create a gap [0, 99, 2, ...]
    backup_tampered_obs_links = tmp_path / "backup-tampered-obs-links"
    shutil.copytree(valid_backup, backup_tampered_obs_links)
    conn = duckdb.connect(str(backup_tampered_obs_links / "raw-v2-index.duckdb"))
    conn.execute("UPDATE metric_v2_observation_links SET position = 99 WHERE position = 1")
    conn.close()
    _update_manifest_hash(backup_tampered_obs_links)

    dest_obs_links = tmp_path / "promoted-tampered-obs-links"
    with pytest.raises(
        RawV2BackupError, match="restored metric observation links have non-contiguous positions"
    ):
        service.restore(backup_tampered_obs_links, dest_obs_links)
    assert not dest_obs_links.exists()
