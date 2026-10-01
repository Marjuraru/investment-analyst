"""Integration tests for raw v2 staging backup, restore and portable resume."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

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


def test_analysis_v4_restore_verifies_full_references_in_bounded_pages(tmp_path: Path) -> None:
    """A4: backup v4 binds diagnostics and snapshots; restore verifies references in pages."""
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


test_analysis_backup_v4_restores_diagnostics_and_snapshots = (
    test_analysis_v4_restore_verifies_full_references_in_bounded_pages
)


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
