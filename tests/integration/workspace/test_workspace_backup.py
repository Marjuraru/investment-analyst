"""Integration coverage for verified local workspace backup and restore."""

import gc
import json
import weakref
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest
from duckdb import DuckDBPyConnection
from pydantic import BaseModel

from investment_analyst.application.asset_preferences import (
    AssetPreferenceEntry,
    AssetPreferencesStore,
    AssetPreferencesUpdate,
    EffectiveAssetPreferences,
    asset_preferences_fingerprint,
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
from investment_analyst.storage import raw_records as raw_records_module
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.raw_records import JsonRawRecordRepository
from investment_analyst.storage.repositories import (
    DuckDBDiagnosticResultRepository,
    DuckDBMetricResultRepository,
    DuckDBObservationRepository,
)
from investment_analyst.storage.serialization import model_from_json
from investment_analyst.workspace import backup as backup_module
from investment_analyst.workspace.backup import (
    BACKUP_MANIFEST_NAME,
    WorkspaceBackupError,
    WorkspaceBackupService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService


def _service(tmp_path: Path) -> tuple[WorkspaceService, WorkspaceBackupService, Path]:
    workspace_service = WorkspaceService(
        environ={},
        home=tmp_path / "home",
        clock=lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )
    workspace = tmp_path / "source"
    initialized = workspace_service.initialize(workspace)
    (initialized.paths.state_root / "scheduler.json").write_text(
        '{"schema_version":"test-state-v1"}\n',
        encoding="utf-8",
    )
    return (
        workspace_service,
        WorkspaceBackupService(
            workspace_service,
            clock=lambda: datetime(2026, 8, 1, hour=1, tzinfo=UTC),
        ),
        workspace,
    )


def _seed_traceable_layers(
    workspace_service: WorkspaceService,
    source: Path,
    *,
    count: int,
    cross_page_metric_references: bool = False,
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...], tuple[UUID, ...]]:
    raw_ids = tuple(UUID(int=index + 1) for index in range(count))
    observation_ids = tuple(UUID(int=100_000 + index) for index in range(count))
    metric_ids = tuple(UUID(int=200_000 + index) for index in range(count))
    diagnostic_ids = tuple(UUID(int=300_000 + index) for index in range(count))
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    writer = workspace_service.open_storage(
        workspace_service.resolve(source),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        for index in range(count):
            reference = SourceReference(
                source_id="test:workspace",
                record_key=f"bounded-{index}",
                retrieved_at=timestamp,
            )
            writer.raw_records.save(
                RawRecord(
                    record_id=raw_ids[index],
                    asset_id="equity:us:aapl",
                    source=reference,
                    event_time=timestamp,
                    available_at=timestamp,
                    received_at=timestamp,
                    payload={"value": str(index)},
                    schema_version="backup-bounded-test-v1",
                )
            )
            writer.observations.save(
                NormalizedObservation(
                    observation_id=observation_ids[index],
                    raw_record_id=raw_ids[index],
                    asset_id="equity:us:aapl",
                    field_name="close",
                    value=Decimal(index),
                    unit="USD",
                    frequency=DataFrequency.DAY_1,
                    observed_at=timestamp,
                    available_at=timestamp,
                    normalized_at=timestamp,
                    source=reference,
                    quality=DataQuality.VALID,
                    transformation_version="backup-bounded-test-v1",
                )
            )
            derived_ids: list[UUID] = []
            if cross_page_metric_references and count > backup_module._TRACEABILITY_BATCH_SIZE:
                if index == backup_module._TRACEABILITY_BATCH_SIZE - 1:
                    derived_ids = [metric_ids[index + 1]]
                elif index == backup_module._TRACEABILITY_BATCH_SIZE:
                    derived_ids = [metric_ids[index - 1]]
            input_observation_ids = [observation_ids[index]]
            if cross_page_metric_references and index == 0:
                input_observation_ids = list(observation_ids)
            writer.metric_results.save(
                MetricResult(
                    result_id=metric_ids[index],
                    asset_id="equity:us:aapl",
                    metric_key="market.technical.ema",
                    value=Decimal(index),
                    unit="USD",
                    as_of=timestamp,
                    available_at=timestamp,
                    computed_at=timestamp,
                    parameters={"known_at": timestamp.isoformat()},
                    input_observation_ids=input_observation_ids,
                    input_metric_result_ids=derived_ids,
                    algorithm_version="market-ema-v1-decimal34",
                    quality=DataQuality.VALID,
                )
            )
            writer.diagnostics.save(
                DiagnosticResult(
                    diagnostic_id=diagnostic_ids[index],
                    asset_id="equity:us:aapl",
                    mode=DiagnosticMode.MARKET,
                    verdict=DiagnosticVerdict.NEUTRAL,
                    final_score=Decimal("50"),
                    confidence=Decimal("1"),
                    as_of=timestamp,
                    available_at=timestamp,
                    computed_at=timestamp,
                    components=[
                        DiagnosticComponent(
                            component_key="bounded",
                            score=Decimal("50"),
                            weight=Decimal("1"),
                            weighted_contribution=Decimal("50"),
                            metric_result_ids=(
                                list(metric_ids)
                                if cross_page_metric_references and index == 0
                                else [metric_ids[index]]
                            ),
                            explanation="bounded backup fixture",
                        )
                    ],
                    evidence=[
                        DiagnosticEvidence(
                            metric_result_id=metric_ids[index],
                            direction=EvidenceDirection.NEUTRAL,
                            contribution=Decimal("0"),
                            reason="bounded backup fixture",
                        )
                    ],
                    algorithm_version="backup-bounded-test-v1",
                    summary="bounded backup fixture",
                    quality=DataQuality.VALID,
                )
            )
    finally:
        writer.close()
    return raw_ids, observation_ids, metric_ids, diagnostic_ids


def _legacy_traceability_error(service: WorkspaceService, root: Path) -> str | None:
    storage: LocalStorage | None = None
    try:
        storage = service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_ONLY)
        raw_records = storage.raw_records.list()
        observations = storage.observations.list()
        metrics = storage.metric_results.list()
        diagnostics = storage.diagnostics.list()
    except (duckdb.Error, OSError, StorageError, WorkspaceError, ValueError):
        return "workspace traceability could not be verified"
    finally:
        if storage is not None:
            storage.close()
    raw_ids = {item.record_id for item in raw_records}
    observation_ids = {item.observation_id for item in observations}
    metric_ids = {item.result_id for item in metrics}
    if any(item.raw_record_id not in raw_ids for item in observations):
        return "workspace contains an observation without its raw record"
    if any(
        input_id not in observation_ids
        for metric in metrics
        for input_id in metric.input_observation_ids
    ):
        return "workspace contains a metric without its observations"
    if any(
        input_id not in metric_ids
        for metric in metrics
        for input_id in metric.input_metric_result_ids
    ):
        return "workspace contains a metric without its derived metrics"
    diagnostic_metric_ids = {
        metric_id
        for diagnostic in diagnostics
        for component in diagnostic.components
        for metric_id in component.metric_result_ids
    } | {
        evidence.metric_result_id for diagnostic in diagnostics for evidence in diagnostic.evidence
    }
    if not diagnostic_metric_ids.issubset(metric_ids):
        return "workspace contains a diagnostic without its metrics"
    return None


def _bounded_traceability_error(service: WorkspaceService, root: Path) -> str | None:
    inspection = service.inspect(root)
    try:
        backup_module._verify_workspace_traceability(
            service,
            root,
            expected_counts=backup_module._counts(inspection),
        )
    except WorkspaceBackupError as error:
        return str(error)
    return None


def _replace_document_reference(
    connection: DuckDBPyConnection,
    *,
    table: str,
    primary_id: str,
    identifier: UUID,
    path: tuple[str | int, ...],
    replacement: object,
) -> None:
    row = connection.execute(
        f"SELECT document_json FROM {table} WHERE {primary_id} = ?",  # noqa: S608
        [str(identifier)],
    ).fetchone()
    assert row is not None
    document = json.loads(row[0])
    target = document
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    connection.execute(
        f"UPDATE {table} SET document_json = ? WHERE {primary_id} = ?",  # noqa: S608
        [json.dumps(document, separators=(",", ":"), sort_keys=True), str(identifier)],
    )


def test_backup_is_repeatable_and_restore_preserves_identity_counts_and_hashes(
    tmp_path: Path,
) -> None:
    workspace_service, service, source = _service(tmp_path)

    first = service.create(source, tmp_path / "backup-1")
    second = service.create(source, tmp_path / "backup-2")
    restored = service.restore(tmp_path / "backup-1", tmp_path / "restored")
    restored_manifest = workspace_service.inspect(tmp_path / "restored")

    assert first.backup_id == second.backup_id
    assert first.files == second.files
    assert first.counts == second.counts
    assert restored.workspace_id == first.source_workspace_id
    assert restored_manifest.workspace_id == first.source_workspace_id
    assert restored.status == "ready"
    assert restored_manifest.status == "ready"
    assert restored.raw_record_count == first.counts.raw_records
    assert restored.observation_count == first.counts.observations
    assert restored.metric_result_count == first.counts.metric_results
    assert restored.diagnostic_result_count == first.counts.diagnostic_results
    assert (tmp_path / "restored/state/scheduler.json").is_file()


def test_restore_rejects_corrupt_or_missing_files_without_publishing_destination(
    tmp_path: Path,
) -> None:
    _, service, source = _service(tmp_path)
    backup = tmp_path / "backup"
    service.create(source, backup)
    manifest = json.loads((backup / BACKUP_MANIFEST_NAME).read_text(encoding="utf-8"))
    target = backup / manifest["files"][0]["path"]
    target.write_bytes(target.read_bytes() + b"corrupt")

    with pytest.raises(WorkspaceBackupError, match="hash verification"):
        service.restore(backup, tmp_path / "not-published")
    assert not (tmp_path / "not-published").exists()

    clean = tmp_path / "clean-backup"
    service.create(source, clean)
    missing = clean / json.loads((clean / BACKUP_MANIFEST_NAME).read_text())["files"][0]["path"]
    missing.unlink()
    with pytest.raises(WorkspaceBackupError, match="inventory"):
        service.restore(clean, tmp_path / "also-not-published")
    assert not (tmp_path / "also-not-published").exists()


def test_backup_and_restore_reject_existing_or_nonempty_destinations(tmp_path: Path) -> None:
    _, service, source = _service(tmp_path)
    existing_backup = tmp_path / "existing-backup"
    existing_backup.mkdir()
    with pytest.raises(WorkspaceBackupError, match="already exists"):
        service.create(source, existing_backup)

    backup = tmp_path / "backup"
    service.create(source, backup)
    destination = tmp_path / "destination"
    destination.mkdir()
    preserved = destination / "preserved.txt"
    preserved.write_text("keep", encoding="utf-8")
    with pytest.raises(WorkspaceBackupError, match="new or empty"):
        service.restore(backup, destination)
    assert preserved.read_text(encoding="utf-8") == "keep"


def test_backup_rejects_internal_symlink_without_copying_external_file(tmp_path: Path) -> None:
    _, service, source = _service(tmp_path)
    external = tmp_path / "external-secret.txt"
    external.write_text("must stay outside the backup", encoding="utf-8")
    (source / "state/external-link.txt").symlink_to(external)
    destination = tmp_path / "backup"

    with pytest.raises(WorkspaceBackupError, match="symbolic links"):
        service.create(source, destination)

    assert not destination.exists()


def test_backup_rejects_metric_with_missing_derived_metric(tmp_path: Path) -> None:
    workspace_service, service, source = _service(tmp_path)
    record_id = uuid4()
    observation_id = uuid4()
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)
    source_reference = SourceReference(
        source_id="test:workspace",
        record_key="backup-derived-lineage",
        retrieved_at=timestamp,
    )
    writer = workspace_service.open_storage(
        workspace_service.resolve(source),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        writer.raw_records.save(
            RawRecord(
                record_id=record_id,
                asset_id="equity:us:aapl",
                source=source_reference,
                event_time=timestamp,
                available_at=timestamp,
                received_at=timestamp,
                payload={"close": "210"},
                schema_version="backup-test-v1",
            )
        )
        writer.observations.save(
            NormalizedObservation(
                observation_id=observation_id,
                raw_record_id=record_id,
                asset_id="equity:us:aapl",
                field_name="close",
                value=Decimal("210"),
                unit="USD",
                frequency=DataFrequency.DAY_1,
                observed_at=timestamp,
                available_at=timestamp,
                normalized_at=timestamp,
                source=source_reference,
                quality=DataQuality.VALID,
                transformation_version="backup-test-v1",
            )
        )
        writer.metric_results.save(
            MetricResult(
                asset_id="equity:us:aapl",
                metric_key="market.technical.ema",
                value=Decimal("210"),
                unit="USD",
                as_of=timestamp,
                available_at=timestamp,
                computed_at=timestamp,
                parameters={"source_id": "test:workspace", "known_at": timestamp.isoformat()},
                input_observation_ids=[observation_id],
                input_metric_result_ids=[uuid4()],
                algorithm_version="market-ema-v1-decimal34",
                quality=DataQuality.VALID,
            )
        )
    finally:
        writer.close()

    with pytest.raises(WorkspaceBackupError, match="derived metrics"):
        service.create(source, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_restore_verification_rejects_symlinked_inventory_file(tmp_path: Path) -> None:
    _, service, source = _service(tmp_path)
    backup = tmp_path / "backup"
    manifest = service.create(source, backup)
    target = backup / manifest.files[0].path
    external = tmp_path / "external-replacement.bin"
    external.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(external)
    destination = tmp_path / "restored"

    with pytest.raises(WorkspaceBackupError, match="symbolic links"):
        service.restore(backup, destination)

    assert not destination.exists()


def test_backup_restore_preserves_preference_revision_and_supports_absent_state(
    tmp_path: Path,
) -> None:
    _, service, source = _service(tmp_path)
    absent_manifest = service.create(source, tmp_path / "without-preferences")
    assert all(
        item.path != "state/asset_preferences_state_v1.json" for item in absent_manifest.files
    )

    entry = AssetPreferenceEntry(
        asset_id="equity:us:aapl",
        watchlist=True,
        favorite=True,
        scheduled_refresh=True,
    )
    entries = (entry,)
    fingerprint = asset_preferences_fingerprint(entries)
    seed = EffectiveAssetPreferences(
        source="cli_seed",
        revision_id=None,
        created_at=None,
        fingerprint=fingerprint,
        entries=entries,
    )
    preference_path = source / "state/asset_preferences_state_v1.json"
    store = AssetPreferencesStore(
        preference_path,
        clock=lambda: datetime(2026, 8, 1, hour=2, tzinfo=UTC),
        max_revisions=6,
        archive_at_revisions=4,
        retained_revisions=2,
    )
    persisted, changed = store.apply(
        AssetPreferencesUpdate(
            expected_revision_id=None,
            expected_fingerprint=fingerprint,
            entries=entries,
        ),
        seed,
    )
    revision_ids = [persisted.revision_id]
    for favorite in (False, True, False):
        revised_entry = entry.model_copy(update={"favorite": favorite})
        persisted, appended = store.apply(
            AssetPreferencesUpdate(
                expected_revision_id=persisted.revision_id,
                expected_fingerprint=persisted.fingerprint,
                entries=(revised_entry,),
            ),
            seed,
        )
        assert appended is True
        revision_ids.append(persisted.revision_id)
    manifest = service.create(source, tmp_path / "with-preferences")
    service.restore(tmp_path / "with-preferences", tmp_path / "restored-preferences")
    restored_store = AssetPreferencesStore(
        tmp_path / "restored-preferences/state/asset_preferences_state_v1.json",
        max_revisions=6,
        archive_at_revisions=4,
        retained_revisions=2,
    )

    restored = restored_store.load()
    assert changed is True
    assert any(item.path == "state/asset_preferences_state_v1.json" for item in manifest.files)
    assert any(
        item.path.startswith("state/asset_preferences_state_v1_archives/")
        for item in manifest.files
    )
    assert restored is not None
    assert restored.current.revision_id == persisted.revision_id
    assert restored.current.fingerprint == persisted.fingerprint
    assert tuple(item.revision_id for item in restored_store.load_history()) == tuple(revision_ids)
    assert preference_path.read_bytes() == restored_store.path.read_bytes()


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("valid", None),
        ("raw_checksum", "workspace traceability could not be verified"),
        ("observation_raw", "workspace contains an observation without its raw record"),
        ("metric_observation", "workspace contains a metric without its observations"),
        ("metric_metric", "workspace contains a metric without its derived metrics"),
        ("diagnostic_metric", "workspace contains a diagnostic without its metrics"),
        ("invalid_document", "workspace traceability could not be verified"),
    ],
)
def test_bounded_traceability_matches_pre_d2_oracle(
    tmp_path: Path,
    mutation: str,
    expected_error: str | None,
) -> None:
    workspace_service, backup_service, source = _service(tmp_path)
    raw_ids, observation_ids, metric_ids, diagnostic_ids = _seed_traceable_layers(
        workspace_service,
        source,
        count=3,
    )
    if mutation == "raw_checksum":
        writer = workspace_service.open_storage(
            workspace_service.resolve(source),
            WorkspaceAccessMode.READ_WRITE,
        )
        try:
            row = writer.store.connection.execute(
                "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
                [str(raw_ids[0])],
            ).fetchone()
            assert row is not None
        finally:
            writer.close()
        raw_path = source / "storage/data/raw" / row[0]
        raw_path.write_bytes(raw_path.read_bytes() + b"corrupt")
    elif mutation in {"observation_raw", "metric_observation"}:
        writer = workspace_service.open_storage(
            workspace_service.resolve(source),
            WorkspaceAccessMode.READ_WRITE,
        )
        try:
            if mutation == "observation_raw":
                writer.store.connection.execute(
                    "DELETE FROM raw_record_index WHERE record_id = ?",
                    [str(raw_ids[0])],
                )
            else:
                writer.store.connection.execute(
                    "DELETE FROM normalized_observations WHERE observation_id = ?",
                    [str(observation_ids[0])],
                )
        finally:
            writer.close()
    elif mutation in {"metric_metric", "diagnostic_metric", "invalid_document"}:
        writer = workspace_service.open_storage(
            workspace_service.resolve(source),
            WorkspaceAccessMode.READ_WRITE,
        )
        try:
            missing = UUID(int=900_000)
            if mutation == "metric_metric":
                _replace_document_reference(
                    writer.store.connection,
                    table="metric_results",
                    primary_id="result_id",
                    identifier=metric_ids[0],
                    path=("input_metric_result_ids",),
                    replacement=[str(missing)],
                )
            elif mutation == "diagnostic_metric":
                _replace_document_reference(
                    writer.store.connection,
                    table="diagnostic_results",
                    primary_id="diagnostic_id",
                    identifier=diagnostic_ids[0],
                    path=("components", 0, "metric_result_ids"),
                    replacement=[str(missing)],
                )
                _replace_document_reference(
                    writer.store.connection,
                    table="diagnostic_results",
                    primary_id="diagnostic_id",
                    identifier=diagnostic_ids[0],
                    path=("evidence", 0, "metric_result_id"),
                    replacement=str(missing),
                )
            else:
                writer.store.connection.execute(
                    "UPDATE normalized_observations SET document_json = ? WHERE observation_id = ?",
                    ["{}", str(observation_ids[0])],
                )
        finally:
            writer.close()

    assert _legacy_traceability_error(workspace_service, source) == expected_error
    assert _bounded_traceability_error(workspace_service, source) == expected_error
    if expected_error is not None:
        destination = tmp_path / "not-published"
        with pytest.raises(WorkspaceBackupError, match=expected_error):
            backup_service.create(source, destination)
        assert not destination.exists()


def test_traceable_backup_restore_is_deterministic_and_preserves_manifest_contract(
    tmp_path: Path,
) -> None:
    workspace_service, service, source = _service(tmp_path)
    _seed_traceable_layers(workspace_service, source, count=5)

    first = service.create(source, tmp_path / "backup-1")
    second = service.create(source, tmp_path / "backup-2")
    restored = service.restore(tmp_path / "backup-1", tmp_path / "restored")

    assert first.backup_id == second.backup_id
    assert first.source_workspace_id == second.source_workspace_id == restored.workspace_id
    assert first.files == second.files
    assert tuple(item.path for item in first.files) == tuple(
        sorted(item.path for item in first.files)
    )
    assert first.counts == second.counts
    assert first.counts.model_dump() == {
        "raw_records": 5,
        "observations": 5,
        "metric_results": 5,
        "diagnostic_results": 5,
    }
    assert first.traceability_verified is True


@pytest.mark.parametrize(
    "count",
    [
        backup_module._TRACEABILITY_BATCH_SIZE + 7,
        backup_module._TRACEABILITY_BATCH_SIZE * 3 + 7,
    ],
)
def test_backup_restore_never_materializes_full_history_and_keeps_structural_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    workspace_service, service, source = _service(tmp_path)
    _seed_traceable_layers(
        workspace_service,
        source,
        count=count,
        cross_page_metric_references=True,
    )

    def fail_list(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("traceability verification must not call list()")

    for repository in (
        JsonRawRecordRepository,
        DuckDBObservationRepository,
        DuckDBMetricResultRepository,
        DuckDBDiagnosticResultRepository,
    ):
        monkeypatch.setattr(repository, "list", fail_list)

    primary_page_sizes: list[int] = []
    document_page_sizes: list[int] = []
    get_many_sizes: list[int] = []
    original_primary_page = backup_module._load_raw_record_ids_page
    original_document_page = backup_module._load_document_rowid_page
    original_get_many = JsonRawRecordRepository.get_many

    def tracked_primary_page(
        connection: DuckDBPyConnection,
        *,
        start_rowid: int,
        end_rowid: int,
    ) -> tuple[str, ...]:
        page = original_primary_page(
            connection,
            start_rowid=start_rowid,
            end_rowid=end_rowid,
        )
        if page:
            primary_page_sizes.append(len(page))
        return page

    def tracked_document_page(
        connection: DuckDBPyConnection,
        *,
        table: str,
        primary_id: str,
        start_rowid: int,
        end_rowid: int,
        model_type: type[BaseModel],
    ) -> tuple[tuple[str, BaseModel], ...]:
        page = original_document_page(
            connection,
            table=table,
            primary_id=primary_id,
            start_rowid=start_rowid,
            end_rowid=end_rowid,
            model_type=model_type,
        )
        if page:
            document_page_sizes.append(len(page))
        return page

    def tracked_get_many(
        repository: JsonRawRecordRepository,
        record_ids: Collection[UUID],
    ) -> dict[UUID, RawRecord]:
        get_many_sizes.append(len(record_ids))
        return original_get_many(repository, record_ids)

    active_models = 0
    maximum_active_models = 0

    def release_model() -> None:
        nonlocal active_models
        active_models -= 1

    def tracked_model_from_json(
        model_type: type[BaseModel],
        data: bytes | str,
    ) -> BaseModel:
        nonlocal active_models, maximum_active_models
        model = model_from_json(model_type, data)
        active_models += 1
        maximum_active_models = max(maximum_active_models, active_models)
        weakref.finalize(model, release_model)
        return model

    monkeypatch.setattr(backup_module, "_load_raw_record_ids_page", tracked_primary_page)
    monkeypatch.setattr(backup_module, "_load_document_rowid_page", tracked_document_page)
    monkeypatch.setattr(JsonRawRecordRepository, "get_many", tracked_get_many)
    monkeypatch.setattr(backup_module, "model_from_json", tracked_model_from_json)
    monkeypatch.setattr(raw_records_module, "model_from_json", tracked_model_from_json)

    manifest = service.create(source, tmp_path / "backup")
    restored = service.restore(tmp_path / "backup", tmp_path / "restored")
    gc.collect()

    batch_size = backup_module._TRACEABILITY_BATCH_SIZE
    assert manifest.counts.model_dump() == {
        "raw_records": count,
        "observations": count,
        "metric_results": count,
        "diagnostic_results": count,
    }
    assert restored.raw_record_count == count
    assert restored.observation_count == count
    assert restored.metric_result_count == count
    assert restored.diagnostic_result_count == count
    assert max(primary_page_sizes) <= batch_size
    assert max(document_page_sizes) <= batch_size
    assert max(get_many_sizes) <= batch_size
    assert count % batch_size in primary_page_sizes
    assert count % batch_size in document_page_sizes
    assert maximum_active_models <= batch_size + 1
    assert active_models == 0


def test_reference_missing_immediately_after_keyset_boundary_fails_closed(
    tmp_path: Path,
) -> None:
    workspace_service, service, source = _service(tmp_path)
    count = backup_module._TRACEABILITY_BATCH_SIZE + 7
    raw_ids, _, _, _ = _seed_traceable_layers(
        workspace_service,
        source,
        count=count,
    )
    writer = workspace_service.open_storage(
        workspace_service.resolve(source),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        writer.store.connection.execute(
            "DELETE FROM raw_record_index WHERE record_id = ?",
            [str(raw_ids[backup_module._TRACEABILITY_BATCH_SIZE])],
        )
    finally:
        writer.close()

    destination = tmp_path / "not-published"
    with pytest.raises(WorkspaceBackupError, match="observation without its raw record"):
        service.create(source, destination)
    assert not destination.exists()


def test_changed_count_and_incomplete_keyset_scan_fail_before_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_service, service, source = _service(tmp_path)
    _seed_traceable_layers(
        workspace_service,
        source,
        count=backup_module._TRACEABILITY_BATCH_SIZE + 1,
    )
    original_count = DuckDBObservationRepository.count

    def changed_count(repository: DuckDBObservationRepository, **kwargs: object) -> int:
        return original_count(repository, **kwargs) + 1

    monkeypatch.setattr(DuckDBObservationRepository, "count", changed_count)
    changed_destination = tmp_path / "changed-count"
    with pytest.raises(WorkspaceBackupError, match="traceability could not be verified"):
        service.create(source, changed_destination)
    assert not changed_destination.exists()

    monkeypatch.setattr(DuckDBObservationRepository, "count", original_count)
    original_page = backup_module._load_document_rowid_page

    def incomplete_page(
        connection: DuckDBPyConnection,
        *,
        table: str,
        primary_id: str,
        start_rowid: int,
        end_rowid: int,
        model_type: type[BaseModel],
    ) -> tuple[tuple[str, BaseModel], ...]:
        if table == "normalized_observations" and start_rowid > 0:
            return ()
        return original_page(
            connection,
            table=table,
            primary_id=primary_id,
            start_rowid=start_rowid,
            end_rowid=end_rowid,
            model_type=model_type,
        )

    monkeypatch.setattr(backup_module, "_load_document_rowid_page", incomplete_page)
    incomplete_destination = tmp_path / "incomplete-scan"
    with pytest.raises(WorkspaceBackupError, match="traceability could not be verified"):
        service.create(source, incomplete_destination)
    assert not incomplete_destination.exists()


def test_metric_observation_lineage_rejects_missing_reference_at_tail_of_oversized_array(
    tmp_path: Path,
) -> None:
    workspace_service, service, source = _service(tmp_path)
    window_size = backup_module._TRACEABILITY_BATCH_SIZE
    ref_count = window_size + 10
    raw_ids = tuple(UUID(int=index + 1) for index in range(ref_count))
    obs_ids = tuple(UUID(int=100_000 + index) for index in range(ref_count))
    metric_id = UUID(int=200_001)
    timestamp = datetime(2026, 8, 1, tzinfo=UTC)

    writer = workspace_service.open_storage(
        workspace_service.resolve(source),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        for index in range(ref_count):
            ref = SourceReference(
                source_id="test:workspace",
                record_key=f"tail-{index}",
                retrieved_at=timestamp,
            )
            writer.raw_records.save(
                RawRecord(
                    record_id=raw_ids[index],
                    asset_id="equity:us:aapl",
                    source=ref,
                    event_time=timestamp,
                    available_at=timestamp,
                    received_at=timestamp,
                    payload={"value": str(index)},
                    schema_version="backup-tail-test-v1",
                )
            )
            if index < ref_count - 1:
                writer.observations.save(
                    NormalizedObservation(
                        observation_id=obs_ids[index],
                        raw_record_id=raw_ids[index],
                        asset_id="equity:us:aapl",
                        field_name="close",
                        value=Decimal(index),
                        unit="USD",
                        frequency=DataFrequency.DAY_1,
                        observed_at=timestamp,
                        available_at=timestamp,
                        normalized_at=timestamp,
                        source=ref,
                        quality=DataQuality.VALID,
                        transformation_version="backup-tail-test-v1",
                    )
                )

        writer.metric_results.save(
            MetricResult(
                result_id=metric_id,
                asset_id="equity:us:aapl",
                metric_key="market.technical.ema",
                value=Decimal("100"),
                unit="USD",
                as_of=timestamp,
                available_at=timestamp,
                computed_at=timestamp,
                parameters={"known_at": timestamp.isoformat()},
                input_observation_ids=list(obs_ids),
                input_metric_result_ids=[],
                algorithm_version="market-ema-v1-decimal34",
                quality=DataQuality.VALID,
            )
        )
    finally:
        writer.close()

    destination = tmp_path / "not-published"
    with pytest.raises(
        WorkspaceBackupError,
        match="workspace contains a metric without its observations",
    ):
        service.create(source, destination)
    assert not destination.exists()


def test_lineage_verification_query_count_is_not_proportional_to_row_count(
    tmp_path: Path,
) -> None:
    workspace_service, _, source = _service(tmp_path)
    count = 100
    _seed_traceable_layers(workspace_service, source, count=count)

    paths = workspace_service.resolve(source)
    storage = workspace_service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
    try:

        class ConnectionProxy:
            def __init__(self, target: DuckDBPyConnection) -> None:
                self._target = target
                self.query_count = 0

            def execute(self, *args: object, **kwargs: object) -> DuckDBPyConnection:
                self.query_count += 1
                return self._target.execute(*args, **kwargs)

            def __getattr__(self, name: str) -> object:
                return getattr(self._target, name)

        proxy = ConnectionProxy(storage.store.connection)
        backup_module._verify_observation_raw_lineage(proxy)  # type: ignore[arg-type]
        assert proxy.query_count == 1

        backup_module._verify_metric_observation_lineage(proxy)  # type: ignore[arg-type]
        assert proxy.query_count == 2

        backup_module._verify_metric_metric_lineage(proxy)  # type: ignore[arg-type]
        assert proxy.query_count == 3

        backup_module._verify_diagnostic_metric_lineage(proxy)  # type: ignore[arg-type]
        assert proxy.query_count == 4
    finally:
        storage.close()


def test_traceability_scan_handles_rowid_gaps(tmp_path: Path) -> None:
    workspace_service, service, source = _service(tmp_path)
    count = 20
    raw_ids, obs_ids, metric_ids, diag_ids = _seed_traceable_layers(
        workspace_service,
        source,
        count=count,
    )
    writer = workspace_service.open_storage(
        workspace_service.resolve(source),
        WorkspaceAccessMode.READ_WRITE,
    )
    try:
        for table, pid, val in (
            ("diagnostic_results", "diagnostic_id", str(diag_ids[5])),
            ("metric_results", "result_id", str(metric_ids[5])),
            ("normalized_observations", "observation_id", str(obs_ids[5])),
            ("raw_record_index", "record_id", str(raw_ids[5])),
        ):
            writer.store.connection.execute(
                f"DELETE FROM {table} WHERE {pid} = ?",
                [val],  # noqa: S608
            )
    finally:
        writer.close()

    destination = tmp_path / "gap-backup"
    manifest = service.create(source, destination)
    restored = service.restore(destination, tmp_path / "gap-restored")
    assert manifest.counts.raw_records == count - 1
    assert manifest.counts.observations == count - 1
    assert manifest.counts.metric_results == count - 1
    assert manifest.counts.diagnostic_results == count - 1
    assert restored.raw_record_count == count - 1


def test_traceability_scan_uses_bounded_rowid_ranges_without_uuid_top_n(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_service, _, source = _service(tmp_path)
    _seed_traceable_layers(workspace_service, source, count=10)

    paths = workspace_service.resolve(source)
    storage = workspace_service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
    try:
        queries: list[str] = []

        class QueryTrackingProxy:
            def __init__(self, target: DuckDBPyConnection) -> None:
                self._target = target

            def execute(self, query: str, *args: object, **kwargs: object) -> DuckDBPyConnection:
                queries.append(query)
                return self._target.execute(query, *args, **kwargs)

            def __getattr__(self, name: str) -> object:
                return getattr(self._target, name)

        proxy = QueryTrackingProxy(storage.store.connection)
        monkeypatch.setattr(storage.store, "_connection", proxy)
        backup_module._scan_raw_records(storage)
        backup_module._scan_documents(
            proxy,  # type: ignore[arg-type]
            table="normalized_observations",
            primary_id="observation_id",
            model_type=NormalizedObservation,
        )
        backup_module._scan_documents(
            proxy,  # type: ignore[arg-type]
            table="metric_results",
            primary_id="result_id",
            model_type=MetricResult,
        )
        backup_module._scan_documents(
            proxy,  # type: ignore[arg-type]
            table="diagnostic_results",
            primary_id="diagnostic_id",
            model_type=DiagnosticResult,
        )

        assert len(queries) > 0
        for query in queries:
            normalized = query.upper()
            assert "ORDER BY" not in normalized
            assert "OFFSET" not in normalized
            assert "LIMIT" not in normalized
            assert "TOP_N" not in normalized
    finally:
        storage.close()


test_document_and_raw_scans_contain_no_top_n_or_keyset_queries = (
    test_traceability_scan_uses_bounded_rowid_ranges_without_uuid_top_n
)


def test_lineage_queries_are_constant_and_use_direct_unnest() -> None:
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE raw_record_index (record_id VARCHAR PRIMARY KEY)")
    con.execute(
        "CREATE TABLE normalized_observations ("
        "observation_id VARCHAR PRIMARY KEY, raw_record_id VARCHAR NOT NULL)"
    )
    con.execute(
        "CREATE TABLE metric_results ("
        "result_id VARCHAR PRIMARY KEY, document_json VARCHAR NOT NULL)"
    )
    con.execute(
        "CREATE TABLE diagnostic_results ("
        "diagnostic_id VARCHAR PRIMARY KEY, document_json VARCHAR NOT NULL)"
    )

    for query in (
        backup_module._OBSERVATION_RAW_QUERY,
        backup_module._METRIC_OBSERVATION_QUERY,
        backup_module._METRIC_METRIC_QUERY,
        backup_module._DIAGNOSTIC_METRIC_QUERY,
    ):
        plan = con.execute(f"EXPLAIN {query}").fetchall()[0][1]
        assert "DELIM_JOIN" not in plan
        assert "LEFT_DELIM_JOIN" not in plan
        assert "LATERAL" not in plan
        assert "GROUP_BY" not in plan
        assert "ANTI" in plan
        if query != backup_module._OBSERVATION_RAW_QUERY:
            assert "UNNEST" in plan


test_lineage_queries_use_direct_unnest_without_delim_join = (
    test_lineage_queries_are_constant_and_use_direct_unnest
)
