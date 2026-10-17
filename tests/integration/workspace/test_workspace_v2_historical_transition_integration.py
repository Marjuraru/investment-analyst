"""Nonempty historical append, durable crash recovery and parent preservation."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
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
from investment_analyst.storage.compact_analytical_v2 import (
    CompactAnalyticalError,
    CompactAnalyticalStore,
)
from investment_analyst.storage.raw_v2_import import RawV2Importer, RawV2ImportError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import (
    create_v1_migration_source_snapshot,
    migrate_workspace_v1_to_v2,
    quiescent_source_cut,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    HistoricalTransition,
    HistoricalTransitionReceipt,
)

_MOMENT = datetime(2026, 10, 14, tzinfo=UTC)


@pytest.mark.parametrize("corruption", [None, "blob", "metadata", "projection", "identity"])
def test_completed_f_raw_resume_rehashes_bytes_and_projections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str | None
) -> None:
    service, source, destination, _ = _prepared(tmp_path)
    _save_raw(service, source, "F", moment=_MOMENT + timedelta(hours=5))
    operation_id = uuid4()

    def stop(event: str) -> None:
        if event == "after_metric_page_write":
            raise RuntimeError("controlled interruption")

    arguments = dict(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
    )
    with (
        quiescent_source_cut(
            service,
            source,
            operation_id=operation_id,
            clock=lambda: _MOMENT + timedelta(hours=6),
        ) as cut,
        pytest.raises(RuntimeError, match="controlled interruption"),
    ):
        migrate_workspace_v1_to_v2(**arguments, frozen_source=cut, analytical_failure_injector=stop)
    terminal = destination / f"storage/v2/raw-v2-terminal-verification.F.{operation_id}.json"
    original = terminal.read_bytes()
    document = json.loads(original)
    assert document["operation_id"] == str(operation_id)
    assert document["destination_workspace_id"] == str(service.manifest(destination).workspace_id)
    if corruption == "identity":
        document["destination_workspace_id"] = str(uuid4())
        terminal.write_text(json.dumps(document), encoding="utf-8")
    elif corruption is not None:
        with service.open_storage(
            service.resolve(destination), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            connection = storage.store.connection
            if corruption == "blob":
                relative = connection.execute(
                    "SELECT relative_path FROM raw_v2_index LIMIT 1"
                ).fetchone()[0]
                blob = storage.store.raw_staging._resolve_blob(relative)
                payload = blob.read_bytes()
                blob.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
            elif corruption == "metadata":
                connection.execute("UPDATE raw_v2_index SET source_id='foreign'")
            else:
                connection.execute(
                    "INSERT INTO workspace_raw_json_projections_v2 "
                    "SELECT record_id, 'unknown', 'value' FROM raw_v2_index LIMIT 1"
                )

    def repeated_hydration(*args, **kwargs):
        raise AssertionError("terminal F must reuse the paired source-model verification")

    monkeypatch.setattr(RawV2Importer, "_verify_epoch_page", repeated_hydration)
    with quiescent_source_cut(
        service,
        source,
        operation_id=operation_id,
        clock=lambda: _MOMENT + timedelta(hours=6),
    ) as cut:
        if corruption is None:
            assert migrate_workspace_v1_to_v2(**arguments, frozen_source=cut).raw.complete
            assert terminal.read_bytes() == original
        else:
            from investment_analyst.storage.errors import StorageError

            with pytest.raises((RawV2ImportError, StorageError)):
                migrate_workspace_v1_to_v2(**arguments, frozen_source=cut)


def _save_raw(
    service: WorkspaceService, root: Path, key: str, *, moment: datetime = _MOMENT
) -> None:
    record = RawRecord(
        record_id=uuid5(NAMESPACE_URL, key),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="fixture:cutover", record_key=key, retrieved_at=moment),
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"value": key},
        schema_version="fixture-cutover-v1",
    )
    with service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(record)
        storage.observations.save(
            NormalizedObservation(
                observation_id=uuid5(NAMESPACE_URL, f"cutover-observation:{key}"),
                raw_record_id=record.record_id,
                asset_id=record.asset_id,
                field_name="close",
                value=Decimal("210.5000000001"),
                unit="USD",
                frequency=DataFrequency.DAY_1,
                observed_at=moment,
                available_at=moment,
                normalized_at=moment,
                source=record.source,
                quality=DataQuality.VALID,
                transformation_version="fixture-v1",
            )
        )
        metric = MetricResult(
            result_id=uuid5(NAMESPACE_URL, f"cutover-metric:{key}"),
            asset_id=record.asset_id,
            metric_key="fixture:cutover",
            value=Decimal("210.5000000001"),
            unit="USD",
            as_of=moment,
            available_at=moment,
            computed_at=moment,
            input_observation_ids=[uuid5(NAMESPACE_URL, f"cutover-observation:{key}")],
            algorithm_version="fixture-v1",
            quality=DataQuality.VALID,
        )
        storage.metric_results.save(metric)
        storage.diagnostics.save(
            DiagnosticResult(
                diagnostic_id=uuid5(NAMESPACE_URL, f"cutover-diagnostic:{key}"),
                asset_id=record.asset_id,
                mode=DiagnosticMode.MARKET,
                verdict=DiagnosticVerdict.INSUFFICIENT_DATA,
                final_score=Decimal("0"),
                confidence=Decimal("0"),
                as_of=moment,
                available_at=moment,
                computed_at=moment,
                evidence=[
                    DiagnosticEvidence(
                        metric_result_id=metric.result_id,
                        direction=EvidenceDirection.NEUTRAL,
                        contribution=Decimal("0"),
                        reason="Evidencia sintética incompleta.",
                    )
                ],
                algorithm_version="fixture-v1",
                summary="Prueba de continuidad histórica.",
                quality=DataQuality.PARTIAL,
            )
        )


def _prepared(tmp_path: Path):
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    _save_raw(service, source, "A", moment=_MOMENT)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT + timedelta(hours=1),
    )
    _save_raw(service, source, "B", moment=_MOMENT + timedelta(hours=2))
    snapshot = create_v1_migration_source_snapshot(
        service=service,
        source_root=source,
        snapshot_root=tmp_path / "snapshot",
        host_free_bytes=lambda: 30_000_000_000,
    )
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=snapshot.snapshot_root,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        reconcile_source=True,
        clock=lambda: _MOMENT + timedelta(hours=4),
    )
    protected = {
        path: path.read_bytes() for path in (destination / "state").iterdir() if path.is_file()
    }
    return service, source, destination, protected


@pytest.mark.parametrize(
    "failure",
    [
        "after_metric_page_write",
        "after_metric_page_checkpoint",
        "after_metrics_complete",
        "after_diagnostic_page_write",
        "before_historical_seal",
        "after_historical_seal_commit",
        "after_historical_receipt",
    ],
)
def test_historical_transition_reopens_after_durable_failures(tmp_path: Path, failure: str) -> None:
    service, source, destination, protected = _prepared(tmp_path)
    _save_raw(service, source, "F-late", moment=_MOMENT - timedelta(days=1))
    operation_id = uuid4()
    fired = False

    def interrupt(event: str) -> None:
        nonlocal fired
        if event == failure and not fired:
            fired = True
            raise RuntimeError("controlled interruption")

    with quiescent_source_cut(
        service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
    ) as cut:
        arguments = dict(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
            clock=lambda: _MOMENT + timedelta(hours=6),
        )
        with pytest.raises(RuntimeError, match="controlled interruption"):
            migrate_workspace_v1_to_v2(**arguments, analytical_failure_injector=interrupt)
    assert fired
    raw_checkpoint = destination / f"storage/v2/raw-v2-import-state.F.{operation_id}.json"
    observation_checkpoint = (
        destination / f"storage/v2/observation-v2-import-state.F.{operation_id}.json"
    )
    earlier = {p: p.read_bytes() for p in (raw_checkpoint, observation_checkpoint)}
    with quiescent_source_cut(
        service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
    ) as cut:
        summary = migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
            clock=lambda: _MOMENT + timedelta(hours=7),
        )
    assert summary.analytical.metric_count == summary.analytical.diagnostic_count == 3
    assert summary.analytical.max_page_hydrated == 1
    assert all(p.read_bytes() == data for p, data in protected.items())
    assert all(p.read_bytes() == data for p, data in earlier.items())
    companion = destination / f"state/historical-transition.{operation_id}.json"
    transition = HistoricalTransition.model_validate_json(companion.read_bytes())
    receipt_path = destination / f"state/historical-transition-receipt.{operation_id}.json"
    receipt = HistoricalTransitionReceipt.model_validate_json(receipt_path.read_bytes())
    assert transition.phase == "COMPLETE" and receipt.transition == transition
    assert receipt.metric_created_count == receipt.diagnostic_created_count == 1
    assert receipt.metric_reused_count == receipt.diagnostic_reused_count == 2
    assert transition.destination_workspace_id == service.manifest(destination).workspace_id
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        assert compact.verify_complete()["historical_metric_count"] == 3
        compact.verify_transition_archive(transition)
        # Ordinary append is still rejected after completing the explicit transition.
        existing = compact.get_metrics([uuid5(NAMESPACE_URL, "cutover-metric:A")])[
            uuid5(NAMESPACE_URL, "cutover-metric:A")
        ]
        extra = existing.model_copy(
            update={"result_id": uuid5(NAMESPACE_URL, "unauthorized-history")}
        )
        with pytest.raises(CompactAnalyticalError, match="sealed historical"):
            compact.save_metrics([extra], origin="HISTORICAL")
    before = {p: p.read_bytes() for p in (companion, receipt_path)}
    with quiescent_source_cut(
        service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
    ) as cut:
        again = migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
            clock=lambda: _MOMENT + timedelta(hours=8),
        )
    assert again.analytical.metric_created_count == again.analytical.diagnostic_created_count == 0
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize(
    "field",
    [
        "destination_workspace_id",
        "operation_id",
        "page_limit",
        "parent_seal_sha256",
        "source_fingerprint",
        "policy",
        "parent_membership",
    ],
)
def test_historical_resume_rejects_altered_authorization(tmp_path: Path, field: str) -> None:
    service, source, destination, _ = _prepared(tmp_path)
    _save_raw(service, source, "F-late", moment=_MOMENT - timedelta(days=1))
    operation_id = uuid4()

    def stop(event: str) -> None:
        if event == "after_metric_page_write":
            raise RuntimeError("controlled interruption")

    with (
        quiescent_source_cut(
            service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
        ) as cut,
        pytest.raises(RuntimeError, match="controlled interruption"),
    ):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
            analytical_failure_injector=stop,
        )
    path = destination / f"state/historical-transition.{operation_id}.json"
    value = json.loads(path.read_bytes())
    if field == "parent_membership":
        membership = Path(value["parent_membership_path"])
        membership.write_bytes(membership.read_bytes().replace(b'"metrics"', b'"unknown"', 1))
    else:
        value[field] = (
            str(uuid4())
            if field in {"destination_workspace_id", "operation_id"}
            else 2
            if field == "page_limit"
            else "wrong-policy"
            if field == "policy"
            else "a" * 64
        )
        path.write_text(json.dumps(value))
    with quiescent_source_cut(
        service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
    ) as cut:
        from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2InventoryError

        with pytest.raises((WorkspaceV2InventoryError, CompactAnalyticalError, ValueError)):
            migrate_workspace_v1_to_v2(
                service=service,
                source_root=source,
                destination_root=destination,
                page_limit=1,
                host_free_bytes=lambda: 30_000_000_000,
                frozen_source=cut,
            )
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        assert compact.count_metrics(origin="HISTORICAL") == 3
        assert compact.count_diagnostics(origin="HISTORICAL") == 2


def test_transition_rejects_live_provenance_collision(tmp_path: Path) -> None:
    service, source, destination, _ = _prepared(tmp_path)
    _save_raw(service, source, "F-live", moment=_MOMENT + timedelta(days=1))
    identifier = uuid5(NAMESPACE_URL, "cutover-metric:F-live")
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_ONLY) as original:
        metric = original.metric_results.get(identifier)
        raw = original.raw_records.get(uuid5(NAMESPACE_URL, "F-live"))
        observation = original.observations.get(uuid5(NAMESPACE_URL, "cutover-observation:F-live"))
    live = metric
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_WRITE
    ) as target:
        target.raw_records.save(raw)
        target.observations.save(observation)
        target.metric_results.save(live)
    with quiescent_source_cut(service, source, operation_id=uuid4()) as cut:
        from investment_analyst.storage.errors import RecordConflictError

        with pytest.raises((CompactAnalyticalError, RecordConflictError)):
            migrate_workspace_v1_to_v2(
                service=service,
                source_root=source,
                destination_root=destination,
                page_limit=1,
                host_free_bytes=lambda: 30_000_000_000,
                frozen_source=cut,
            )
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as target:
        assert target.metric_results.get(identifier) == live
        assert CompactAnalyticalStore(target.store.connection).count_metrics(origin="LIVE") == 1


class _FixtureTransport:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fetches: list[str] = []

    def copy_immutable_object(self, source: Path, key: str) -> tuple[object, ...]:
        import shutil

        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            assert target.read_bytes() == source.read_bytes()
        else:
            shutil.copyfile(source, target)
        return ()

    def fetch_sealed_object(self, key: str, destination: Path) -> tuple[object, ...]:
        import shutil

        shutil.copyfile(self.root / key, destination)
        self.fetches.append(key)
        return ()


def test_parent_delta_recovers_applying_and_terminal_historical_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from investment_analyst.storage.workspace_v2_inventory import historical_transition_status
    from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        WorkspaceV2PhysicalDeltaError,
        WorkspaceV2PhysicalDeltaIndexService,
        WorkspaceV2PhysicalDeltaService,
    )

    service, source, destination, protected = _prepared(tmp_path)
    backup = WorkspaceV2BackupService(service)
    base = tmp_path / "base"
    backup.create_packed(
        destination,
        base,
        volume_bytes=64_000,
        fragment_bytes=8_000,
        available_bytes=lambda: 30_000_000_000,
    )
    index_root = tmp_path / "index"
    WorkspaceV2PhysicalDeltaIndexService(backup).build(base, index_root)
    index = index_root / "physical-delta-index.jsonl"
    parent = tmp_path / "parent"
    backup.restore_packed(base, parent, available_bytes=lambda: 30_000_000_000)
    _save_raw(service, source, "F-restore", moment=_MOMENT - timedelta(days=1))
    epoch = uuid4()

    def interrupt(event: str) -> None:
        if event == "before_historical_seal":
            raise RuntimeError("before seal")

    with (
        quiescent_source_cut(
            service, source, operation_id=epoch, clock=lambda: _MOMENT + timedelta(hours=5)
        ) as cut,
        pytest.raises(RuntimeError, match="before seal"),
    ):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
            analytical_failure_injector=interrupt,
        )
    delta = WorkspaceV2PhysicalDeltaService(service)
    transport = _FixtureTransport(tmp_path / "remote")
    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="incomplete historical"):
        delta.plan(
            destination, index, tmp_path / "rejected", parent_manifest_sha256="a" * 64, epoch=epoch
        )
    for phase in ("applying", "terminal"):
        if phase == "terminal":
            with quiescent_source_cut(
                service, source, operation_id=epoch, clock=lambda: _MOMENT + timedelta(hours=5)
            ) as cut:
                migrate_workspace_v1_to_v2(
                    service=service,
                    source_root=source,
                    destination_root=destination,
                    page_limit=1,
                    host_free_bytes=lambda: 30_000_000_000,
                    frozen_source=cut,
                )
        delta_root = tmp_path / f"delta-{phase}"
        manifest = delta.plan(
            destination,
            index,
            delta_root,
            parent_manifest_sha256="a" * 64,
            epoch=epoch,
            allow_incomplete_historical=phase == "applying",
        )
        uploaded = delta.upload(
            delta_root,
            transport,
            checkpoint_path=tmp_path / f"upload-{phase}.json",
            receipt_path=tmp_path / f"upload-receipt-{phase}.json",
        )
        restored = tmp_path / f"recovered-{phase}"
        receipt = delta.restore(
            delta_root,
            parent_root=parent,
            parent_index=index,
            parent_manifest_sha256="a" * 64,
            destination=restored,
            transport=transport,
            checkpoint_path=tmp_path / f"restore-{phase}.json",
            receipt_path=tmp_path / f"restore-receipt-{phase}.json",
        )
        expected = "APPLYING" if phase == "applying" else "COMPLETE"
        assert manifest.historical_state == receipt.historical_state == expected
        assert historical_transition_status(restored) == expected
        with service.open_storage(
            service.resolve(restored), WorkspaceAccessMode.READ_ONLY
        ) as storage:
            compact = CompactAnalyticalStore(storage.store.connection)
            assert compact.count_metrics(origin="HISTORICAL") == 3
            assert compact.count_diagnostics(origin="HISTORICAL") == 3
            if phase == "applying":
                with pytest.raises(CompactAnalyticalError, match="inventory changed"):
                    compact.verify_historical_seal()
            else:
                assert compact.verify_complete()["historical_metric_count"] == 3
        assert all(p.read_bytes() == data for p, data in protected.items())
    assert any(key.startswith("physical-delta/objects/") for key in transport.fetches)

    import investment_analyst.workspace.workspace_v2_lifecycle as lifecycle_module
    from investment_analyst.workspace.workspace_v2_lifecycle import (
        WorkspaceV2LifecycleError,
        WorkspaceV2LifecycleService,
    )
    from investment_analyst.workspace.workspace_v2_lifecycle_models import LifecyclePhase

    retirement_id = uuid4()
    lifecycle = WorkspaceV2LifecycleService(service)
    options = dict(
        delta_root=delta_root,
        operation_id=retirement_id,
        checkpoint_path=tmp_path / "retire-slot.json",
        proof_path=tmp_path / "retire-slot-proof.json",
    )
    with pytest.raises(WorkspaceV2LifecycleError, match="root-bound restore"):
        lifecycle.retire_delta_recovery_slot(uploaded, **options)
    with pytest.raises(WorkspaceV2LifecycleError, match="disjoint"):
        lifecycle.retire_delta_recovery_slot(
            receipt.model_copy(update={"restored_root": destination}), **options
        )
    unique = restored / "state/unique.bin"
    unique.write_bytes(b"unique")
    with pytest.raises(WorkspaceV2LifecycleError, match="unique|coverage"):
        lifecycle.retire_delta_recovery_slot(receipt, **options)
    unique.unlink()
    with (
        service.open_storage(service.resolve(restored), WorkspaceAccessMode.READ_WRITE),
        pytest.raises(WorkspaceV2LifecycleError, match="writer remains active"),
    ):
        lifecycle.retire_delta_recovery_slot(receipt, **options)
    rename = lifecycle_module.os.replace

    def interrupt_rename(source: Path, target: Path) -> None:
        if source == restored:
            raise OSError("fixture interruption before rename")
        rename(source, target)

    monkeypatch.setattr(lifecycle_module.os, "replace", interrupt_rename)
    with pytest.raises(WorkspaceV2LifecycleError, match="rename did not complete"):
        lifecycle.retire_delta_recovery_slot(receipt, **options)
    assert restored.is_dir() and (tmp_path / "retire-slot-proof.json").is_file()
    monkeypatch.setattr(lifecycle_module.os, "replace", rename)
    retired = lifecycle.retire_delta_recovery_slot(
        receipt,
        delta_root=delta_root,
        operation_id=retirement_id,
        checkpoint_path=tmp_path / "retire-slot.json",
        proof_path=tmp_path / "retire-slot-proof.json",
    )
    assert retired.phase is LifecyclePhase.RETIRED
    assert retired.files_confirmed == manifest.file_count
    assert not restored.exists()
    assert destination.is_dir() and parent.is_dir()
    again = lifecycle.retire_delta_recovery_slot(
        receipt,
        delta_root=delta_root,
        operation_id=retirement_id,
        checkpoint_path=tmp_path / "retire-slot.json",
        proof_path=tmp_path / "retire-slot-proof.json",
    )
    assert again.source_root == retired.source_root
    assert again.files_confirmed == retired.files_confirmed


@pytest.mark.parametrize("sealed", [False, True])
def test_prepared_membership_resumes_without_replacing_sealed_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sealed: bool
) -> None:
    service, source, destination, protected = _prepared(tmp_path)
    _save_raw(service, source, "F-preparation", moment=_MOMENT - timedelta(days=1))
    operation_id = uuid4()
    original = CompactAnalyticalStore.write_parent_membership
    captured: dict[Path, bytes] = {}

    def interrupt(store: CompactAnalyticalStore, path: Path, *, page_limit: int) -> str:
        if sealed:
            original(store, path, page_limit=page_limit)
            captured[path] = path.read_bytes()
        else:
            # Only unconfirmed metadata of this bound operation may be rewritten.
            path.with_name(path.name + ".partial").write_bytes(b"unconfirmed tail")
        raise RuntimeError("membership preparation interruption")

    monkeypatch.setattr(CompactAnalyticalStore, "write_parent_membership", interrupt)
    with (
        quiescent_source_cut(
            service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
        ) as cut,
        pytest.raises(RuntimeError, match="membership preparation interruption"),
    ):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
        )
    companion = destination / f"state/historical-transition.{operation_id}.json"
    assert HistoricalTransition.model_validate_json(companion.read_bytes()).phase == "PREPARED"
    monkeypatch.setattr(CompactAnalyticalStore, "write_parent_membership", original)
    with quiescent_source_cut(
        service, source, operation_id=operation_id, clock=lambda: _MOMENT + timedelta(hours=5)
    ) as cut:
        result = migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
        )
    assert result.analytical.metric_count == result.analytical.diagnostic_count == 3
    assert all(p.read_bytes() == data for p, data in captured.items())
    assert all(p.read_bytes() == data for p, data in protected.items())


@pytest.mark.parametrize("corruption", ["missing_evidence", "future_input", "cycle"])
def test_transition_refuses_invalid_financial_relationships(
    tmp_path: Path, corruption: str
) -> None:
    service, source, destination, protected = _prepared(tmp_path)
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        metric = storage.metric_results.get(uuid5(NAMESPACE_URL, "cutover-metric:A"))
        if corruption == "missing_evidence":
            diagnostic = storage.diagnostics.get(uuid5(NAMESPACE_URL, "cutover-diagnostic:A"))
            storage.diagnostics.save(
                diagnostic.model_copy(
                    update={
                        "diagnostic_id": uuid4(),
                        "evidence": [
                            diagnostic.evidence[0].model_copy(update={"metric_result_id": uuid4()})
                        ],
                    }
                )
            )
        elif corruption == "future_input":
            storage.metric_results.save(
                metric.model_copy(
                    update={
                        "result_id": uuid4(),
                        "available_at": metric.available_at - timedelta(days=1),
                        "as_of": metric.as_of - timedelta(days=1),
                    }
                )
            )
        else:
            first, second = uuid4(), uuid4()
            storage.metric_results.save(
                metric.model_copy(update={"result_id": first, "input_metric_result_ids": [second]})
            )
            storage.metric_results.save(
                metric.model_copy(update={"result_id": second, "input_metric_result_ids": [first]})
            )
    operation_id = uuid4()
    with (
        quiescent_source_cut(service, source, operation_id=operation_id) as cut,
        pytest.raises(CompactAnalyticalError, match="missing|future|cycle"),
    ):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
        )
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        assert CompactAnalyticalStore(storage.store.connection).historical_seal().metric_count == 2
    assert (
        HistoricalTransition.model_validate_json(
            (destination / f"state/historical-transition.{operation_id}.json").read_bytes()
        ).phase
        == "APPLYING"
    )
    assert all(p.read_bytes() == data for p, data in protected.items())


def test_unrelated_live_metric_survives_complete_historical_transition(tmp_path: Path) -> None:
    service, source, destination, _ = _prepared(tmp_path)
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_WRITE
    ) as target:
        parent = target.metric_results.get(uuid5(NAMESPACE_URL, "cutover-metric:A"))
        live = parent.model_copy(
            update={
                "result_id": uuid5(NAMESPACE_URL, "unrelated-live"),
                "computed_at": parent.computed_at + timedelta(days=2),
            }
        )
        target.metric_results.save(live)
    _save_raw(service, source, "F-with-live", moment=_MOMENT - timedelta(days=1))
    with quiescent_source_cut(service, source, operation_id=uuid4()) as cut:
        result = migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
        )
    assert result.analytical.metric_count == 3
    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as target:
        compact = CompactAnalyticalStore(target.store.connection)
        assert compact.count_metrics(origin="HISTORICAL") == 3
        assert compact.count_metrics(origin="LIVE") == 1
        assert target.metric_results.get(live.result_id) == live
        assert compact.verify_complete()["metric_count"] == 4
