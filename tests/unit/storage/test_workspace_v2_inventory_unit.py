"""Bound transition authorization and immutable ordinary historical writes."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import investment_analyst.workspace.workspace_v2_migration as migration_module
from investment_analyst.storage.compact_analytical_v2 import (
    _METRIC_SELECT_COLUMNS,
    CompactAnalyticalError,
    CompactAnalyticalStore,
)
from investment_analyst.storage.historical_analytical_import import DirectHistoricalAnalyticalSource
from investment_analyst.storage.resource_policy import (
    DatabaseResourceAssignment,
    DuckDBResourcePolicy,
    MaintenanceExecution,
    MaintenanceResourceBudget,
    MaintenanceResourceReceipt,
    maintenance_execution,
    maintenance_resource_budget,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.storage.workspace_v2_inventory import (
    WorkspaceV2AnalyticalImporter,
    WorkspaceV2InventoryError,
    WorkspaceV2ProgressReconciler,
    _VerifiedParentMetricHasher,
    historical_transition_status,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_lifecycle_models import FrozenSourceCut
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    migrate_workspace_v1_to_v2,
    quiescent_source_cut,
    reserve_recovered_successor,
    verify_recovered_successor,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    HistoricalSealSnapshot,
    HistoricalTransition,
    ProgressReconciliation,
    ReconciliationFileReference,
    RecoveredProgressInventory,
    SuccessorCoverageProof,
)

from .conftest import make_diagnostic_result, make_metric_result, make_observation, make_raw_record


@pytest.mark.parametrize("value", ["0", "1.00", "3E-12", "-0.00", "123456789012345678.123456789"])
def test_parent_fast_hash_preserves_decimal_unicode_utc_and_shared_references(
    tmp_path: Path, value: str
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    metric = make_metric_result(observation_id=uuid4()).model_copy(
        update={
            "value": Decimal(value),
            "parameters": {"texto": 'acción\\"\n', "nested": [None, True, 1, 0.5]},
            "computed_at": datetime(2026, 10, 16, 12, 1, 2, 123456, tzinfo=UTC),
            "input_metric_result_ids": [uuid4()],
        }
    )
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        compact.save_metrics((metric,), origin="HISTORICAL")
        row = storage.store.connection.execute(
            f"SELECT {_METRIC_SELECT_COLUMNS} FROM workspace_metric_results_v2"
        ).fetchone()
        assert row is not None
        expected = sha256_hex(canonical_json_bytes(metric))
        attested = (*row, expected)
        hasher = _VerifiedParentMetricHasher(compact)
        hasher.prepare((attested,))
        assert hasher.verify(attested) == expected
        assert hasher.fast_count == 1 and hasher.typed_fallback_count == 0
        assert hasher.cache_bytes <= 1 << 20


@pytest.mark.parametrize(
    "column,replacement",
    [(1, "LIVE"), (4, "999"), (8, "2026-10-16 01:02:03+00"), (14, 8), (15, "0" * 64)],
)
def test_parent_fast_hash_rejects_identity_and_financial_tampering(
    tmp_path: Path, column: int, replacement: object
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    metric = make_metric_result(observation_id=uuid4())
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        compact.save_metrics((metric,), origin="HISTORICAL")
        row = storage.store.connection.execute(
            f"SELECT {_METRIC_SELECT_COLUMNS} FROM workspace_metric_results_v2"
        ).fetchone()
        assert row is not None
        attested = list((*row, sha256_hex(canonical_json_bytes(metric))))
        attested[column] = replacement
        # Persist financial tampering too: a full typed fallback must detect it.
        if column in (4, 8):
            field = "value_text" if column == 4 else "computed_at"
            storage.store.connection.execute(
                f"UPDATE workspace_metric_results_v2 SET {field}=?", [replacement]
            )
        with pytest.raises((WorkspaceV2InventoryError, CompactAnalyticalError)):
            _VerifiedParentMetricHasher(compact).verify(tuple(attested))


def test_parent_fast_hash_requires_attestation_and_loads_evicted_fragment_only(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    metric = make_metric_result(observation_id=uuid4())
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        compact.save_metrics((metric,), origin="HISTORICAL")
        row = storage.store.connection.execute(
            f"SELECT {_METRIC_SELECT_COLUMNS} FROM workspace_metric_results_v2"
        ).fetchone()
        assert row is not None
        hasher = _VerifiedParentMetricHasher(compact)
        with pytest.raises(WorkspaceV2InventoryError, match="attested"):
            hasher.verify((*row, None))
        hasher._BUFFER = 48
        attested = (*row, sha256_hex(canonical_json_bytes(metric)))
        hasher.prepare((attested,))
        assert hasher.verify(attested) == attested[16]
        assert hasher.cache_bytes <= 48
        # A different scalar representation can only pass after typed recovery.
        variant = list(attested)
        variant[3] = f" {metric.metric_key} "
        assert hasher.verify(tuple(variant)) == attested[16]
        assert hasher.typed_fallback_count == 1
        hasher.cache.clear()
        hasher.cache_bytes = 0
        hasher._BUFFER = 20
        with pytest.raises(WorkspaceV2InventoryError, match="buffer budget"):
            hasher.prepare((attested,))


@pytest.mark.parametrize("size", [32, 128, 256])
def test_parent_hash_batches_unique_fragments_without_hydrating_known_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    metrics = [
        make_metric_result(observation_id=uuid4()).model_copy(
            update={"parameters": {"index": index}}
        )
        for index in range(size)
    ]
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        compact.save_metrics(metrics, origin="HISTORICAL")
        expected = {str(m.result_id): sha256_hex(canonical_json_bytes(m)) for m in metrics}
        rows = [
            (*row, expected[str(row[0])])
            for row in storage.store.connection.execute(
                f"SELECT {_METRIC_SELECT_COLUMNS} FROM workspace_metric_results_v2"
            ).fetchall()
        ]
        loads = {"content": 0, "sequences": 0}
        content, sequences = compact._load_content_batch, compact._load_sequences

        def load_content(*args: object, **kwargs: object) -> object:
            loads["content"] += 1
            return content(*args, **kwargs)

        def load_sequences(*args: object, **kwargs: object) -> object:
            loads["sequences"] += 1
            return sequences(*args, **kwargs)

        def reject_hydration(*args: object, **kwargs: object) -> object:
            pytest.fail("attested canonical parent unnecessarily hydrated")

        monkeypatch.setattr(compact, "_load_content_batch", load_content)
        monkeypatch.setattr(compact, "_load_sequences", load_sequences)
        monkeypatch.setattr(compact, "get_metrics", reject_hydration)
        hasher = _VerifiedParentMetricHasher(compact)
        hasher.prepare(rows)
        for row in rows:
            assert hasher.verify(row) == expected[str(row[0])]
        assert hasher.fast_count == size
        assert loads["content"] == 1
        assert loads["sequences"] <= 2
        assert hasher.cache_bytes <= 1 << 20


def _prepared_progress(tmp_path: Path) -> ProgressReconciliation:
    now = datetime(2026, 10, 16, tzinfo=UTC)
    destination = tmp_path / "destination"

    def reference(name: str, role: str) -> ReconciliationFileReference:
        return ReconciliationFileReference.model_validate(
            {"path": destination / name, "role": role, "sha256": "a" * 64, "size_bytes": 1}
        )

    return ProgressReconciliation(
        operation_id=uuid4(),
        predecessor_operation_id=uuid4(),
        predecessor=reference("state/predecessor.json", "control"),
        predecessor_proofs=(reference("state/proof.json", "control"),),
        parent_membership=reference("state/parent.jsonl", "inventory"),
        parent_seal=HistoricalSealSnapshot(
            source_fingerprint="b" * 64,
            metric_count=2,
            metric_digest="c" * 64,
            diagnostic_count=1,
            diagnostic_digest="d" * 64,
            sealed_at=now,
        ),
        source_workspace_id=uuid4(),
        source_created_at=now,
        source_root=tmp_path / "source",
        destination_root=destination,
        destination_workspace_id=uuid4(),
        database_before=reference("storage/v2/index.duckdb", "database"),
        wal_before=reference("storage/v2/index.duckdb.wal", "wal"),
        predecessor_code=reference("code.json", "code"),
        predecessor_tree_fingerprint="e" * 64,
        predecessor_environment_fingerprint="f" * 64,
        resource_receipt=reference("admission.json", "resource"),
        prepared_at=now,
        updated_at=now,
    )


def _progress_update(progress: ProgressReconciliation, **changes: object) -> ProgressReconciliation:
    return ProgressReconciliation.model_validate({**progress.model_dump(), **changes})


def _verified_progress(tmp_path: Path) -> ProgressReconciliation:
    prepared = _prepared_progress(tmp_path)
    inventory = RecoveredProgressInventory(
        raw_count=5,
        observation_count=4,
        metric_count=3,
        diagnostic_count=2,
        metric_digest="1" * 64,
        diagnostic_digest="2" * 64,
        dataset_sha256="3" * 64,
        delta_membership=prepared.parent_membership,
        relationships=prepared.predecessor,
        verified_at=prepared.prepared_at,
    )
    return _progress_update(
        prepared,
        phase="BASELINE_VERIFIED",
        database_after=prepared.database_before,
        recovered_at=prepared.prepared_at,
        current_tree_fingerprint="4" * 64,
        current_environment_fingerprint="5" * 64,
        inventory=inventory,
    )


def _generation_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from types import SimpleNamespace

    from investment_analyst.application import local_release
    from investment_analyst.storage.workspace_v2_inventory import (
        WorkspaceV2GenerationLedger,
        reconciliation_file_reference,
    )
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutInvalidation,
        CutOwner,
        ReconciliationGenerationReference,
    )

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root = tmp_path / "destination"
    initialized = service.initialize(root, format_version=2)
    state = root / "state"
    proof = state / "fixture-proof.json"
    proof.write_bytes(b"{}")
    ref = reconciliation_file_reference(proof, "control")
    baseline = _progress_update(
        _verified_progress(tmp_path),
        destination_workspace_id=initialized.manifest.workspace_id,
    )
    baseline_path = state / "fixture-baseline.json"
    baseline_path.write_bytes(canonical_json_bytes(baseline))
    baseline_ref = reconciliation_file_reference(baseline_path, "control")
    attempt = uuid4()
    cut = FrozenSourceCut(
        operation_id=attempt,
        source_root=baseline.source_root,
        workspace_id=baseline.source_workspace_id,
        created_at=baseline.source_created_at,
        workspace_format_version=1,
        source_fingerprint="a" * 64,
        writer_closed=True,
        frozen_at=baseline.prepared_at,
    )
    resource = ref.model_copy(update={"role": "resource"})
    reservation = migration_module._SuccessorReservation(
        reconciliation_id=baseline.operation_id,
        successor=cut,
        source_session=uuid4(),
        baseline=baseline_ref,
        baseline_dataset_sha256=baseline.inventory.dataset_sha256,
        module_sha256="b" * 64,
        resource_receipt=resource,
        recorded_at=baseline.prepared_at,
    )
    reservation_path = state / "fixture-reservation.json"
    reservation_path.write_bytes(canonical_json_bytes(reservation))
    reservation_ref = reconciliation_file_reference(reservation_path, "control")
    coverage = SuccessorCoverageProof(
        successor=cut,
        baseline_dataset_sha256=baseline.inventory.dataset_sha256,
        covered_metric_count=baseline.inventory.metric_count,
        covered_diagnostic_count=baseline.inventory.diagnostic_count,
        comparison_sha256="c" * 64,
        verifier_tree_fingerprint=baseline.current_tree_fingerprint,
        verified_at=baseline.prepared_at,
    )
    bound = _progress_update(
        baseline,
        phase="SUCCESSOR_BOUND",
        reserved_successor=cut,
        reserved_successor_id=attempt,
        coverage=coverage,
    )
    pointer = state / f"progress-reconciliation.{baseline.operation_id}.json"
    pointer.write_bytes(canonical_json_bytes(bound))
    archived = state / "fixture-old-certificate.json"
    archived.write_bytes(pointer.read_bytes())
    old = ReconciliationGenerationReference(
        schema_type="workspace-v2-progress-reconciliation-v1",
        artifact=reconciliation_file_reference(archived, "control"),
    )
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(),
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=root / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    temp_directory=tmp_path / "temporary",
                ),
            ),
        ),
    )
    admission = MaintenanceResourceReceipt(
        budget=budget,
        pid=os.getpid(),
        process_start_ticks=1,
        boot_id=uuid4(),
        cgroup_path=Path("/sys/fs/cgroup/fixture"),
        resource_policy_sha256="a" * 64,
        coordinator_sha256="b" * 64,
        python_version="3.12",
        duckdb_version="fixture",
        stage="admitted",
        observed_memory_high_bytes=budget.memory_high_bytes,
        observed_memory_max_bytes=budget.memory_max_bytes,
        observed_swap_max_bytes=0,
        host_free_bytes=30_000_000_000,
        effective_settings=(),
        recorded_at=baseline.prepared_at,
    )
    generation_id = uuid4()
    event = CutInvalidation(
        event_id=uuid4(),
        generation_id=generation_id,
        attempt_id=attempt,
        certificate=old,
        reservation=reservation_ref,
        owner=CutOwner(source_session=reservation.source_session),
        source_root=baseline.source_root,
        source_workspace_id=baseline.source_workspace_id,
        destination_root=root,
        destination_workspace_id=initialized.manifest.workspace_id,
        database=reconciliation_file_reference(root / "storage/v2/index.duckdb", "database"),
        wal=None,
        write_generation=ref,
        historical_seal=None,
        historical_receipt=None,
        reason="EVIDENCE_INVALID",
        owner_absence=ref,
        exclusivity=ref,
        no_activation=ref,
        code_sha256="a" * 64,
        policy_sha256="b" * 64,
        recorded_at=baseline.prepared_at,
    )
    generation_values = dict(
        generation_id=generation_id,
        ordinal=1,
        previous_generation=old,
        source_root=baseline.source_root,
        source_workspace_id=baseline.source_workspace_id,
        source_created_at=baseline.source_created_at,
        destination_root=root,
        destination_workspace_id=initialized.manifest.workspace_id,
        predecessor=ref,
        previous_attempt=ref,
        previous_reservation=reservation_ref,
        previous_receipts=(ref,),
        parent_membership=ref.model_copy(update={"role": "inventory"}),
        parent_seal=baseline.parent_seal,
        verified_w=baseline_ref,
        database=event.database,
        wal=None,
        code=(ref.model_copy(update={"role": "code"}),),
        tree_fingerprint="c" * 64,
        environment_fingerprint="d" * 64,
        resource_receipt=resource,
        prepared_at=baseline.prepared_at,
        recorded_at=baseline.prepared_at,
    )
    monkeypatch.setattr(
        local_release,
        "LocalReleaseService",
        lambda: SimpleNamespace(
            load_workspace_bindings=lambda: SimpleNamespace(current=None),
        ),
    )
    return WorkspaceV2GenerationLedger(service, root), event, generation_values, admission


def test_generation_invalidation_survives_before_head_and_cas_preserves_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_analyst.workspace.workspace_v2_migration_models import ProgressReconciliationV2

    ledger, event, values, admission = _generation_fixture(tmp_path, monkeypatch)
    before = event.certificate.artifact.path.read_bytes(), event.reservation.path.read_bytes()
    budget = admission.budget
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        invalidation = ledger.invalidate(event, admission=admission)
        assert ledger.resolve() is None
        assert ledger.attempt_invalidated(event.attempt_id)
        assert ledger.invalidate(event, admission=admission) == invalidation
        generation = ProgressReconciliationV2(**values, invalidation=invalidation)
        published = ledger.publish(generation, expected_head_sha256=None, admission=admission)
        head, resolved = ledger.resolve()
        assert head.certificate == published and resolved == generation
        with pytest.raises(WorkspaceV2InventoryError, match="compare-and-swap"):
            ledger.publish(generation, expected_head_sha256=None, admission=admission)
        assert before == (
            event.certificate.artifact.path.read_bytes(),
            event.reservation.path.read_bytes(),
        )
        with pytest.raises(ValueError, match="baseline generation requires"):
            ProgressReconciliationV2.model_validate(
                {**generation.model_dump(), "phase": "BASELINE_VERIFIED"}
            )


@pytest.mark.parametrize("fault", ["session", "destination", "active", "live_owner"])
def test_generation_invalidation_rejects_wrong_identity_and_live_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from types import SimpleNamespace

    from investment_analyst.application import local_release
    from investment_analyst.workspace.workspace_v2_migration_models import CutInvalidation, CutOwner

    ledger, event, _, admission = _generation_fixture(tmp_path, monkeypatch)
    changes = {}
    if fault == "session":
        changes["owner"] = CutOwner(source_session=uuid4())
    if fault == "destination":
        changes["destination_workspace_id"] = uuid4()
    if fault == "active":
        monkeypatch.setattr(
            local_release,
            "LocalReleaseService",
            lambda: SimpleNamespace(
                load_workspace_bindings=lambda: SimpleNamespace(
                    current=SimpleNamespace(workspace_format_version=2)
                ),
            ),
        )
    if fault == "live_owner":
        boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        ticks = int(Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(") ", 1)[1].split()[19])
        changes["owner"] = CutOwner(
            source_session=event.owner.source_session,
            pid=os.getpid(),
            start_ticks=ticks,
            boot_id=boot,
            cgroup=Path("/sys/fs/cgroup/fixture"),
        )
    event = CutInvalidation.model_validate({**event.model_dump(), **changes})
    budget = admission.budget
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
        pytest.raises(WorkspaceV2InventoryError),
    ):
        ledger.invalidate(event, admission=admission)
    assert ledger.resolve() is None
    assert not list(ledger.state.glob("cut-invalidation.*.json"))


@pytest.mark.parametrize(
    "case",
    [
        0,
        1,
        3,
        "changed_w",
        "unreceipted",
        "bad_seal",
        "extra_w_projection",
        "unknown_bad_content",
        "unknown_bad_segment",
        "unknown_bad_sequence",
        "orphan_membership",
    ],
)
def test_corrective_generation_reuses_native_w_and_validates_all_added_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: int | str,
) -> None:
    from investment_analyst.storage.workspace_v2_inventory import (
        reconciliation_file_reference,
        verify_generation_contents,
    )

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root, snapshot = tmp_path / "destination", tmp_path / "snapshot-w"
    initialized = service.initialize(root, format_version=2)
    raw = make_raw_record()
    observation = make_observation(raw_record_id=raw.record_id)
    metric = make_metric_result(observation_id=observation.observation_id)
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        CompactAnalyticalStore(storage.store.connection).save_metrics(
            (metric,), origin="HISTORICAL"
        )
    shutil.copytree(root, snapshot)  # Small fixture, never the real workspace.
    database = reconciliation_file_reference(root / "storage/v2/index.duckdb", "database")
    now = datetime(2026, 10, 17, tzinfo=UTC)
    prepared = _prepared_progress(tmp_path)
    parent = prepared.parent_seal.model_copy(update={"metric_count": 0, "diagnostic_count": 0})
    inventory = RecoveredProgressInventory(
        raw_count=1,
        observation_count=1,
        metric_count=1,
        diagnostic_count=0,
        metric_digest=sha256_hex(canonical_json_bytes(metric)),
        diagnostic_digest=hashlib.sha256(b"").hexdigest(),
        dataset_sha256="1" * 64,
        delta_membership=prepared.parent_membership,
        relationships=prepared.predecessor,
        verified_at=now,
    )
    baseline = _progress_update(
        prepared,
        phase="BASELINE_VERIFIED",
        parent_seal=parent,
        destination_workspace_id=initialized.manifest.workspace_id,
        database_after=database,
        recovered_at=now,
        inventory=inventory,
        current_tree_fingerprint="a" * 64,
        current_environment_fingerprint="b" * 64,
    )
    certificate = tmp_path / "verified-w.json"
    certificate.write_bytes(canonical_json_bytes(baseline))
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        count = case if isinstance(case, int) else 1
        new = []
        for index in range(count):
            added = metric.model_copy(
                update={
                    "result_id": UUID(int=(4 << 76) | (2 << 62) | (index + 1)),
                    "value": Decimal(index + 10),
                    "computed_at": metric.computed_at + timedelta(days=2),
                }
            )
            compact.save_metrics((added,), origin="HISTORICAL")
            new.append(added.result_id)
        values = compact.historical_inventory_digests()
        compact.seal_historical_inventory(
            source_fingerprint="f" * 64,
            metric_count=values[0],
            metric_digest=values[1],
            diagnostic_count=values[2],
            diagnostic_digest=values[3],
            sealed_at=now,
        )
        if case == "changed_w":
            storage.store.connection.execute(
                "UPDATE workspace_metric_results_v2 SET value_text='123' WHERE result_id=?",
                [str(metric.result_id)],
            )
        if case == "bad_seal":
            storage.store.connection.execute(
                "UPDATE workspace_v2_historical_seal SET metric_digest=?", ["f" * 64]
            )
        if case == "extra_w_projection":
            storage.store.connection.execute(
                "INSERT INTO workspace_raw_json_projections_v2 VALUES (?, ?, ?)",
                [str(raw.record_id), "unexpected", "extra"],
            )
        if case == "unknown_bad_content":
            storage.store.connection.execute(
                "INSERT INTO workspace_analytical_content_v2 VALUES (?, ?, ?, ?, ?)",
                ["f" * 64, "parameters", "a" * 64, 2, b"{}"],
            )
        if case in {"unknown_bad_segment", "unknown_bad_sequence"}:
            table = (
                "workspace_analytical_segments_v2"
                if case == "unknown_bad_segment"
                else "workspace_analytical_sequences_v2"
            )
            storage.store.connection.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?, ?)",
                ["f" * 64, "metric-input-result-v1", 1, "a" * 64],
            )
        if case == "orphan_membership":
            storage.store.connection.execute(
                "INSERT INTO workspace_analytical_sequence_segments_v2 VALUES (?, ?, ?)",
                ["f" * 64, 0, "e" * 64],
            )
    operation = uuid4()
    budget = MaintenanceResourceBudget(
        operation_id=operation,
        profile="verification",
        assignments=tuple(
            DatabaseResourceAssignment(
                database=path / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    spill_limit_bytes=0 if path == snapshot else 1_000_000_000,
                    temp_directory=tmp_path
                    / ("temporary-w" if path == snapshot else "temporary-current"),
                ),
            )
            for path in (root, snapshot)
        ),
    )
    admission = MaintenanceResourceReceipt(
        budget=budget,
        pid=os.getpid(),
        process_start_ticks=1,
        boot_id=uuid4(),
        cgroup_path=Path("/sys/fs/cgroup/fixture"),
        resource_policy_sha256="a" * 64,
        coordinator_sha256="b" * 64,
        python_version="3.12",
        duckdb_version="fixture",
        stage="admitted",
        observed_memory_high_bytes=budget.memory_high_bytes,
        observed_memory_max_bytes=budget.memory_max_bytes,
        observed_swap_max_bytes=0,
        host_free_bytes=30_000_000_000,
        effective_settings=(),
        recorded_at=now,
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(admission.model_dump_json())
    get_metrics = CompactAnalyticalStore.get_metrics
    seen = []

    def only_additions(self, identifiers):
        assert metric.result_id not in identifiers, "known W financial model rehydrated"
        seen.extend(identifiers)
        return get_metrics(self, identifiers)

    monkeypatch.setattr(CompactAnalyticalStore, "get_metrics", only_additions)
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        arguments = dict(
            generation_id=operation,
            destination=root,
            baseline_root=snapshot,
            baseline_certificate=reconciliation_file_reference(certificate, "control"),
            baseline_database_sha256=database.sha256,
            admission_path=admission_path,
        )
        if case in {
            "changed_w",
            "bad_seal",
            "extra_w_projection",
            "unknown_bad_content",
            "unknown_bad_segment",
            "unknown_bad_sequence",
            "orphan_membership",
        }:
            with pytest.raises((WorkspaceV2InventoryError, CompactAnalyticalError)):
                verify_generation_contents(service, **arguments)
        else:
            result = verify_generation_contents(service, **arguments)
            assert result.metric_count == 1 + count
            assert dict(result.validated_additions)["metrics"] == count
            assert set(seen) == set(new)
            assert result.comparison_scope == "preserved-W-subset-current-destination"


def test_progress_recovery_is_not_baseline_or_successor(tmp_path: Path) -> None:
    prepared = _prepared_progress(tmp_path)
    recovered = _progress_update(
        prepared, database_after=prepared.database_before, recovered_at=prepared.prepared_at
    )
    assert recovered.phase == "PREPARED" and recovered.inventory is None
    assert recovered.predecessor_operation_id == prepared.predecessor_operation_id
    with pytest.raises(ValueError, match="baseline requires"):
        _progress_update(recovered, phase="BASELINE_VERIFIED")
    with pytest.raises(ValueError, match="prepared progress"):
        _progress_update(prepared, reserved_successor_id=uuid4())


def test_successor_reservation_requires_complete_coverage_to_bind(tmp_path: Path) -> None:
    baseline = _verified_progress(tmp_path)
    cut = FrozenSourceCut(
        operation_id=uuid4(),
        source_root=baseline.source_root,
        workspace_id=baseline.source_workspace_id,
        workspace_format_version=1,
        created_at=baseline.source_created_at,
        source_fingerprint="6" * 64,
        frozen_at=baseline.prepared_at,
    )
    reserved = _progress_update(
        baseline, reserved_successor_id=cut.operation_id, reserved_successor=cut
    )
    assert reserved.phase == "BASELINE_VERIFIED" and reserved.coverage is None
    with pytest.raises(ValueError, match="complete baseline coverage"):
        _progress_update(reserved, phase="SUCCESSOR_BOUND")
    assert reserved.inventory is not None
    coverage = SuccessorCoverageProof(
        successor=cut,
        baseline_dataset_sha256=reserved.inventory.dataset_sha256,
        covered_metric_count=3,
        covered_diagnostic_count=2,
        comparison_sha256="7" * 64,
        verifier_tree_fingerprint="4" * 64,
        verified_at=baseline.prepared_at,
    )
    bound = _progress_update(reserved, phase="SUCCESSOR_BOUND", coverage=coverage)
    assert bound.parent_seal == baseline.parent_seal
    for change in (
        {"covered_metric_count": 2},
        {"baseline_dataset_sha256": "8" * 64},
        {"verifier_tree_fingerprint": "9" * 64},
    ):
        with pytest.raises(ValueError, match="complete baseline coverage"):
            _progress_update(
                reserved,
                phase="SUCCESSOR_BOUND",
                coverage=SuccessorCoverageProof.model_validate({**coverage.model_dump(), **change}),
            )
    with pytest.raises(ValueError, match="atomically"):
        _progress_update(reserved, coverage=coverage)


@pytest.mark.parametrize(
    "field,value",
    [
        ("exclusive", False),
        ("page_limit", 257),
        ("page_limit", 1.5),
        ("destination_root", Path("relative")),
        ("verifier_policy", "old-verifier"),
        ("prepared_at", datetime(2026, 10, 16)),
        ("extra_binding", True),
    ],
)
def test_progress_contract_rejects_unsafe_bindings(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        _progress_update(_prepared_progress(tmp_path), **{field: value})


def test_progress_rejects_foreign_roots_and_predecessor_epoch(tmp_path: Path) -> None:
    prepared = _prepared_progress(tmp_path)
    for changes in (
        {"source_root": prepared.destination_root / "child"},
        {"operation_id": prepared.predecessor_operation_id},
        {"database_before": prepared.wal_before},
        {"recovered_at": prepared.prepared_at},
    ):
        with pytest.raises(ValueError):
            _progress_update(prepared, **changes)


@pytest.mark.parametrize(
    "financial_fault,successor_case",
    [
        (None, 0),
        ("parent_value", 0),
        ("new_quality", 0),
        (None, 1),
        (None, 3),
        (None, "changed_w"),
        (None, "noncanonical"),
        (None, "metadata"),
        (None, "missing_w"),
        (None, "invalid_new"),
        (None, "reservation_crash"),
        (None, "coverage_crash"),
        (None, "reopened_session"),
        (None, "append_zero"),
        (None, "append_crash"),
        (None, "pipeline"),
        (None, "observation_content"),
        (None, "observation_metadata"),
        (None, "observation_missing"),
        (None, "observation_noncanonical"),
    ],
)
def test_reconciliation_recovers_once_and_preserves_predecessor_after_receipt_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    financial_fault: str | None,
    successor_case: int | str,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    raw = make_raw_record()
    observation = make_observation(raw_record_id=raw.record_id)
    metric = make_metric_result(
        observation_id=observation.observation_id,
        result_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    metric = metric.model_copy(
        update={
            "available_at": metric.available_at + timedelta(seconds=1),
            "parameters": {"window": 1, "texto": "acción\n雪"},
        }
    )
    diagnostic = make_diagnostic_result(metric_result_id=metric.result_id)
    diagnostic = diagnostic.model_copy(update={"available_at": metric.available_at})
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save(metric)
        storage.diagnostics.save(diagnostic)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        host_free_bytes=lambda: 30_000_000_000,
        page_limit=1,
    )
    extra = metric.model_copy(
        update={
            "result_id": UUID("ffffffff-ffff-4fff-bfff-ffffffffffff"),
            "available_at": metric.available_at - timedelta(seconds=1),
            "computed_at": metric.computed_at + timedelta(days=1),
        }
    )
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.metric_results.save(extra)
    predecessor_id = uuid4()
    metric_pages = 0

    def interrupt(event: str) -> None:
        nonlocal metric_pages
        if event == "after_metric_page_write":
            metric_pages += 1
            if metric_pages == 2:
                raise RuntimeError("fixture interruption")

    with (
        quiescent_source_cut(service, source, operation_id=predecessor_id) as cut,
        pytest.raises(RuntimeError, match="fixture interruption"),
    ):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=destination,
            host_free_bytes=lambda: 30_000_000_000,
            page_limit=1,
            frozen_source=cut,
            analytical_failure_injector=interrupt,
        )
    predecessor_path = destination / f"state/historical-transition.{predecessor_id}.json"
    predecessor = HistoricalTransition.model_validate_json(predecessor_path.read_bytes())
    original = predecessor_path.read_bytes(), predecessor.parent_membership_path.read_bytes()
    if financial_fault is not None:
        with service.open_storage(
            service.resolve(destination), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            field = "value_text" if financial_fault == "parent_value" else "quality"
            storage.store.connection.execute(
                f"UPDATE workspace_metric_results_v2 SET {field}=? WHERE result_id=?",
                [
                    "999" if financial_fault == "parent_value" else "invalid-fixture-quality",
                    str(metric.result_id if financial_fault == "parent_value" else extra.result_id),
                ],
            )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import duckdb,os,sys; c=duckdb.connect(sys.argv[1],config="
            "{'memory_limit':'256000000B','threads':'1','max_temp_directory_size':'0B'});"
            "c.execute('CREATE TABLE wal_probe AS SELECT 42 AS id');os._exit(0)",
            str(destination / "storage/v2/index.duckdb"),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert child.returncode == 0
    assert (destination / "storage/v2/index.duckdb.wal").stat().st_size > 0
    code, proof = tmp_path / "old-code.json", tmp_path / "proof.json"
    proof.write_text("{}\n")
    operation_id = uuid4()
    package = Path(__file__).resolve().parents[3] / "src/investment_analyst"
    archived = []
    for relative in (
        "storage/workspace_v2_inventory.py",
        "storage/compact_analytical_v2.py",
        "storage/historical_analytical_import.py",
        "workspace/workspace_v2_migration_models.py",
    ):
        current = package / relative
        saved = tmp_path / "old-code" / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        data = current.read_bytes()
        saved.write_bytes(data)
        archived.append(
            {
                "archive": str(saved),
                "source": str(current),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    code.write_text(
        json.dumps(
            {
                "files": archived,
                "tree_fingerprint": predecessor.tree_fingerprint,
                "environment_fingerprint": predecessor.environment_fingerprint,
                "predecessor_operation_id": str(predecessor_id),
                "reconciliation_operation_id": str(operation_id),
                "purpose": "fixture provenance only",
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    budget = MaintenanceResourceBudget(
        operation_id=operation_id,
        profile="wal_recovery",
        assignments=(
            DatabaseResourceAssignment(
                database=destination / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=256_000_000,
                    threads=1,
                    spill_limit_bytes=0,
                    temp_directory=tmp_path / "temp",
                ),
            ),
        ),
    )
    admission = MaintenanceResourceReceipt(
        budget=budget,
        pid=os.getpid(),
        process_start_ticks=1,
        boot_id=uuid4(),
        cgroup_path=Path("/fixture"),
        stage="admitted",
        observed_memory_high_bytes=budget.memory_high_bytes,
        observed_memory_max_bytes=budget.memory_max_bytes,
        observed_swap_max_bytes=0,
        host_free_bytes=30_000_000_000,
        effective_settings=(),
        recorded_at=datetime.now(UTC),
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(admission.model_dump_json())
    reconciler = WorkspaceV2ProgressReconciler(service, destination, operation_id)
    request = {
        "predecessor_path": predecessor_path,
        "predecessor_proofs": (proof,),
        "predecessor_code": code,
        "admission_path": admission_path,
    }
    with pytest.raises(Exception, match="maintenance_admission"):
        reconciler.recover(**request)
    assert not reconciler.path.exists()
    publish = reconciler._publish

    def crash_on_receipt(progress: ProgressReconciliation) -> ReconciliationFileReference:
        if progress.recovered_at is not None:
            raise RuntimeError("fixture receipt crash")
        return publish(progress)

    execution = MaintenanceExecution(budget, lambda *_: None)
    with maintenance_resource_budget(budget), maintenance_execution(execution):
        monkeypatch.setattr(reconciler, "_publish", crash_on_receipt)
        with pytest.raises(RuntimeError, match="fixture receipt crash"):
            reconciler.recover(**request)
        preserved = ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
        assert preserved.recovered_at is None and len(preserved.recovery_attempts) == 1
        monkeypatch.setattr(reconciler, "_publish", publish)
        receipt = reconciler.recover(**request)
        assert receipt.stage == "recovered" and receipt.reconciliation.phase == "PREPARED"
        assert len(receipt.reconciliation.recovery_attempts) == 2
        assert receipt.reconciliation.wal_before is not None
        assert receipt.reconciliation.inventory is None
        assert receipt.reconciliation.parent_seal == predecessor.parent_seal
        assert reconciler.recover(**request).reconciliation == receipt.reconciliation
        other = WorkspaceV2ProgressReconciler(service, destination, uuid4())
        with pytest.raises(WorkspaceV2InventoryError):
            other.recover(**request)
    assert original == (
        predecessor_path.read_bytes(),
        predecessor.parent_membership_path.read_bytes(),
    )

    with service.open_storage(
        service.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        compact = CompactAnalyticalStore(storage.store.connection)
        assert storage.store.connection.execute("SELECT id FROM wal_probe").fetchone() == (42,)
        assert compact.count_metrics(origin="HISTORICAL") == 2
        assert compact.historical_seal().model_dump() == predecessor.parent_seal.model_dump()

    verification_budget = MaintenanceResourceBudget.model_validate(
        {**budget.model_dump(), "profile": "verification"}
    )
    verification_admission = admission.model_copy(update={"budget": verification_budget})
    verification_path = tmp_path / "verification-admission.json"
    verification_path.write_text(verification_admission.model_dump_json())
    with (
        maintenance_resource_budget(verification_budget),
        maintenance_execution(MaintenanceExecution(verification_budget, lambda *_: None)),
    ):
        proof_result = reconciler.verify_relationships(admission_path=verification_path)
        assert (
            proof_result.raw_count,
            proof_result.observation_count,
            proof_result.metric_count,
            proof_result.diagnostic_count,
        ) == (1, 1, 2, 1)
        assert proof_result.database == receipt.reconciliation.database_after
        profile = reconciler.verify_relationships(
            admission_path=verification_path, profile_only=True
        )
        assert profile.profile_only is True and profile.metric_equal_edges == 0
        assert profile.schema_version == "workspace-v2-progress-relationship-profile-v1"
    assert (
        ProgressReconciliation.model_validate_json(reconciler.path.read_bytes()).phase == "PREPARED"
    )
    proof_path = next(
        destination.glob(f"state/progress-relationships.{reconciler.operation_id}.*.json")
    )
    native_step = MaintenanceExecution.step

    def require_supervisor_phase(self: MaintenanceExecution, phase: str, seconds: int) -> object:
        assert phase in {"ready", "open", "sql"}
        return native_step(self, phase, seconds)

    monkeypatch.setattr(MaintenanceExecution, "step", require_supervisor_phase)
    with (
        maintenance_resource_budget(verification_budget),
        maintenance_execution(MaintenanceExecution(verification_budget, lambda *_: None)),
    ):
        forged_proof = tmp_path / "mismatched-relationships.json"
        forged_proof.write_text(
            proof_result.model_copy(update={"verifier_module_sha256": "0" * 64}).model_dump_json()
        )
        with pytest.raises(WorkspaceV2InventoryError, match="does not bind"):
            reconciler.verify_financial_baseline(
                admission_path=verification_path, relationships_path=forged_proof
            )
        if financial_fault is not None:
            with pytest.raises((WorkspaceV2InventoryError, CompactAnalyticalError)):
                reconciler.verify_financial_baseline(
                    admission_path=verification_path, relationships_path=proof_path
                )
            assert (
                ProgressReconciliation.model_validate_json(reconciler.path.read_bytes()).phase
                == "PREPARED"
            )
            assert original == (
                predecessor_path.read_bytes(),
                predecessor.parent_membership_path.read_bytes(),
            )
            return
        checkpoint = WorkspaceV2ProgressReconciler._financial_checkpoint

        def crash_before_delta_checkpoint(path: Path, work: object) -> None:
            if json.loads(canonical_json_bytes(work))["delta_bytes"]:
                raise RuntimeError("fixture financial checkpoint interruption")
            checkpoint(path, work)

        monkeypatch.setattr(
            WorkspaceV2ProgressReconciler,
            "_financial_checkpoint",
            staticmethod(crash_before_delta_checkpoint),
        )
        with pytest.raises(RuntimeError, match="financial checkpoint interruption"):
            reconciler.verify_financial_baseline(
                admission_path=verification_path, relationships_path=proof_path
            )
        assert (
            ProgressReconciliation.model_validate_json(reconciler.path.read_bytes()).phase
            == "PREPARED"
        )
        monkeypatch.setattr(
            WorkspaceV2ProgressReconciler, "_financial_checkpoint", staticmethod(checkpoint)
        )

        def crash_after_ordered_digest(path: Path, work: object) -> None:
            checkpoint(path, work)
            if path.name.startswith("progress-ordered-digest."):
                raise RuntimeError("fixture ordered digest interruption")

        monkeypatch.setattr(
            WorkspaceV2ProgressReconciler,
            "_financial_checkpoint",
            staticmethod(crash_after_ordered_digest),
        )
        with pytest.raises(RuntimeError, match="ordered digest interruption"):
            reconciler.verify_financial_baseline(
                admission_path=verification_path, relationships_path=proof_path
            )
        monkeypatch.setattr(
            WorkspaceV2ProgressReconciler, "_financial_checkpoint", staticmethod(checkpoint)
        )
        baseline = reconciler.verify_financial_baseline(
            admission_path=verification_path, relationships_path=proof_path
        )
        assert baseline.stage == "baseline_verified"
        assert baseline.reconciliation.phase == "BASELINE_VERIFIED"
        assert baseline.reconciliation.inventory is not None
        assert baseline.reconciliation.inventory.metric_count == 2
        digest = hashlib.sha256(b"").hexdigest()
        for item in (extra, metric):
            checksum = sha256_hex(canonical_json_bytes(item))
            digest = hashlib.sha256(f"{digest}:{checksum}".encode()).hexdigest()
        assert baseline.reconciliation.inventory.metric_digest == digest
        delta = baseline.reconciliation.inventory.delta_membership.path.read_text().splitlines()
        assert len(delta) == 1 and json.loads(delta[0])[1] == str(extra.result_id)
        assert (
            reconciler.verify_financial_baseline(
                admission_path=verification_path, relationships_path=proof_path
            ).reconciliation
            == baseline.reconciliation
        )
    assert original == (
        predecessor_path.read_bytes(),
        predecessor.parent_membership_path.read_bytes(),
    )

    if (
        isinstance(successor_case, int)
        or (isinstance(successor_case, str) and successor_case.startswith("observation_"))
        or successor_case == "pipeline"
    ):
        from investment_analyst.storage.raw_v2 import RawV2Staging

        def forbid_blob_read(*_args: object, **_kwargs: object) -> bytes:
            raise AssertionError("core proof must reuse preserved immutable raw payload validation")

        with (
            maintenance_resource_budget(verification_budget),
            maintenance_execution(MaintenanceExecution(verification_budget, lambda *_: None)),
            monkeypatch.context() as native,
        ):
            native.setattr(RawV2Staging, "_read_blob", forbid_blob_read)
            core_reference = migration_module.verify_recovered_core_baseline(
                service=service,
                destination_root=destination,
                reconciliation_id=operation_id,
                admission_path=verification_path,
            )
        core = migration_module._RecoveredCoreBaseline.model_validate_json(
            core_reference.path.read_bytes()
        )
        assert core.raw_count == 1 and core.observation_count == 1
        assert core.database == baseline.reconciliation.database_after
        assert (
            core.raw_metadata_binding
            == json.loads(core.raw_terminal.path.read_bytes())["input_references"][3]
        )
        assert (
            ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
            == baseline.reconciliation
        )
    if successor_case == 0:
        # Exercise an actual archived-code receipt without rewriting the real
        # recovered baseline or executing any archived source.
        archive = tmp_path / "baseline-code"
        current_root = Path(migration_module.__file__).parent.parent
        old_digest = hashlib.sha256()
        for relative in migration_module._BASELINE_CODE_PATHS:
            old = (current_root / relative).read_bytes()
            if relative == "storage/workspace_v2_inventory.py":
                old_tree = ast.parse(old)
                for node in old_tree.body:
                    if (
                        isinstance(node, ast.ClassDef)
                        and node.name == "WorkspaceV2AnalyticalImporter"
                    ):
                        node.body = [
                            member
                            for member in node.body
                            if not isinstance(member, ast.FunctionDef)
                            or member.name
                            not in {"bind_recovered_successor", "_run_recovered_successor"}
                        ]
                old = (ast.unparse(old_tree) + "\n# archived build annotation\n").encode()
            if relative == "storage/historical_analytical_import.py":
                old_tree = ast.parse(old)
                for node in old_tree.body:
                    if (
                        isinstance(node, ast.ClassDef)
                        and node.name == "DirectHistoricalAnalyticalSource"
                    ):
                        node.body = [
                            member
                            for member in node.body
                            if not isinstance(member, ast.FunctionDef)
                            or member.name != "iter_recovered_pages"
                        ]
                old = (ast.unparse(old_tree) + "\n").encode()
            if relative == "storage/compact_analytical_v2.py":
                old_tree = ast.parse(old)
                for node in old_tree.body:
                    if isinstance(node, ast.ClassDef) and node.name == "CompactAnalyticalStore":
                        node.body = [
                            member
                            for member in node.body
                            if not isinstance(member, ast.FunctionDef)
                            or member.name
                            not in {
                                "recovered_historical_transition",
                                "publish_recovered_transition_seal",
                            }
                        ]
                old = (ast.unparse(old_tree) + "\n").encode()
            old_digest.update(hashlib.sha256(old).digest())
            path = archive / "src/investment_analyst" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(old)
        compatible_progress = baseline.reconciliation.model_copy(
            update={"current_tree_fingerprint": old_digest.hexdigest()}
        )
        isolated = WorkspaceV2ProgressReconciler(service, tmp_path / "compatibility", operation_id)
        isolated.state.mkdir(parents=True)
        with pytest.raises(WorkspaceV2MigrationError, match="requires archived baseline code"):
            migration_module._verify_baseline_code_compatibility(
                isolated, compatible_progress, None
            )
        compatible = migration_module._verify_baseline_code_compatibility(
            isolated, compatible_progress, archive
        )
        assert compatible is not None
        artifact = migration_module._BaselineVerifierCompatibility.model_validate_json(
            compatible.path.read_bytes()
        )
        assert artifact.dataset_sha256 == baseline.reconciliation.inventory.dataset_sha256
        assert artifact.baseline_tree_fingerprint == old_digest.hexdigest()
        assert (
            artifact.candidate_tree_fingerprint == baseline.reconciliation.current_tree_fingerprint
        )
        assert (
            migration_module._verify_baseline_code_compatibility(
                isolated, compatible_progress, archive
            )
            == compatible
        )
        assert (
            ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
            == baseline.reconciliation
        )
        invalid = archive / "src/investment_analyst/storage/historical_analytical_import.py"
        invalid.write_bytes(invalid.read_bytes() + b"\nUNAUTHORIZED_DEPENDENCY_CHANGE = True\n")
        with pytest.raises(WorkspaceV2MigrationError, match="verifier or dependencies changed"):
            migration_module._verify_baseline_code_compatibility(
                isolated, compatible_progress, archive
            )

    new_metrics = []
    new_observations = []
    new_raw_records = []
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        if isinstance(successor_case, int) or successor_case in {"append_crash", "pipeline"}:
            new_metrics = [
                metric.model_copy(
                    update={
                        "result_id": uuid4(),
                        "available_at": extra.available_at,
                        "computed_at": metric.computed_at + timedelta(days=2 + index),
                        "value": Decimal(f"{index + 1}.10"),
                    }
                )
                for index in range(
                    3
                    if successor_case == "append_crash"
                    else 1
                    if successor_case == "pipeline"
                    else successor_case
                )
            ]
            storage.metric_results.save_many(new_metrics)
            if isinstance(successor_case, int):
                new_raw_records = [
                    raw.model_copy(
                        update={
                            "record_id": uuid4(),
                            "received_at": raw.received_at + timedelta(days=2),
                            "payload": {"fixture": index, "position": {"report_id": str(uuid4())}},
                        }
                    )
                    for index in range(successor_case)
                ]
                storage.raw_records.save_many(new_raw_records)
                new_observations = [
                    observation.model_copy(
                        update={
                            "observation_id": uuid4(),
                            "available_at": observation.available_at - timedelta(seconds=1),
                            "normalized_at": observation.normalized_at + timedelta(days=2),
                            "value": Decimal(f"{index + 1}.100"),
                        }
                    )
                    for index in range(successor_case)
                ]
                storage.observations.save_many(new_observations)
        elif successor_case == "observation_missing":
            storage.store.connection.execute("DELETE FROM normalized_observations")
        elif successor_case == "observation_metadata":
            storage.store.connection.execute(
                "UPDATE normalized_observations SET asset_id='equity:us:msft'"
            )
        elif successor_case in {"observation_content", "observation_noncanonical"}:
            changed = observation.model_copy(update={"value": Decimal("999")})
            document = (
                canonical_json_bytes(changed).decode()
                if successor_case == "observation_content"
                else json.dumps(json.loads(canonical_json_bytes(observation)), indent=2)
            )
            storage.store.connection.execute(
                "UPDATE normalized_observations SET document_json=?", [document]
            )
        elif successor_case == "missing_w":
            storage.store.connection.execute(
                "DELETE FROM metric_results WHERE result_id=?", [str(metric.result_id)]
            )
        elif successor_case == "metadata":
            storage.store.connection.execute(
                "UPDATE metric_results SET asset_id='equity:us:msft' WHERE result_id=?",
                [str(metric.result_id)],
            )
        elif successor_case == "invalid_new":
            invalid = metric.model_copy(update={"result_id": uuid4()})
            storage.metric_results.save(invalid)
            document = json.loads(canonical_json_bytes(invalid))
            document["quality"] = "invalid"
            storage.store.connection.execute(
                "UPDATE metric_results SET document_json=?,quality='invalid' WHERE result_id=?",
                [json.dumps(document), str(invalid.result_id)],
            )
        elif successor_case in {"changed_w", "noncanonical"}:
            document = json.loads(canonical_json_bytes(metric))
            if successor_case == "changed_w":
                document["value"] = "999"
            storage.store.connection.execute(
                "UPDATE metric_results SET document_json=? WHERE result_id=?",
                [json.dumps(document, indent=2, ensure_ascii=True), str(metric.result_id)],
            )
    successor_id = uuid4()
    successor_budget = MaintenanceResourceBudget.model_validate(
        {
            **verification_budget.model_dump(),
            "operation_id": successor_id,
            "assignments": (
                *verification_budget.assignments,
                DatabaseResourceAssignment(
                    database=source / "storage/data/processed/investment_analyst.duckdb",
                    policy=DuckDBResourcePolicy(
                        memory_limit_bytes=256_000_000,
                        threads=1,
                        spill_limit_bytes=0,
                        temp_directory=tmp_path / "source-temp",
                    ),
                ),
            ),
        }
    )
    successor_admission = verification_admission.model_copy(update={"budget": successor_budget})
    successor_path = tmp_path / "successor-admission.json"
    successor_path.write_text(successor_admission.model_dump_json())
    with (
        maintenance_resource_budget(successor_budget),
        maintenance_execution(MaintenanceExecution(successor_budget, lambda *_: None)),
        quiescent_source_cut(service, source, operation_id=successor_id) as successor,
    ):
        publish = WorkspaceV2ProgressReconciler._publish

        def interrupt_publication(
            self: WorkspaceV2ProgressReconciler, progress: ProgressReconciliation
        ) -> ReconciliationFileReference:
            interrupted_phase = (
                "SUCCESSOR_BOUND" if successor_case == "coverage_crash" else "BASELINE_VERIFIED"
            )
            if progress.reserved_successor is not None and progress.phase == interrupted_phase:
                raise RuntimeError("fixture successor publication interruption")
            return publish(self, progress)

        if successor_case == "reservation_crash":
            with monkeypatch.context() as crash:
                crash.setattr(WorkspaceV2ProgressReconciler, "_publish", interrupt_publication)
                with pytest.raises(RuntimeError, match="successor publication interruption"):
                    reserve_recovered_successor(
                        service=service,
                        destination_root=destination,
                        reconciliation_id=operation_id,
                        frozen_source=successor,
                        admission_path=successor_path,
                    )
            unpublished = ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
            assert unpublished.reserved_successor is None and unpublished.coverage is None
            assert (
                destination / f"state/progress-successor-reservation.{successor_id}.json"
            ).exists()
        reservation = reserve_recovered_successor(
            service=service,
            destination_root=destination,
            reconciliation_id=operation_id,
            frozen_source=successor,
            admission_path=successor_path,
        )
        assert reservation.reconciliation.phase == "BASELINE_VERIFIED"
        assert reservation.reconciliation.reserved_successor == successor
        assert reservation.reconciliation.coverage is None
        if successor_case == "reopened_session":
            # The first context closes below; a new identical financial cut is
            # still a different source session and cannot inherit its reservation.
            original_successor = successor
        if successor_case == "coverage_crash":
            with monkeypatch.context() as crash:
                crash.setattr(WorkspaceV2ProgressReconciler, "_publish", interrupt_publication)
                with pytest.raises(RuntimeError, match="successor publication interruption"):
                    verify_recovered_successor(
                        service=service,
                        destination_root=destination,
                        reconciliation_id=operation_id,
                        frozen_source=successor,
                        admission_path=successor_path,
                    )
            unpublished = ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
            assert unpublished.phase == "BASELINE_VERIFIED" and unpublished.coverage is None
            assert list(
                destination.glob(f"state/progress-successor-comparison.{successor_id}.*.json")
            )
        assert (
            reserve_recovered_successor(
                service=service,
                destination_root=destination,
                reconciliation_id=operation_id,
                frozen_source=successor,
                admission_path=successor_path,
            ).reconciliation
            == reservation.reconciliation
        )
        if successor_case in {"changed_w", "metadata", "missing_w", "invalid_new"}:
            with pytest.raises((WorkspaceV2MigrationError, ValueError)):
                verify_recovered_successor(
                    service=service,
                    destination_root=destination,
                    reconciliation_id=operation_id,
                    frozen_source=successor,
                    admission_path=successor_path,
                )
            assert (
                ProgressReconciliation.model_validate_json(reconciler.path.read_bytes()).phase
                == "BASELINE_VERIFIED"
            )
            return
        bound = verify_recovered_successor(
            service=service,
            destination_root=destination,
            reconciliation_id=operation_id,
            frozen_source=successor,
            admission_path=successor_path,
        )
        assert bound.stage == "successor_bound"
        assert bound.reconciliation.phase == "SUCCESSOR_BOUND"
        assert bound.reconciliation.coverage is not None
        assert bound.reconciliation.coverage.covered_metric_count == 2
        assert bound.reconciliation.coverage.covered_diagnostic_count == 1
        comparison_path = (
            destination
            / "state"
            / (
                f"progress-successor-comparison.{successor_id}."
                f"{bound.reconciliation.coverage.comparison_sha256}.json"
            )
        )
        comparison = json.loads(comparison_path.read_text())
        assert comparison["work"]["metric_count"] == 2 + len(new_metrics)
        assert comparison["work"]["fast_count"] == (2 if successor_case == "noncanonical" else 3)
        assert comparison["work"]["typed_count"] == len(new_metrics) + (
            successor_case == "noncanonical"
        )
        digest = hashlib.sha256(b"").hexdigest()
        for item in sorted(
            [metric, extra, *new_metrics], key=lambda m: (m.available_at, str(m.result_id))
        ):
            digest = hashlib.sha256(
                f"{digest}:{sha256_hex(canonical_json_bytes(item))}".encode()
            ).hexdigest()
        assert comparison["work"]["metric_digest"] == digest
        assert (
            verify_recovered_successor(
                service=service,
                destination_root=destination,
                reconciliation_id=operation_id,
                frozen_source=successor,
                admission_path=successor_path,
            ).reconciliation
            == bound.reconciliation
        )
        if isinstance(successor_case, str) and successor_case.startswith("observation_"):
            with (
                service.open_storage(
                    service.resolve(source), WorkspaceAccessMode.READ_ONLY
                ) as source_storage,
                service.open_storage(
                    service.resolve(destination), WorkspaceAccessMode.READ_WRITE
                ) as target_storage,
            ):
                arguments = dict(
                    source=source_storage,
                    destination=target_storage,
                    frozen_source=successor,
                    core_baseline=core_reference,
                    admission_path=successor_path,
                    raw_digest="0" * 64,
                    page_limit=2,
                )
                if successor_case == "observation_noncanonical":
                    result = migration_module._append_recovered_observations(**arguments)
                    assert result.imported_count == result.reused_count == 1
                    assert result.max_page_hydrated == 1
                else:
                    with pytest.raises(WorkspaceV2MigrationError, match="observation"):
                        migration_module._append_recovered_observations(**arguments)
            assert original == (
                predecessor_path.read_bytes(),
                predecessor.parent_membership_path.read_bytes(),
            )
            return
        if isinstance(successor_case, int):
            with (
                service.open_storage(
                    service.resolve(source), WorkspaceAccessMode.READ_ONLY
                ) as source_storage,
                service.open_storage(
                    service.resolve(destination), WorkspaceAccessMode.READ_WRITE
                ) as target_storage,
                monkeypatch.context() as fast,
            ):
                hydrated = []
                get_many = type(source_storage.observations).get_many

                def selected_only(self, identifiers):
                    hydrated.extend(identifiers)
                    assert observation.observation_id not in identifiers
                    return get_many(self, identifiers)

                fast.setattr(type(source_storage.observations), "get_many", selected_only)
                raw_hydrated = []
                raw_get_many = type(source_storage.raw_records).get_many

                def selected_raw_only(self, identifiers):
                    raw_hydrated.extend(identifiers)
                    assert raw.record_id not in identifiers
                    return raw_get_many(self, identifiers)

                fast.setattr(type(source_storage.raw_records), "get_many", selected_raw_only)
                raw_arguments = dict(
                    source=source_storage,
                    destination=target_storage,
                    frozen_source=successor,
                    core_baseline=core_reference,
                    admission_path=successor_path,
                    page_limit=2,
                )
                if successor_case == 3:
                    raw_writer = type(target_storage.raw_records).save_many

                    def crash_after_raw_write(self, records):
                        raw_writer(self, records)
                        raise RuntimeError("fixture raw commit interruption")

                    with monkeypatch.context() as crash:
                        crash.setattr(
                            type(target_storage.raw_records), "save_many", crash_after_raw_write
                        )
                        with pytest.raises(RuntimeError, match="raw commit interruption"):
                            migration_module._append_recovered_raw(**raw_arguments)
                    intents = list(
                        destination.glob(f"state/progress-core-append.{successor_id}.raw.*.json")
                    )
                    assert len(intents) == 1
                    intent = migration_module._RecoveredCoreAppendIntent.model_validate_json(
                        intents[0].read_bytes()
                    )
                    assert intent.core_baseline == core_reference
                    assert intent.successor == successor
                    assert len(intent.facts) == 1
                raw_result = migration_module._append_recovered_raw(**raw_arguments)
                assert set(raw_hydrated) == {item.record_id for item in new_raw_records}
                assert raw_result.imported_count == 1 + successor_case
                assert raw_result.reused_count == 1 + (successor_case == 3)
                raw_hydrated.clear()
                raw_replay = migration_module._append_recovered_raw(**raw_arguments)
                assert not raw_hydrated
                assert raw_replay.reused_count == raw_replay.imported_count
                assert raw_replay.corpus_digest == raw_result.corpus_digest
                arguments = dict(
                    source=source_storage,
                    destination=target_storage,
                    frozen_source=successor,
                    core_baseline=core_reference,
                    admission_path=successor_path,
                    raw_digest=raw_result.corpus_digest,
                    page_limit=2,
                )
                if successor_case == 3:
                    observation_writer = type(target_storage.observations).save_many

                    def crash_after_observation_write(self, records):
                        observation_writer(self, records)
                        raise RuntimeError("fixture observation commit interruption")

                    with monkeypatch.context() as crash:
                        crash.setattr(
                            type(target_storage.observations),
                            "save_many",
                            crash_after_observation_write,
                        )
                        with pytest.raises(RuntimeError, match="observation commit interruption"):
                            migration_module._append_recovered_observations(**arguments)
                    intents = list(
                        destination.glob(
                            f"state/progress-core-append.{successor_id}.observations.*.json"
                        )
                    )
                    assert len(intents) == 1
                    intent = migration_module._RecoveredCoreAppendIntent.model_validate_json(
                        intents[0].read_bytes()
                    )
                    assert len(intent.facts) == 2
                observation_result = migration_module._append_recovered_observations(**arguments)
                assert set(hydrated) == {item.observation_id for item in new_observations}
                assert observation_result.imported_count == 1 + successor_case
                assert observation_result.reused_count == 1 + 2 * (successor_case == 3)
                expected_digest = hashlib.sha256(b"").hexdigest()
                for item in sorted(
                    [observation, *new_observations],
                    key=lambda item: (item.available_at, str(item.observation_id)),
                ):
                    expected_digest = hashlib.sha256(
                        f"{expected_digest}:{sha256_hex(canonical_json_bytes(item))}".encode()
                    ).hexdigest()
                assert observation_result.corpus_digest == expected_digest
                hydrated.clear()
                replay = migration_module._append_recovered_observations(**arguments)
                assert not hydrated
                assert replay.reused_count == replay.imported_count
                assert replay.corpus_digest == expected_digest
        if successor_case == "pipeline":

            def forbid_full_epoch(_self: WorkspaceV2AnalyticalImporter) -> object:
                raise AssertionError("recovered pipeline must not hydrate the full financial epoch")

            with monkeypatch.context() as fast:
                fast.setattr(WorkspaceV2AnalyticalImporter, "_run_epoch", forbid_full_epoch)
                fast.setattr(migration_module.RawV2Importer, "run", forbid_full_epoch)
                fast.setattr(migration_module.ObservationV2Importer, "run", forbid_full_epoch)
                migrated = migrate_workspace_v1_to_v2(
                    service=service,
                    source_root=source,
                    destination_root=destination,
                    host_free_bytes=lambda: 30_000_000_000,
                    page_limit=1,
                    frozen_source=successor,
                    reconciliation_id=operation_id,
                    reconciliation_admission_path=successor_path,
                    core_baseline_path=core_reference.path,
                )
            assert migrated.analytical.metric_created_count == 1
            assert migrated.analytical.metric_reused_count == 2
            assert migrated.analytical.metric_digest == comparison["work"]["metric_digest"]
            assert historical_transition_status(destination) == "COMPLETE"
            core_comparison_path = (
                destination / "state" / (f"progress-core-comparison.{successor_id}.json")
            )
            core_comparison = migration_module._RecoveredCoreComparison.model_validate_json(
                core_comparison_path.read_bytes()
            )
            assert core_comparison.core_baseline == core_reference
            assert core_comparison.raw_count == core_comparison.observation_count == 1
            assert not core_comparison.append_intents
            assert core_comparison.raw_digest == migrated.raw.corpus_digest
            assert core_comparison.observation_digest == migrated.observations.corpus_digest
        if successor_case in {1, 3, "append_zero", "append_crash"}:
            with (
                service.open_storage(
                    service.resolve(source), WorkspaceAccessMode.READ_ONLY
                ) as source_storage,
                service.open_storage(
                    service.resolve(destination), WorkspaceAccessMode.READ_WRITE
                ) as target_storage,
            ):
                reader = DirectHistoricalAnalyticalSource(source_storage)
                target = CompactAnalyticalStore(target_storage.store.connection)
                importer = WorkspaceV2AnalyticalImporter(
                    reader,
                    target,
                    source_root=source,
                    destination_root=destination,
                    state_root=destination / "state",
                    source_workspace_id=str(successor.workspace_id),
                    source_fingerprint=successor.source_fingerprint,
                    page_limit=1,
                    direct_transport=True,
                    epoch=successor_id,
                    frozen_source=successor,
                )
                importer.bind_recovered_successor(
                    reconciliation_id=operation_id,
                    admission_path=successor_path,
                    baseline_code_root=None,
                )
                original_reader = source_storage.metric_results.get_many
                requested = []
                relationships = target.verify_historical_relationships
                relationship_calls = 0

                def count_relationships() -> None:
                    nonlocal relationship_calls
                    relationship_calls += 1
                    relationships()

                def interrupt_after_seal(event: str) -> None:
                    if event == "after_historical_seal_commit":
                        raise RuntimeError("fixture recovered seal interruption")

                def new_only(identifiers: tuple) -> dict:
                    assert set(identifiers).issubset({item.result_id for item in new_metrics})
                    requested.extend(identifiers)
                    return original_reader(identifiers)

                with monkeypatch.context() as fast:
                    fast.setattr(source_storage.metric_results, "get_many", new_only)
                    fast.setattr(target, "verify_historical_relationships", count_relationships)
                    if successor_case == "append_crash":
                        importer._failure_injector = interrupt_after_seal
                        with pytest.raises(RuntimeError, match="recovered seal interruption"):
                            importer.run()
                        importer._failure_injector = None
                    result = importer.run()
                assert relationship_calls == 1
                assert result.metric_created_count == (
                    0 if successor_case == "append_crash" else len(new_metrics)
                )
                assert result.metric_reused_count == (
                    2 + len(new_metrics) if successor_case == "append_crash" else 2
                )
                assert result.metric_digest == comparison["work"]["metric_digest"]
                assert requested == [
                    item.result_id
                    for item in sorted(
                        new_metrics, key=lambda item: (item.available_at, str(item.result_id))
                    )
                ]
                final = target.historical_seal()
                assert final is not None and final.metric_count == 2 + len(new_metrics)
                assert historical_transition_status(destination) == "COMPLETE"
                assert target_storage.store.connection.execute(
                    "SELECT count(*) FROM workspace_v2_historical_seal_history"
                ).fetchone() == (1,)
                with pytest.raises(CompactAnalyticalError, match="immutable"):
                    target.save_metrics(
                        [metric.model_copy(update={"result_id": uuid4()})], origin="HISTORICAL"
                    )
                terminal = HistoricalTransition.model_validate_json(
                    (destination / f"state/historical-transition.{successor_id}.json").read_bytes()
                )
                unrelated_id = uuid4()
                unrelated = terminal.model_copy(
                    update={
                        "operation_id": unrelated_id,
                        "phase": "PREPARED",
                        "source": terminal.source.model_copy(
                            update={
                                "cut": successor.model_copy(update={"operation_id": unrelated_id})
                            }
                        ),
                    }
                )
                (destination / f"state/historical-transition.{unrelated_id}.json").write_text(
                    unrelated.model_dump_json()
                )
                assert historical_transition_status(destination) == "APPLYING"
        if successor_case == 0:
            with (
                service.open_storage(
                    service.resolve(source), WorkspaceAccessMode.READ_ONLY
                ) as source_storage,
                service.open_storage(
                    service.resolve(destination), WorkspaceAccessMode.READ_ONLY
                ) as target_storage,
            ):
                target = CompactAnalyticalStore(target_storage.store.connection)
                importer = WorkspaceV2AnalyticalImporter(
                    DirectHistoricalAnalyticalSource(source_storage),
                    target,
                    source_root=source,
                    destination_root=destination,
                    state_root=destination / "state",
                    source_workspace_id=str(successor.workspace_id),
                    source_fingerprint=successor.source_fingerprint,
                    page_limit=1,
                    direct_transport=True,
                    epoch=successor_id,
                    frozen_source=successor,
                )
                with pytest.raises(
                    WorkspaceV2InventoryError, match="another historical transition"
                ):
                    importer._prepare_transition()
                importer.bind_recovered_successor(
                    reconciliation_id=operation_id,
                    admission_path=successor_path,
                    baseline_code_root=None,
                )

                def forbid_parent_copy(*_args: object, **_kwargs: object) -> str:
                    raise AssertionError("recovered successor must reuse original B membership")

                with monkeypatch.context() as forbid:
                    forbid.setattr(target, "write_parent_membership", forbid_parent_copy)
                    prepared_successor = importer._prepare_transition()
                assert prepared_successor is not None
                assert (
                    prepared_successor.parent_membership_path == predecessor.parent_membership_path
                )
                assert (
                    prepared_successor.parent_membership_sha256
                    == predecessor.parent_membership_sha256
                )
                assert prepared_successor.parent_seal == predecessor.parent_seal
                assert prepared_successor.operation_id == successor_id
                assert prepared_successor.phase == "PREPARED"
                foreign_id = uuid4()
                foreign_source = prepared_successor.source.model_copy(
                    update={"cut": successor.model_copy(update={"operation_id": foreign_id})}
                )
                foreign = prepared_successor.model_copy(
                    update={"operation_id": foreign_id, "source": foreign_source}
                )
                (destination / f"state/historical-transition.{foreign_id}.json").write_text(
                    foreign.model_dump_json()
                )
                with pytest.raises(
                    WorkspaceV2InventoryError, match="another historical transition"
                ):
                    importer._prepare_transition()
    if successor_case == "reopened_session":
        with (
            maintenance_resource_budget(successor_budget),
            maintenance_execution(MaintenanceExecution(successor_budget, lambda *_: None)),
            quiescent_source_cut(
                service,
                source,
                operation_id=successor_id,
                clock=lambda: original_successor.frozen_at,
            ) as reopened,
        ):
            assert reopened == original_successor
            with pytest.raises(WorkspaceV2MigrationError, match="session or code changed"):
                verify_recovered_successor(
                    service=service,
                    destination_root=destination,
                    reconciliation_id=operation_id,
                    frozen_source=reopened,
                    admission_path=successor_path,
                )
    assert original == (
        predecessor_path.read_bytes(),
        predecessor.parent_membership_path.read_bytes(),
    )


def test_append_requires_actual_source_page_and_keeps_ordinary_methods_immutable(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    raw = make_raw_record()
    observation = make_observation(raw_record_id=raw.record_id)
    metric = make_metric_result(observation_id=observation.observation_id)
    diagnostic = make_diagnostic_result(metric_result_id=metric.result_id)
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save(metric)
        storage.diagnostics.save(diagnostic)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        host_free_bytes=lambda: 30_000_000_000,
        page_limit=1,
    )
    extra = metric.model_copy(
        update={"result_id": uuid4(), "computed_at": metric.computed_at + timedelta(days=1)}
    )
    extra_diagnostic = make_diagnostic_result(metric_result_id=extra.result_id)
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.metric_results.save(extra)
        storage.diagnostics.save(extra_diagnostic)
    operation_id = uuid4()

    def stop(event: str) -> None:
        if event == "after_metric_page_write":
            raise RuntimeError("fixture interruption")

    with quiescent_source_cut(service, source, operation_id=operation_id) as cut:
        with pytest.raises(RuntimeError, match="fixture interruption"):
            migrate_workspace_v1_to_v2(
                service=service,
                source_root=source,
                destination_root=destination,
                host_free_bytes=lambda: 30_000_000_000,
                page_limit=1,
                frozen_source=cut,
                analytical_failure_injector=stop,
            )
        transition = HistoricalTransition.model_validate_json(
            (destination / f"state/historical-transition.{operation_id}.json").read_bytes()
        )
        with (
            service.open_storage(
                service.resolve(source), WorkspaceAccessMode.READ_ONLY
            ) as original,
            service.open_storage(
                service.resolve(destination), WorkspaceAccessMode.READ_WRITE
            ) as target,
        ):
            compact = CompactAnalyticalStore(target.store.connection)
            direct = DirectHistoricalAnalyticalSource(original)
            for family, rows in (("metrics", [extra]), ("diagnostics", [extra_diagnostic])):
                with pytest.raises(CompactAnalyticalError, match="active bound transition"):
                    getattr(compact, f"append_transition_{family}")(transition, rows)
            with compact.historical_transition(transition, source=direct):
                new_metric = extra.model_copy(update={"result_id": uuid4()})
                new_diagnostic = extra_diagnostic.model_copy(update={"diagnostic_id": uuid4()})
                for family, rows in (("metrics", [new_metric]), ("diagnostics", [new_diagnostic])):
                    with pytest.raises(CompactAnalyticalError, match="sealed historical"):
                        getattr(compact, f"save_{family}")(rows, origin="HISTORICAL")
                    with pytest.raises(CompactAnalyticalError, match="bound source page"):
                        getattr(compact, f"append_transition_{family}")(transition, rows)
            assert compact.historical_seal().model_dump() == transition.parent_seal.model_dump()
