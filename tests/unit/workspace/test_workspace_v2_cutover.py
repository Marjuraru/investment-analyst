"""Focused tests for a finite source cut and direct catch-up into v2."""

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
from investment_analyst.workspace import workspace_v2_migration as migration_module
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_cutover import (
    WorkspaceV2CutoverError,
    WorkspaceV2CutoverPhase,
    WorkspaceV2CutoverService,
)
from investment_analyst.workspace.workspace_v2_migration import (
    create_v1_migration_source_snapshot,
    migrate_workspace_v1_to_v2,
    quiescent_source_cut,
)

_MOMENT = datetime(2026, 10, 14, tzinfo=UTC)


def _window_fixture(tmp_path: Path):
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference
    from investment_analyst.workspace.workspace_v2_cutover import WorkspaceV2CutWindowService
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutOwner,
        CutWindow,
        CutWindowBound,
        CutWindowGo,
    )

    proof = tmp_path / "proof.json"
    proof.write_bytes(b"{}")
    reference = reconciliation_file_reference(proof, "control")
    source_id, destination_id, boot = uuid4(), uuid4(), uuid4()
    bounds = tuple(
        CutWindowBound(
            step=step,
            upper_seconds=100,
            measurement=reference,
            measured_at=_MOMENT,
            universe="fixture:complete",
            workload="fixture:bounded",
            changes_since_measurement="none",
        )
        for step in (
            "freeze",
            "comparison_delta",
            "operational",
            "physical_plan",
            "upload",
            "restore_open",
            "activation_health",
            "cancel_close_restart",
        )
    )
    go = CutWindowGo(
        bounds=bounds,
        margin_seconds=100,
        baseline=reference,
        prerequisites=(reference,),
        guard=reference,
        capacity=reference,
        source_workspace_id=source_id,
        destination_workspace_id=destination_id,
        admitted_at=_MOMENT,
    )
    go_path = tmp_path / "go.json"
    go_path.write_text(go.model_dump_json())
    window = CutWindow(
        window_id=uuid4(),
        attempt_id=uuid4(),
        source_root=tmp_path / "source",
        source_workspace_id=source_id,
        destination_root=tmp_path / "destination",
        destination_workspace_id=destination_id,
        release_sha="a" * 40,
        owner=CutOwner(
            source_session=uuid4(),
            pid=123,
            start_ticks=456,
            boot_id=boot,
            cgroup=Path("/sys/fs/cgroup/fixture"),
        ),
        boot_id=boot,
        started_monotonic_ns=100,
        deadline_monotonic_ns=1800_000_000_100,
        stop_started_at=_MOMENT,
        recovery_reserve_seconds=100,
        go=reconciliation_file_reference(go_path, "control"),
        baseline=reference,
        code=reference,
        environment_sha256="e" * 64,
        resource_receipt=reference,
        step="admitted",
        updated_at=_MOMENT,
    )
    owner = WorkspaceV2CutWindowService(tmp_path / "window.json")
    return owner, window, go


@pytest.mark.parametrize("operation", ["catchup", "plan", "upload", "restore", "activate"])
def test_global_window_commands_share_clock_and_preserve_recovery_reserve(
    tmp_path: Path, operation: str
) -> None:
    owner, window, go = _window_fixture(tmp_path)
    phase = (
        "QUIESCENT"
        if operation == "catchup"
        else "ACTIVATING"
        if operation == "activate"
        else "APPLYING"
    )
    values = window.model_dump()
    values.update(phase=phase)
    if operation == "activate":
        values.update(activation_intent=window.baseline, recovery_receipt=window.baseline)
    active = type(window).model_validate(values)
    owner.path.write_text(active.model_dump_json())
    original = owner.path.read_bytes()
    admitted = owner.admit_operation(operation=operation, boot_id=window.boot_id, monotonic_ns=100)
    assert admitted.deadline_monotonic_ns == window.deadline_monotonic_ns
    with pytest.raises(WorkspaceV2CutoverError, match="recovery reserve"):
        owner.admit_operation(
            operation=operation,
            boot_id=window.boot_id,
            monotonic_ns=window.deadline_monotonic_ns - 110_000_000_000,
        )
    with pytest.raises(WorkspaceV2CutoverError, match="clock changed"):
        owner.admit_operation(operation=operation, boot_id=uuid4(), monotonic_ns=100)
    assert owner.path.read_bytes() == original


def test_global_window_activation_requires_durable_intention_and_verified_go(
    tmp_path: Path,
) -> None:
    from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2InventoryError

    owner, window, go = _window_fixture(tmp_path)
    values = window.model_dump()
    values.update(phase="RECOVERY_VERIFIED")
    owner.path.write_text(type(window).model_validate(values).model_dump_json())
    with pytest.raises(WorkspaceV2CutoverError, match="durable window phase"):
        owner.admit_operation(operation="activate", boot_id=window.boot_id, monotonic_ns=100)
    values.update(
        phase="ACTIVATING", activation_intent=window.baseline, recovery_receipt=window.baseline
    )
    owner.path.write_text(type(window).model_validate(values).model_dump_json())
    window.go.path.write_bytes(b"{}")
    with pytest.raises(WorkspaceV2InventoryError, match="artifact binding differs"):
        owner.admit_operation(operation="activate", boot_id=window.boot_id, monotonic_ns=100)


def test_global_window_resume_keeps_one_deadline_and_reserves_recovery(tmp_path: Path) -> None:
    owner, window, go = _window_fixture(tmp_path)
    reference = owner.prepare(window, go)
    assert owner.prepare(window, go) == reference
    now = 1_000_000_100
    reference = owner.advance(
        "STOPPING",
        expected=reference,
        boot_id=window.boot_id,
        monotonic_ns=now,
        at=_MOMENT + timedelta(seconds=1),
        step="normal stop",
    )
    assert owner.load().deadline_monotonic_ns == window.deadline_monotonic_ns
    assert owner.remaining_work_ns(boot_id=window.boot_id, monotonic_ns=now) == 1699_000_000_000
    with pytest.raises(WorkspaceV2CutoverError, match="reserve reached"):
        owner.advance(
            "QUIESCENT",
            expected=reference,
            boot_id=window.boot_id,
            monotonic_ns=1700_000_000_100,
            at=_MOMENT + timedelta(seconds=1700),
            step="too late",
        )
    reference = owner.advance(
        "RETURNING_V1",
        expected=reference,
        boot_id=window.boot_id,
        monotonic_ns=1700_000_000_100,
        at=_MOMENT + timedelta(seconds=1700),
        step="cancel",
    )
    owner.advance(
        "HEALTHY_V1",
        expected=reference,
        boot_id=window.boot_id,
        monotonic_ns=1750_000_000_100,
        at=_MOMENT + timedelta(seconds=1750),
        step="health proven",
    )
    assert owner.load().phase == "HEALTHY_V1"
    assert owner.load().deadline_monotonic_ns == window.deadline_monotonic_ns


@pytest.mark.parametrize("fault", ["boot", "clock", "cas", "reset", "unmeasured", "budget"])
def test_global_window_rejects_unknown_budget_clock_and_restart(
    tmp_path: Path,
    fault: str,
) -> None:
    from investment_analyst.workspace.workspace_v2_migration_models import CutWindowGo

    owner, window, go = _window_fixture(tmp_path)
    if fault in {"unmeasured", "budget"}:
        values = go.model_dump()
        if fault == "unmeasured":
            values["bounds"] = go.bounds[:-1]
        else:
            values["margin_seconds"] = 1800
        with pytest.raises(ValueError):
            CutWindowGo.model_validate(values)
        assert not owner.path.exists()
        return
    reference = owner.prepare(window, go)
    before = owner.path.read_bytes()
    with pytest.raises(WorkspaceV2CutoverError):
        if fault == "reset":
            owner.prepare(window.model_copy(update={"started_monotonic_ns": 200}), go)
        else:
            owner.advance(
                "STOPPING",
                expected=reference.model_copy(update={"sha256": "f" * 64})
                if fault == "cas"
                else reference,
                boot_id=uuid4() if fault == "boot" else window.boot_id,
                monotonic_ns=99 if fault == "clock" else 101,
                at=_MOMENT,
                step="fixture",
            )
    assert owner.path.read_bytes() == before


def test_global_window_never_returns_old_v1_from_activating(tmp_path: Path) -> None:
    owner, window, go = _window_fixture(tmp_path)
    reference = owner.prepare(window, go)
    for phase in ("STOPPING", "QUIESCENT", "APPLYING", "RECOVERY_VERIFIED", "ACTIVATING"):
        reference = owner.advance(
            phase,
            expected=reference,
            boot_id=window.boot_id,
            monotonic_ns=200,
            at=_MOMENT,
            step=phase,
            recovery_receipt=go.baseline,
            activation_intent=go.baseline,
        )
    with pytest.raises(WorkspaceV2CutoverError, match="forbidden"):
        owner.advance(
            "RETURNING_V1",
            expected=reference,
            boot_id=window.boot_id,
            monotonic_ns=201,
            at=_MOMENT,
            step="unsafe rollback",
        )
    assert owner.load().phase == "ACTIVATING"


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


def test_frozen_cut_catches_up_changed_source_without_a_snapshot_copy(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    service.initialize(source, format_version=1)
    _save_raw(service, source, "base")
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    _save_raw(service, source, "late-revision")
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT)
    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)

    summary = cutover.catch_up(
        frozen,
        destination,
        checkpoint_path=tmp_path / "cutover.json",
        host_free_bytes=lambda: 30_000_000_000,
        reserve_bytes=5_000_000_000,
        operational_inventory_path=tmp_path / "operational-inventory.jsonl",
        operational_seal_path=tmp_path / "operational-seal.json",
        operational_checkpoint_path=tmp_path / "operational-checkpoint.json",
        operational_receipt_path=tmp_path / "operational-receipt.json",
        page_limit=1,
    )

    assert summary.raw.imported_count == 2
    checkpoint = (tmp_path / "cutover.json").read_text(encoding="utf-8")
    assert WorkspaceV2CutoverPhase.CATCHUP_COMPLETE.value in checkpoint
    assert service.inspect(destination).raw_record_count == 2
    assert (tmp_path / "operational-receipt.json").is_file()


def test_cutover_rejects_active_writer_and_changed_frozen_source(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=1)
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT)
    with pytest.raises(WorkspaceV2CutoverError, match="writer must be closed"):
        cutover.freeze(source, operation_id=uuid4(), writer_closed=False)

    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)
    _save_raw(service, source, "changed-after-cut")
    with pytest.raises(WorkspaceV2CutoverError, match="frozen cut changed"):
        cutover.catch_up(
            frozen,
            tmp_path / "destination",
            checkpoint_path=tmp_path / "cutover.json",
            host_free_bytes=lambda: 30_000_000_000,
            reserve_bytes=5_000_000_000,
            operational_inventory_path=tmp_path / "operational-inventory.jsonl",
            operational_seal_path=tmp_path / "operational-seal.json",
            operational_checkpoint_path=tmp_path / "operational-checkpoint.json",
            operational_receipt_path=tmp_path / "operational-receipt.json",
        )


@pytest.mark.parametrize("changes", [0, 1, 3])
def test_f_epoch_preserves_a_b_and_imports_late_ids_before_historic_cursor(
    tmp_path: Path, changes: int
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    service.initialize(source, format_version=1)
    _save_raw(service, source, "A")
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    _save_raw(service, source, "B", moment=_MOMENT + timedelta(hours=1))
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
        clock=lambda: _MOMENT + timedelta(hours=2),
    )
    protected = {
        path: path.read_bytes()
        for path in (
            destination / "state/workspace-v2-source-transition.json",
            destination / "storage/v2/raw-v2-import-state.json",
            destination / "storage/v2/observation-v2-import-state.json",
            destination / "state/direct-historical-import-state.json",
        )
    }
    for number in range(changes):
        _save_raw(service, source, f"F-{number}", moment=_MOMENT - timedelta(days=number + 1))
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT + timedelta(hours=3))
    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)
    summary = cutover.catch_up(
        frozen,
        destination,
        checkpoint_path=tmp_path / "F.json",
        host_free_bytes=lambda: 30_000_000_000,
        reserve_bytes=5_000_000_000,
        operational_inventory_path=tmp_path / "F-inventory.jsonl",
        operational_seal_path=tmp_path / "F-seal.json",
        operational_checkpoint_path=tmp_path / "F-ops.json",
        operational_receipt_path=tmp_path / "F-ops-receipt.json",
        page_limit=1,
    )
    assert summary.raw.imported_count == 2 + changes
    assert summary.observations.imported_count == 2 + changes
    assert summary.analytical.metric_count == 2 + changes
    assert summary.analytical.diagnostic_count == 2 + changes
    assert summary.analytical.max_page_hydrated <= 1
    assert summary.raw.max_page_hydrated <= 1
    assert summary.observations.max_page_hydrated <= 1
    assert service.inspect(destination).raw_record_count == 2 + changes
    assert all(path.read_bytes() == content for path, content in protected.items())
    assert (destination / f"storage/v2/raw-v2-import-state.F.{frozen.operation_id}.json").is_file()


def test_live_quiescence_hashes_once_and_does_not_survive_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=1)
    _save_raw(service, source, "quiescent")
    original = migration_module.source_fingerprint
    calls = 0
    opened = 0
    original_open = service.open_storage

    def counted_open(*args: object, **kwargs: object) -> object:
        nonlocal opened
        storage = original_open(*args, **kwargs)
        original_close = storage.close
        closed = False
        opened += 1

        def counted_close() -> None:
            nonlocal opened, closed
            try:
                original_close()
            finally:
                if not closed:
                    closed = True
                    opened -= 1

        monkeypatch.setattr(storage, "close", counted_close)
        return storage

    monkeypatch.setattr(service, "open_storage", counted_open)

    def counted(workspace_service: WorkspaceService, root: Path) -> str:
        nonlocal calls
        calls += 1
        assert opened == 0, "physical hashing must precede the long-lived native reader"
        return original(workspace_service, root)

    monkeypatch.setattr(migration_module, "source_fingerprint", counted)
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT + timedelta(hours=1))
    operation = uuid4()
    with quiescent_source_cut(
        service, source, operation_id=operation, clock=lambda: _MOMENT
    ) as frozen:
        assert cutover.freeze(source, operation_id=operation, writer_closed=True) == frozen
        cutover.catch_up(
            frozen,
            tmp_path / "destination",
            checkpoint_path=tmp_path / "F.json",
            host_free_bytes=lambda: 30_000_000_000,
            reserve_bytes=5_000_000_000,
            operational_inventory_path=tmp_path / "ops-inventory.jsonl",
            operational_seal_path=tmp_path / "ops-seal.json",
            operational_checkpoint_path=tmp_path / "ops-checkpoint.json",
            operational_receipt_path=tmp_path / "ops-receipt.json",
        )
        assert calls == 1
    assert migration_module._live_quiescent_source(source) is None


def test_f_receipts_are_preserved_inside_the_recoverable_photo(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    _save_raw(service, source, "recoverable-controls")
    coordinator = WorkspaceV2CutoverService(service)
    frozen = coordinator.freeze(source, operation_id=uuid4(), writer_closed=True)
    checkpoint = tmp_path / "ops/cutover.json"
    inventory = tmp_path / "ops/inventory.jsonl"
    receipt = tmp_path / "ops/receipt.json"
    coordinator.catch_up(
        frozen,
        destination,
        checkpoint_path=checkpoint,
        host_free_bytes=lambda: 30_000_000_000,
        reserve_bytes=5_000_000_000,
        operational_inventory_path=inventory,
        operational_seal_path=tmp_path / "ops/seal.json",
        operational_checkpoint_path=tmp_path / "ops/operational.json",
        operational_receipt_path=receipt,
        page_limit=1,
    )
    arguments = dict(
        checkpoint_path=checkpoint,
        operational_inventory_path=inventory,
        operational_receipt_path=receipt,
    )
    paths = coordinator.preserve_recovery_controls(**arguments)
    assert all(path.parent == destination / "state" for path in paths)
    assert [path.read_bytes() for path in paths] == [
        checkpoint.read_bytes(),
        receipt.read_bytes(),
        inventory.read_bytes(),
    ]
    before = {path: path.read_bytes() for path in paths}
    assert coordinator.preserve_recovery_controls(**arguments) == paths
    assert all(path.read_bytes() == data for path, data in before.items())
    paths[-1].write_bytes(b"changed sealed control")
    with pytest.raises(WorkspaceV2CutoverError, match="sealed recovery control differs"):
        coordinator.preserve_recovery_controls(**arguments)
