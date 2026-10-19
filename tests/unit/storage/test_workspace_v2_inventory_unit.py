"""Bound transition authorization and immutable ordinary historical writes."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import investment_analyst.workspace.workspace_v2_migration as migration_module
from investment_analyst.core.models import MetricResult, NormalizedObservation, RawRecord
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
    HistoricalTransitionReceipt,
    ProgressReconciliation,
    ProgressReconciliationReceipt,
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


def _generation_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, populated: bool = False
):
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
        parent_membership=ref.model_copy(update={"role": "inventory"}),
    )
    if populated:
        raw = make_raw_record()
        observation = make_observation(raw_record_id=raw.record_id)
        first = make_metric_result(observation_id=observation.observation_id)
        second = first.model_copy(update={"result_id": uuid4(), "value": Decimal("7.00")})
        added = first.model_copy(update={"result_id": uuid4(), "value": Decimal("8.00")})
        diagnostic = make_diagnostic_result(metric_result_id=first.result_id)
        with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
            storage.raw_records.save(raw)
            storage.observations.save(observation)
            compact = CompactAnalyticalStore(storage.store.connection)
            compact.save_metrics((first, second), origin="HISTORICAL")
            compact.save_diagnostics((diagnostic,), origin="HISTORICAL")
            parent_values = compact.historical_inventory_digests()
            members = state / "original-B.jsonl"
            members.write_text(
                "".join(
                    json.dumps([family, str(identifier), sha256_hex(canonical_json_bytes(model))])
                    + "\n"
                    for family, identifier, model in (
                        ("metrics", first.result_id, first),
                        ("metrics", second.result_id, second),
                        ("diagnostics", diagnostic.diagnostic_id, diagnostic),
                    )
                )
            )
            compact.save_metrics((added,), origin="HISTORICAL")
            current_values = compact.historical_inventory_digests()
        baseline = _progress_update(
            baseline,
            parent_membership=reconciliation_file_reference(members, "inventory"),
            parent_seal=baseline.parent_seal.model_copy(
                update={
                    "metric_digest": parent_values[1],
                    "diagnostic_digest": parent_values[3],
                }
            ),
            inventory=baseline.inventory.model_copy(
                update={
                    "raw_count": 1,
                    "observation_count": 1,
                    "metric_count": 3,
                    "diagnostic_count": 1,
                    "metric_digest": current_values[1],
                    "diagnostic_digest": current_values[3],
                }
            ),
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
    generation_id = budget.operation_id
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
        parent_membership=baseline.parent_membership,
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


@pytest.mark.parametrize("crash", [None, "before_head", "after_head"])
def test_coordinator_prepares_generation_and_resumes_exact_immutable_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: str | None
) -> None:
    import importlib.util
    from types import SimpleNamespace

    from investment_analyst.storage.workspace_v2_inventory import (
        WorkspaceV2GenerationLedger,
        reconciliation_file_reference,
    )
    from investment_analyst.workspace.workspace_v2_migration_models import (
        HistoricalFrozenSource,
        HistoricalTransitionReceipt,
    )

    ledger, event, values, admission = _generation_fixture(tmp_path, monkeypatch)
    root, controls = ledger.root, tmp_path / "controls"
    controls.mkdir()
    run = controls / "run"
    run.mkdir()
    (run / "admission.json").write_text(admission.model_dump_json())
    old_admission = controls / "old-admission.json"
    old_admission.write_text(admission.model_dump_json())
    reservation = migration_module._SuccessorReservation.model_validate_json(
        event.reservation.path.read_bytes()
    )
    reservation = migration_module._SuccessorReservation.model_validate(
        {
            **reservation.model_dump(),
            "resource_receipt": reconciliation_file_reference(old_admission, "resource"),
        }
    )
    reservation_path = root / "state" / f"progress-successor-reservation.{event.attempt_id}.json"
    reservation_path.write_text(reservation.model_dump_json())
    proof = event.write_generation
    pointer = next((root / "state").glob("progress-reconciliation.*.json"))
    legacy = ProgressReconciliation.model_validate_json(pointer.read_bytes())
    legacy = _progress_update(
        legacy, predecessor=proof, parent_membership=proof.model_copy(update={"role": "inventory"})
    )
    pointer.write_bytes(canonical_json_bytes(legacy))
    baseline = ProgressReconciliation.model_validate_json(values["verified_w"].path.read_bytes())
    database = reconciliation_file_reference(root / "storage/v2/index.duckdb", "database")
    core = migration_module._RecoveredCoreBaseline(
        reconciliation=values["verified_w"],
        raw_state=proof,
        raw_terminal=proof,
        observation_state=proof,
        database=database,
        resource_receipt=reservation.resource_receipt,
        raw_count=5,
        raw_digest="a" * 64,
        raw_metadata_binding="b" * 64,
        observation_count=4,
        observation_digest="c" * 64,
        raw_verifier_sha256="d" * 64,
        verifier_module_sha256="e" * 64,
        resource_policy_sha256="f" * 64,
        verified_at=baseline.prepared_at,
    )
    core_path = controls / "core.json"
    core_path.write_text(core.model_dump_json())
    cut = legacy.reserved_successor
    transition = HistoricalTransition(
        operation_id=cut.operation_id,
        phase="COMPLETE",
        source=HistoricalFrozenSource(cut=cut),
        source_workspace_id=cut.workspace_id,
        source_created_at=cut.created_at,
        source_root=cut.source_root,
        source_fingerprint=cut.source_fingerprint,
        destination_root=root,
        destination_workspace_id=ledger.identity.workspace_id,
        parent_seal=legacy.parent_seal,
        parent_seal_sha256=sha256_hex(canonical_json_bytes(legacy.parent_seal)),
        parent_membership_path=legacy.parent_membership.path,
        parent_membership_sha256=legacy.parent_membership.sha256,
        page_limit=256,
        destination_inactive=True,
        writer_closed=True,
        tree_fingerprint="a" * 64,
        environment_fingerprint="b" * 64,
        prepared_at=baseline.prepared_at,
        updated_at=baseline.prepared_at,
    )
    transition_path = root / "state" / f"historical-transition.{cut.operation_id}.json"
    transition_path.write_text(transition.model_dump_json())
    seal = HistoricalSealSnapshot(
        source_fingerprint=cut.source_fingerprint,
        metric_count=3,
        metric_digest="c" * 64,
        diagnostic_count=2,
        diagnostic_digest="d" * 64,
        sealed_at=baseline.prepared_at,
    )
    terminal = HistoricalTransitionReceipt(
        transition=transition,
        parent_preservation_sha256=transition.parent_membership_sha256,
        final_seal=seal,
        final_seal_sha256=sha256_hex(canonical_json_bytes(seal)),
        final_inventory_digest=sha256_hex(
            json.dumps((3, "c" * 64, 2, "d" * 64), separators=(",", ":")).encode()
        ),
        metric_created_count=1,
        metric_reused_count=2,
        diagnostic_created_count=1,
        diagnostic_reused_count=1,
        completed_at=baseline.prepared_at,
    )
    terminal_path = root / "state" / f"historical-transition-receipt.{cut.operation_id}.json"
    terminal_path.write_text(terminal.model_dump_json())
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "generation_cli_under_test", repository / "scripts/close_data_chassis.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "LocalReleaseService",
        lambda: SimpleNamespace(
            load_workspace_bindings=lambda: SimpleNamespace(current=None, previous=None),
            systemctl=SimpleNamespace(
                show_properties=lambda *args: {
                    "ActiveState": "active",
                    "MainPID": "123",
                    "ExecStart": f"python --workspace {legacy.source_root}",
                }
            ),
        ),
    )
    manifest = ledger.service.manifest
    monkeypatch.setattr(
        ledger.service,
        "manifest",
        lambda path: (
            SimpleNamespace(
                workspace_id=legacy.source_workspace_id, created_at=legacy.source_created_at
            )
            if path == legacy.source_root
            else manifest(path)
        ),
    )
    args = SimpleNamespace(
        source=legacy.source_root,
        destination=root,
        controls_root=controls,
        operation_id=values["generation_id"],
        reconciliation_id=legacy.operation_id,
        resource_run_root=run,
        predecessor_proof=[proof.path],
        predecessor_transition=transition_path,
        core_baseline_path=core_path,
    )
    immutable_before = {
        p: p.read_bytes()
        for p in (
            pointer,
            reservation_path,
            transition_path,
            terminal_path,
            values["verified_w"].path,
        )
    }
    publish = WorkspaceV2GenerationLedger.publish
    crashed = False

    def interrupt(self, *args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            if crash == "after_head":
                publish(self, *args, **kwargs)
            raise RuntimeError("simulated session loss")
        return publish(self, *args, **kwargs)

    budget = admission.budget
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        if crash is not None:
            monkeypatch.setattr(WorkspaceV2GenerationLedger, "publish", interrupt)
            with pytest.raises(RuntimeError, match="session loss"):
                module._prepare_generation(args, ledger.service)
        result = module._prepare_generation(args, ledger.service)
        assert result["generation"]["phase"] == "PREPARED"
        saved = {p: p.read_bytes() for p in ledger.state.glob("cut-invalidation.*.json")}
        assert len(saved) == 2
        assert module._prepare_generation(args, ledger.service)["reused"] is True
    assert all(p.read_bytes() == content for p, content in {**immutable_before, **saved}.items())
    assert ledger.resolve()[1].generation_id == args.operation_id


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
        admission_path = tmp_path / "graph-admission.json"
        admission_path.write_text(admission.model_dump_json())
        relationships = ledger.verify_relationships(admission_path=admission_path)
        assert relationships.reconciliation == published
        assert relationships.operation_id == generation.generation_id
        assert relationships.raw_count == relationships.metric_count == 0
        relationships_path = tmp_path / "relationships.json"
        relationships_path.write_text(relationships.model_dump_json())
        assert (
            ledger.reuse_relationships(relationships_path, admission_path=admission_path)
            == relationships
        )
        for update in (
            {"operation_id": uuid4()},
            {"verifier_module_sha256": "0" * 64},
            {"destination_root": tmp_path / "other"},
            {"database": relationships.database.model_copy(update={"sha256": "0" * 64})},
        ):
            relationships_path.write_text(relationships.model_copy(update=update).model_dump_json())
            with pytest.raises((WorkspaceV2InventoryError, ValueError)):
                ledger.reuse_relationships(relationships_path, admission_path=admission_path)
        relationships_path.write_text(relationships.model_dump_json())
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


@pytest.mark.parametrize("fault", [None, "raw_count", "database", "policy", "delta_tail"])
def test_generation_baseline_requires_corrective_proofs_and_preserves_original_B(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import inspect

    from investment_analyst.storage.workspace_v2_inventory import (
        GenerationRawCoverageProof,
        reconciliation_file_reference,
        verify_generation_contents,
        verify_generation_raw_files,
    )
    from investment_analyst.workspace import workspace_v2_physical_delta as physical
    from investment_analyst.workspace.workspace_v2_migration_models import (
        GenerationContentProof,
        ProgressReconciliationV2,
    )

    ledger, event, values, admission = _generation_fixture(tmp_path, monkeypatch, populated=True)
    budget = admission.budget
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(admission.model_dump_json())
    before = {p: p.read_bytes() for p in ledger.state.glob("fixture-*.json")}
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        invalidation = ledger.invalidate(event, admission=admission)
        generation = ProgressReconciliationV2(**values, invalidation=invalidation)
        ledger.publish(generation, expected_head_sha256=None, admission=admission)
        graph = ledger.verify_relationships(admission_path=admission_path)
        graph_path = tmp_path / "relationships.json"
        graph_path.write_text(graph.model_dump_json())
        original_w = ProgressReconciliation.model_validate_json(
            generation.verified_w.path.read_bytes()
        )
        inventory = original_w.inventory
        content = GenerationContentProof(
            generation_id=generation.generation_id,
            workspace_id=ledger.identity.workspace_id,
            baseline_certificate=generation.verified_w,
            baseline_database=generation.database,
            database=generation.database,
            resource_receipt=reconciliation_file_reference(admission_path, "resource"),
            verifier_sha256=sha256_hex(inspect.getsource(verify_generation_contents).encode()),
            policy_sha256=admission.resource_policy_sha256,
            preserved_tables=(),
            raw_count=1,
            raw_digest="a" * 64,
            observation_count=1,
            observation_digest="b" * 64,
            metric_count=inventory.metric_count,
            metric_digest=inventory.metric_digest,
            diagnostic_count=inventory.diagnostic_count,
            diagnostic_digest=inventory.diagnostic_digest,
            validated_additions=(),
            verified_at=admission.recorded_at,
        )
        content_path = tmp_path / "content.json"
        content_path.write_text(content.model_dump_json())
        blob = next((ledger.root / "storage/v2/raw/raw/sha256").rglob("?" * 64))
        member = physical.PhysicalDeltaFile(
            path=blob.relative_to(ledger.root).as_posix(),
            size_bytes=blob.stat().st_size,
            sha256=blob.name,
            blocks=(
                physical.PhysicalDeltaBlock(
                    offset_bytes=0,
                    length_bytes=blob.stat().st_size,
                    sha256=blob.name,
                    origin="parent",
                ),
            ),
        )
        files = tmp_path / "files.jsonl"
        files.write_text(member.model_dump_json() + "\n")
        slot = tmp_path / "slot"
        slot.mkdir()
        header = physical.ImmutableLinkHeader(
            operation_id=generation.generation_id,
            action="LINK",
            retained_root=ledger.root,
            retained_workspace_id=ledger.identity.workspace_id,
            slot=slot,
            inventory=reconciliation_file_reference(files, "inventory"),
            parent_manifest=generation.verified_w,
            parent_index=generation.parent_membership,
            parent_proof=generation.verified_w,
            verifier_sha256=physical._immutable_link_verifier_sha256(),
            byte_budget=1_000_000,
            created_at=admission.recorded_at,
        )
        journal = physical.ImmutableLinkJournal(tmp_path / "links", header).open()
        journal.apply_page(((0, member),))
        witness = reconciliation_file_reference(
            journal.root / "page-00000000.trailer.json", "control"
        )
        raw = verify_generation_raw_files(
            ledger.service,
            destination=ledger.root,
            content_path=content_path,
            link_witness=witness,
            admission_path=admission_path,
        )
        raw_path = tmp_path / "raw-coverage.json"
        if fault == "raw_count":
            raw = raw.model_copy(update={"raw_count": 0})
        elif fault == "database":
            with generation.database.path.open("ab") as handle:
                handle.write(b"unknown bytes")
        elif fault == "policy":
            raw = raw.model_copy(update={"policy_sha256": "0" * 64})
        elif fault == "delta_tail":
            (
                ledger.state / f"generation-delta-membership.{generation.generation_id}.jsonl"
            ).write_bytes(b"[]\n")
        raw_path.write_text(raw.model_dump_json())
        assert GenerationRawCoverageProof.model_validate_json(raw_path.read_bytes()) == raw
        kwargs = dict(
            content_path=content_path,
            raw_coverage_path=raw_path,
            relationships_path=graph_path,
            admission_path=admission_path,
            code=generation.code,
        )
        if fault is not None:
            with pytest.raises(WorkspaceV2InventoryError):
                ledger.certify_baseline(**kwargs)
            assert ledger.resolve()[1].phase == "PREPARED"
        else:
            certificate = ledger.certify_baseline(**kwargs)
            head, baseline = ledger.resolve()
            assert head.certificate == certificate
            assert baseline.phase == "BASELINE_VERIFIED" and baseline.reserved_successor is None
            assert baseline.parent_membership == generation.parent_membership
            additions = baseline.inventory.delta_membership.path.read_text().splitlines()
            assert len(additions) == 1 and json.loads(additions[0])[0] == "metrics"
            assert ledger.certify_baseline(**kwargs) == certificate
        assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize(
    "fault", ["session", "destination", "active", "previous_active", "live_owner"]
)
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
    if fault in {"active", "previous_active"}:
        monkeypatch.setattr(
            local_release,
            "LocalReleaseService",
            lambda: SimpleNamespace(
                load_workspace_bindings=lambda: SimpleNamespace(
                    current=SimpleNamespace(workspace_format_version=2)
                    if fault == "active"
                    else None,
                    previous=SimpleNamespace(workspace_format_version=2)
                    if fault == "previous_active"
                    else None,
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
        "bucketed",
        "bucketed_loss",
        "derived_source",
        "source_capture",
        "missing_added_raw",
        "foreign_added_asset",
    ],
)
def test_corrective_generation_reuses_native_w_and_validates_all_added_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: int | str,
) -> None:
    from investment_analyst.storage.observation_v2 import ObservationV2Error
    from investment_analyst.storage.workspace_v2_inventory import (
        reconciliation_file_reference,
        verify_generation_contents,
    )

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root, snapshot = tmp_path / "destination", tmp_path / "snapshot-w"
    initialized = service.initialize(root, format_version=2)
    raw = make_raw_record()
    observation = make_observation(raw_record_id=raw.record_id)
    if case == "derived_source":
        raw = raw.model_copy(
            update={
                "source": raw.source.model_copy(
                    update={"source_id": "sec-edgar:institutional-holdings-semantics"}
                )
            }
        )
        observation = observation.model_copy(
            update={
                "source": observation.source.model_copy(
                    update={"source_id": "sec-edgar:institutional-holdings-observations"}
                )
            }
        )
    metric = make_metric_result(observation_id=observation.observation_id)
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        CompactAnalyticalStore(storage.store.connection).save_metrics(
            (metric,), origin="HISTORICAL"
        )
        if case in {"bucketed", "bucketed_loss"}:
            # Native-only W fixture: exercise the full comparison at a size
            # requiring bounded SQL, without hydrating old W models or files.
            storage.store.connection.execute(
                "INSERT INTO raw_v2_index SELECT r.* REPLACE "
                "(('00000000-0000-4000-8000-' || lpad(hex(i),12,'0')) AS record_id) "
                "FROM raw_v2_index r CROSS JOIN range(1,70001) t(i) WHERE record_id=?",
                [str(raw.record_id)],
            )
    shutil.copytree(root, snapshot)  # Small fixture, never the real workspace.
    database = reconciliation_file_reference(root / "storage/v2/index.duckdb", "database")
    now = datetime(2026, 10, 17, tzinfo=UTC)
    prepared = _prepared_progress(tmp_path)
    parent = prepared.parent_seal.model_copy(update={"metric_count": 0, "diagnostic_count": 0})
    inventory = RecoveredProgressInventory(
        raw_count=70_001 if case in {"bucketed", "bucketed_loss"} else 1,
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
        if case in {"missing_added_raw", "foreign_added_asset"}:
            changes = {
                "observation_id": uuid4(),
                "normalized_at": observation.normalized_at + timedelta(days=2),
            }
            if case != "missing_added_raw":
                changes["asset_id"] = "equity:us:msft"
            storage.observations.save(observation.model_copy(update=changes))
            if case == "missing_added_raw":
                # Ordinary persistence already rejects missing raw references.
                # Simulate an invalid historical image for the corrective gate.
                storage.store.connection.execute(
                    "UPDATE normalized_observations_v2 SET raw_record_id=? WHERE observation_id=?",
                    [str(uuid4()), str(changes["observation_id"])],
                )
        if case == 1:
            storage.raw_records.save(
                raw.model_copy(
                    update={
                        "record_id": uuid4(),
                        "received_at": raw.received_at + timedelta(days=2),
                        "payload": {"unicode": "acción", "exact": 184467440737095516160000},
                    }
                )
            )
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
        if case == "bucketed_loss":
            storage.store.connection.execute(
                "DELETE FROM raw_v2_index WHERE record_id='00000000-0000-4000-8000-000000000001'"
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
    source_changed = False
    expected_verifier = None
    if case == "source_capture":
        import inspect

        original_source = inspect.getsource
        verifier_source = original_source(verify_generation_contents)
        expected_verifier = sha256_hex(verifier_source.encode())

        def changing_source(value):
            if value is verify_generation_contents and source_changed:
                return "def another_function_after_source_lines_moved(): pass\n"
            return original_source(value)

        monkeypatch.setattr(inspect, "getsource", changing_source)

    def only_additions(self, identifiers):
        nonlocal source_changed
        source_changed = True
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
            "bucketed_loss",
            "missing_added_raw",
            "foreign_added_asset",
        }:
            with pytest.raises(
                (WorkspaceV2InventoryError, CompactAnalyticalError, ObservationV2Error)
            ):
                verify_generation_contents(service, **arguments)
        else:
            result = verify_generation_contents(service, **arguments)
            if case == "source_capture":
                assert source_changed and result.verifier_sha256 == expected_verifier
            assert result.metric_count == 1 + count
            assert dict(result.validated_additions)["metrics"] == count
            assert set(seen) == set(new)
            assert result.comparison_scope == "preserved-W-subset-current-destination"
            if case in {0, 1, 3}:
                from investment_analyst.storage.workspace_v2_inventory import (
                    verify_generation_raw_files,
                )
                from investment_analyst.workspace import workspace_v2_physical_delta as physical

                content_path = tmp_path / "content-proof.json"
                content_path.write_text(result.model_dump_json())
                raw_sha = sha256_hex(canonical_json_bytes(raw))
                blob = root / f"storage/v2/raw/raw/sha256/{raw_sha[:2]}/{raw_sha[2:4]}/{raw_sha}"
                relative = blob.relative_to(root).as_posix()
                member = physical.PhysicalDeltaFile(
                    path=relative,
                    size_bytes=blob.stat().st_size,
                    sha256=blob.name,
                    blocks=(
                        physical.PhysicalDeltaBlock(
                            offset_bytes=0,
                            length_bytes=blob.stat().st_size,
                            sha256=blob.name,
                            origin="parent",
                        ),
                    ),
                )
                files = tmp_path / "raw-files.jsonl"
                files.write_text(member.model_dump_json() + "\n")
                header = physical.ImmutableLinkHeader(
                    operation_id=operation,
                    action="LINK",
                    retained_root=root,
                    retained_workspace_id=initialized.manifest.workspace_id,
                    slot=snapshot,
                    inventory=reconciliation_file_reference(files, "inventory"),
                    parent_manifest=reconciliation_file_reference(certificate, "control"),
                    parent_index=reconciliation_file_reference(files, "inventory"),
                    parent_proof=reconciliation_file_reference(certificate, "control"),
                    verifier_sha256=physical._immutable_link_verifier_sha256(),
                    byte_budget=1_000_000,
                    created_at=now,
                )
                (snapshot / relative).unlink()  # Replace only the tiny fixture copy.
                journal = physical.ImmutableLinkJournal(tmp_path / "links", header).open()
                journal.apply_page(((0, member),))
                witness = reconciliation_file_reference(
                    journal.root / "page-00000000.trailer.json", "control"
                )
                kwargs = dict(
                    destination=root,
                    content_path=content_path,
                    link_witness=witness,
                    admission_path=admission_path,
                )
                covered = verify_generation_raw_files(service, **kwargs)
                assert covered.witnessed_files == 1
                assert covered.raw_count == result.raw_count
                assert covered.hashed_files == (1 if case == 1 else 0)
                if case == 0:
                    extra = blob.with_name("f" * 64)
                    extra.write_bytes(b"unindexed bytes")
                elif case == 1:
                    blob.write_bytes(b"unknown changed payload")
                else:
                    blob.unlink()
                with pytest.raises(
                    (WorkspaceV2InventoryError, physical.WorkspaceV2PhysicalDeltaError)
                ):
                    verify_generation_raw_files(service, **kwargs)


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


@dataclass(frozen=True)
class _RecoveredBaselineFixture:
    service: WorkspaceService
    source: Path
    destination: Path
    raw: RawRecord
    observation: NormalizedObservation
    metric: MetricResult
    extra: MetricResult
    operation_id: UUID
    reconciler: WorkspaceV2ProgressReconciler
    baseline: ProgressReconciliationReceipt
    core_reference: ReconciliationFileReference | None
    verification_admission: MaintenanceResourceReceipt
    verification_budget: MaintenanceResourceBudget
    original: tuple[bytes, bytes]
    predecessor: HistoricalTransition
    predecessor_path: Path


def _recovered_baseline_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    financial_fault: str | None = None,
    successor_case: int | str = 0,
) -> _RecoveredBaselineFixture | None:
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

    core_reference = None
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
    return _RecoveredBaselineFixture(
        service=service,
        source=source,
        destination=destination,
        raw=raw,
        observation=observation,
        metric=metric,
        extra=extra,
        operation_id=operation_id,
        reconciler=reconciler,
        baseline=baseline,
        core_reference=core_reference,
        verification_admission=verification_admission,
        verification_budget=verification_budget,
        original=original,
        predecessor=predecessor,
        predecessor_path=predecessor_path,
    )


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
    fixture = _recovered_baseline_fixture(tmp_path, monkeypatch, financial_fault, successor_case)
    if fixture is None:
        return
    service = fixture.service
    source = fixture.source
    destination = fixture.destination
    raw = fixture.raw
    observation = fixture.observation
    metric = fixture.metric
    extra = fixture.extra
    operation_id = fixture.operation_id
    reconciler = fixture.reconciler
    baseline = fixture.baseline
    core_reference = fixture.core_reference
    verification_admission = fixture.verification_admission
    verification_budget = fixture.verification_budget
    original = fixture.original
    predecessor = fixture.predecessor
    predecessor_path = fixture.predecessor_path
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


@pytest.mark.parametrize("fault", [None, "foreign_baseline", "foreign_session"])
def test_invalidation_covers_reservation_durable_before_head_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    from investment_analyst.workspace.workspace_v2_migration_models import (
        ReconciliationGenerationReference,
    )

    ledger, event, values, admission = _generation_fixture(tmp_path, monkeypatch)
    reservation = migration_module._SuccessorReservation.model_validate_json(
        event.reservation.path.read_bytes()
    )
    pending = event.model_copy(
        update={
            "certificate": ReconciliationGenerationReference(
                schema_type="workspace-v2-progress-reconciliation-v1",
                artifact=reservation.baseline,
            ),
            "reason": "SESSION_ENDED",
        }
    )
    if fault == "foreign_baseline":
        baseline = ProgressReconciliation.model_validate_json(
            reservation.baseline.path.read_bytes()
        )
        changed = _progress_update(baseline, source_workspace_id=uuid4())
        other = tmp_path / "different-baseline.json"
        other.write_bytes(canonical_json_bytes(changed))
        from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference

        pending = pending.model_copy(
            update={
                "certificate": pending.certificate.model_copy(
                    update={"artifact": reconciliation_file_reference(other, "control")}
                )
            }
        )
    elif fault == "foreign_session":
        pending = pending.model_copy(
            update={"owner": pending.owner.model_copy(update={"source_session": uuid4()})}
        )
    immutable = {p: p.read_bytes() for p in (event.reservation.path, reservation.baseline.path)}
    with (
        maintenance_resource_budget(admission.budget),
        maintenance_execution(MaintenanceExecution(admission.budget, lambda *_: None)),
    ):
        if fault:
            with pytest.raises(WorkspaceV2InventoryError):
                ledger.invalidate(pending, admission=admission)
        else:
            reference = ledger.invalidate(pending, admission=admission)
            assert reference.path.is_file()
            assert ledger.resolve() is None
            assert ledger.attempt_invalidated(pending.attempt_id)
    assert all(p.read_bytes() == data for p, data in immutable.items())


@pytest.mark.parametrize(
    "fault", [None, "foreign_session", "foreign_admission", "foreign_baseline"]
)
@pytest.mark.parametrize("next_generation", [False, True])
def test_generational_pending_reservation_can_be_invalidated_without_rebinding_dead_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None, next_generation: bool
) -> None:
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutOwner,
        ProgressReconciliationV2,
        ReconciliationGenerationReference,
    )

    ledger, event, values, admission = _generation_fixture(tmp_path, monkeypatch)
    legacy = ProgressReconciliation.model_validate_json(values["verified_w"].path.read_bytes())
    with (
        maintenance_resource_budget(admission.budget),
        maintenance_execution(MaintenanceExecution(admission.budget, lambda *_: None)),
    ):
        invalidation = ledger.invalidate(event, admission=admission)
        prepared = ProgressReconciliationV2(**values, invalidation=invalidation)
        prepared_ref = ledger.publish(prepared, expected_head_sha256=None, admission=admission)
        generation = ProgressReconciliationV2.model_validate(
            {
                **prepared.model_dump(),
                "phase": "BASELINE_VERIFIED",
                "previous_certificate": prepared_ref,
                "inventory": legacy.inventory,
                "core_proof": values["verified_w"],
                "corrective_proofs": (values["verified_w"],),
            }
        )
        certificate = ledger.publish(
            generation, expected_head_sha256=ledger._head_digest(), admission=admission
        )
        head_before = ledger.head_path.read_bytes()
        attempt = uuid4()
        cut = migration_module._SuccessorReservation.model_validate_json(
            event.reservation.path.read_bytes()
        ).successor.model_copy(update={"operation_id": attempt})
        old_admission = admission.model_copy(
            update={
                "budget": admission.budget.model_copy(update={"operation_id": attempt}),
                "pid": 999_999_999,
            }
        )
        old_admission_path = tmp_path / "old-owner-admission.json"
        old_admission_path.write_text(old_admission.model_dump_json())
        owner = CutOwner(
            source_session=uuid4(),
            pid=old_admission.pid,
            start_ticks=old_admission.process_start_ticks,
            boot_id=old_admission.boot_id,
            cgroup=old_admission.cgroup_path,
        )
        reservation = migration_module._SuccessorReservation(
            reconciliation_id=generation.generation_id,
            successor=cut,
            source_session=owner.source_session,
            baseline=certificate,
            baseline_dataset_sha256=generation.inventory.dataset_sha256,
            module_sha256="a" * 64,
            resource_receipt=reconciliation_file_reference(old_admission_path, "resource"),
            recorded_at=generation.recorded_at,
        )
        path = ledger.state / f"progress-successor-reservation.{attempt}.json"
        path.write_text(reservation.model_dump_json())
        pending = event.model_copy(
            update={
                "event_id": uuid4(),
                "generation_id": uuid4() if next_generation else generation.generation_id,
                "attempt_id": attempt,
                "expected_head_sha256": ledger._head_digest(),
                "certificate": ReconciliationGenerationReference(
                    schema_type="workspace-v2-progress-reconciliation-v2", artifact=certificate
                ),
                "reservation": reconciliation_file_reference(path, "control"),
                "owner": owner,
                "reason": "SESSION_ENDED",
            }
        )
        if fault == "foreign_session":
            pending = pending.model_copy(
                update={"owner": owner.model_copy(update={"source_session": uuid4()})}
            )
        elif fault == "foreign_admission":
            pending = pending.model_copy(
                update={"owner": owner.model_copy(update={"pid": 999_999_998})}
            )
        elif fault == "foreign_baseline":
            pending = pending.model_copy(
                update={
                    "certificate": pending.certificate.model_copy(update={"artifact": prepared_ref})
                }
            )
        immutable = {p: p.read_bytes() for p in (path, certificate.path, prepared_ref.path)}
        if fault:
            with pytest.raises(WorkspaceV2InventoryError):
                ledger.invalidate(pending, admission=admission)
        else:
            saved = ledger.invalidate(pending, admission=admission)
            assert ledger.invalidate(pending, admission=admission) == saved
            assert ledger.attempt_invalidated(attempt)
        assert ledger.head_path.read_bytes() == head_before
        assert all(p.read_bytes() == data for p, data in immutable.items())


@pytest.mark.parametrize("phase", ["PREPARED", "BASELINE_VERIFIED", "SUCCESSOR_BOUND"])
def test_generation_status_cannot_reuse_legacy_complete_without_its_own_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    from types import SimpleNamespace

    from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2GenerationLedger

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root = tmp_path / "empty-v2"
    service.initialize(root, format_version=2)
    generation = SimpleNamespace(
        phase=phase, reserved_successor=SimpleNamespace(operation_id=uuid4())
    )
    monkeypatch.setattr(WorkspaceV2GenerationLedger, "resolve", lambda self: (object(), generation))
    monkeypatch.setattr(WorkspaceV2GenerationLedger, "attempt_invalidated", lambda *_: False)
    assert historical_transition_status(root) == "APPLYING"


@pytest.mark.parametrize("fault", [None, "owner", "v2", "admission"])
def test_coordinator_invalidates_orphan_without_changing_legacy_reservation_or_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import importlib.util
    from types import SimpleNamespace

    from investment_analyst.storage.resource_policy import ResourcePolicyError
    from investment_analyst.storage.workspace_v2_inventory import (
        WorkspaceV2GenerationLedger,
        reconciliation_file_reference,
    )
    from investment_analyst.workspace.workspace_v2_migration_models import CutInvalidation

    ledger, event, values, admission = _generation_fixture(tmp_path, monkeypatch)
    reservation = migration_module._SuccessorReservation.model_validate_json(
        event.reservation.path.read_bytes()
    )
    controls = tmp_path / "controls"
    controls.mkdir()
    run = controls / "run"
    run.mkdir()
    old = admission.model_copy(update={"pid": 99999999})
    old_path = controls / "old-admission.json"
    old_path.write_text(old.model_dump_json())
    reservation = reservation.model_copy(
        update={"resource_receipt": reconciliation_file_reference(old_path, "resource")}
    )
    reservation_path = ledger.state / f"progress-successor-reservation.{event.attempt_id}.json"
    reservation_path.write_text(reservation.model_dump_json())
    budget = admission.budget.model_copy(update={"operation_id": reservation.reconciliation_id})
    current = admission.model_copy(update={"budget": budget})
    (run / "admission.json").write_text(
        (admission if fault == "admission" else current).model_dump_json()
    )
    head = SimpleNamespace(model_dump=lambda **kw: {"certificate": "existing immutable head"})
    generation = SimpleNamespace(
        generation_id=values["generation_id"],
        phase="BASELINE_VERIFIED",
        reserved_successor=None,
        verified_w=reservation.baseline,
    )
    monkeypatch.setattr(WorkspaceV2GenerationLedger, "resolve", lambda self: (head, generation))
    original_manifest = ledger.service.manifest
    monkeypatch.setattr(
        ledger.service,
        "manifest",
        lambda path: (
            SimpleNamespace(
                workspace_id=event.source_workspace_id, created_at=reservation.successor.created_at
            )
            if path == event.source_root
            else original_manifest(path)
        ),
    )
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "orphan_cli", repository / "scripts/close_data_chassis.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "WorkspaceService", lambda: ledger.service)
    monkeypatch.setattr(
        module,
        "LocalReleaseService",
        lambda: SimpleNamespace(
            load_workspace_bindings=lambda: SimpleNamespace(
                current=SimpleNamespace(workspace_format_version=2) if fault == "v2" else None,
                previous=None,
            ),
            systemctl=SimpleNamespace(
                show_properties=lambda *a: {
                    "ActiveState": "active",
                    "MainPID": "42",
                    "ExecStart": f"python --workspace {event.source_root}",
                }
            ),
        ),
    )
    if fault == "owner":
        boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        old = old.model_copy(update={"boot_id": boot})
        old_path.write_text(old.model_dump_json())
        reservation = reservation.model_copy(
            update={"resource_receipt": reconciliation_file_reference(old_path, "resource")}
        )
        reservation_path.write_text(reservation.model_dump_json())
        monkeypatch.setattr(
            module,
            "_process_identity",
            lambda pid: (old.process_start_ticks, boot, old.cgroup_path),
        )
    args = SimpleNamespace(
        phase="invalidate-reservation",
        source=event.source_root,
        destination=ledger.root,
        controls_root=controls,
        operation_id=reservation.reconciliation_id,
        reconciliation_id=values["generation_id"],
        reservation_path=reservation_path,
        resource_run_root=run,
    )
    original = {p: p.read_bytes() for p in (reservation_path, reservation.baseline.path)}
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        if fault:
            with pytest.raises((module.MaintenancePhaseError, ResourcePolicyError, ValueError)):
                module._execute(args)
            assert not list(ledger.state.glob("cut-invalidation.*.json"))
        else:
            result = module._execute(args)
            invalidation = CutInvalidation.model_validate_json(
                Path(result["invalidation"]["path"]).read_bytes()
            )
            assert (
                invalidation.reason == "SESSION_ENDED"
                and invalidation.attempt_id == event.attempt_id
            )
            assert module._execute(args) == result
    assert all(p.read_bytes() == data for p, data in original.items())
    assert not ledger.head_path.exists()


def _certified_successor_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real small W→completed prior attempt→C1/C2 baseline; no proof producers mocked."""
    from investment_analyst.application.local_release import (
        DeploymentWorkspaceBindings,
        LocalReleaseService,
    )
    from investment_analyst.storage.workspace_v2_inventory import (
        WorkspaceV2GenerationLedger,
        historical_transition_fingerprints,
        reconciliation_file_reference,
        verify_generation_contents,
        verify_generation_raw_files,
    )
    from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutInvalidation,
        CutOwner,
        ProgressReconciliationV2,
        ReconciliationGenerationReference,
    )
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        ImmutableLinkHeader,
        ImmutableLinkJournal,
        PhysicalDeltaBlock,
        PhysicalDeltaFile,
        WorkspaceV2PhysicalDeltaIndexService,
        _immutable_link_verifier_sha256,
    )
    from investment_analyst.workspace.workspace_v2_remote import WorkspaceV2RemoteService

    seed = _recovered_baseline_fixture(tmp_path, monkeypatch)
    assert seed is not None and seed.core_reference is not None
    service, source, destination = seed.service, seed.source, seed.destination
    snapshot = tmp_path / "independent-W"
    shutil.copytree(destination, snapshot)  # Small fixture, never any real workspace.
    # Finish the historical attempt in a separate actual source session/process.
    # Its recorded PID really disappears before the C1 invalidation below.
    previous = uuid4()
    child_input = tmp_path / "child-input.json"
    child_input.write_text(
        json.dumps(
            {
                "source": str(source),
                "destination": str(destination),
                "home": str(tmp_path / "home"),
                "operation": str(previous),
                "reconciliation": str(seed.operation_id),
                "core": str(seed.core_reference.path),
                "budget": seed.verification_budget.model_dump(mode="json"),
                "admission": seed.verification_admission.model_dump(mode="json"),
            }
        )
    )
    script = """
import json,os,sys
from pathlib import Path
from uuid import UUID
from datetime import UTC,datetime
from investment_analyst.storage.resource_policy import *
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import (
 quiescent_source_cut,migrate_workspace_v1_to_v2,
)
v=json.loads(Path(sys.argv[1]).read_text())
source=Path(v['source']); destination=Path(v['destination'])
b=MaintenanceResourceBudget.model_validate_json(json.dumps(v['budget']))
a=DatabaseResourceAssignment(database=source/'storage/data/processed/investment_analyst.duckdb',policy=DuckDBResourcePolicy(memory_limit_bytes=256000000,threads=1,spill_limit_bytes=0,temp_directory=source.parent/'child-spill'))
b=b.model_copy(update={'operation_id':UUID(v['operation']),'assignments':(*b.assignments,a)})
r=MaintenanceResourceReceipt.model_validate_json(json.dumps(v['admission']))
ticks=int(Path('/proc/self/stat').read_text().split()[21]);boot=UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip())
r=r.model_copy(update={'budget':b,'pid':os.getpid(),'process_start_ticks':ticks,'boot_id':boot,'recorded_at':datetime.now(UTC)})
p=source.parent/'previous-admission.json';p.write_text(r.model_dump_json())
s=WorkspaceService(environ={},home=Path(v['home']))
with (
 maintenance_resource_budget(b),
 maintenance_execution(MaintenanceExecution(b,lambda *_:None)),
 quiescent_source_cut(s,source,operation_id=UUID(v['operation'])) as cut,
):
 (source.parent/'previous-cut.json').write_text(cut.model_dump_json())
 migrate_workspace_v1_to_v2(service=s,source_root=source,destination_root=destination,page_limit=1,host_free_bytes=lambda:30000000000,frozen_source=cut,reconciliation_id=UUID(v['reconciliation']),core_baseline_path=Path(v['core']),reconciliation_admission_path=p)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(child_input)], capture_output=True, text=True, timeout=30
    )
    assert child.returncode == 0, child.stderr[-4000:]
    old_admission = MaintenanceResourceReceipt.model_validate_json(
        (tmp_path / "previous-admission.json").read_bytes()
    )
    previous_cut = FrozenSourceCut.model_validate_json(
        (tmp_path / "previous-cut.json").read_bytes()
    )
    previous_reservation = destination / "state" / f"progress-successor-reservation.{previous}.json"
    reserved = migration_module._SuccessorReservation.model_validate_json(
        previous_reservation.read_bytes()
    )
    previous_receipt = destination / "state" / f"historical-transition-receipt.{previous}.json"
    terminal = HistoricalTransitionReceipt.model_validate_json(previous_receipt.read_bytes())
    assert terminal.transition.phase == "COMPLETE"
    assert not (Path("/proc") / str(old_admission.pid)).exists()

    backup = WorkspaceV2BackupService(service)
    packed = tmp_path / "parent-packed"
    backup.create_packed(
        destination,
        packed,
        volume_bytes=1_048_576,
        fragment_bytes=65_536,
        available_bytes=lambda: 30_000_000_000,
    )
    parent_root = tmp_path / "parent-restored"
    backup.restore_packed(packed, parent_root, available_bytes=lambda: 30_000_000_000)
    index_root = tmp_path / "parent-index"
    WorkspaceV2PhysicalDeltaIndexService(backup).build(packed, index_root)
    remote = (
        WorkspaceV2RemoteService(backup)
        .plan_base(packed, configuration_fingerprint="a" * 64)
        .manifest
    )
    parent_manifest = tmp_path / "parent-remote-manifest.json"
    parent_manifest.write_text(remote.model_dump_json())

    generation_id = uuid4()
    assignments = (
        seed.verification_budget.assignments[0],
        DatabaseResourceAssignment(
            database=snapshot / "storage/v2/index.duckdb",
            policy=DuckDBResourcePolicy(
                memory_limit_bytes=256_000_000,
                threads=1,
                spill_limit_bytes=0,
                temp_directory=tmp_path / "baseline-spill",
            ),
        ),
    )
    budget = seed.verification_budget.model_copy(
        update={"operation_id": generation_id, "assignments": assignments}
    )
    policy = sha256_hex(
        (Path(migration_module.__file__).parent.parent / "storage/resource_policy.py").read_bytes()
    )
    admission = seed.verification_admission.model_copy(
        update={"budget": budget, "resource_policy_sha256": policy}
    )
    admission_path = tmp_path / "generation-admission.json"
    admission_path.write_text(admission.model_dump_json())
    ledger = WorkspaceV2GenerationLedger(service, destination)
    legacy = ProgressReconciliation.model_validate_json(seed.reconciler.path.read_bytes())
    previous_ref = reconciliation_file_reference(seed.reconciler.path, "control")
    old = ReconciliationGenerationReference(
        schema_type=legacy.schema_version, artifact=previous_ref
    )
    proof_path = tmp_path / "session-ended-evidence.json"
    proof_path.write_text(
        json.dumps(
            {
                "pid": old_admission.pid,
                "owner_absent": True,
                "source_session_closed": True,
                "attempt": str(previous),
                "prior_exit": child.returncode,
            }
        )
    )
    proof = reconciliation_file_reference(proof_path, "control")
    event = CutInvalidation(
        event_id=uuid4(),
        generation_id=generation_id,
        attempt_id=previous,
        certificate=old,
        reservation=reconciliation_file_reference(previous_reservation, "control"),
        owner=CutOwner(
            source_session=reserved.source_session,
            pid=old_admission.pid,
            start_ticks=old_admission.process_start_ticks,
            boot_id=old_admission.boot_id,
            cgroup=old_admission.cgroup_path,
        ),
        source_root=source,
        source_workspace_id=previous_cut.workspace_id,
        destination_root=destination,
        destination_workspace_id=ledger.identity.workspace_id,
        database=reconciliation_file_reference(destination / "storage/v2/index.duckdb", "database"),
        wal=None,
        write_generation=reconciliation_file_reference(previous_receipt, "control"),
        historical_seal=reconciliation_file_reference(previous_receipt, "control"),
        historical_receipt=reconciliation_file_reference(previous_receipt, "control"),
        reason="SESSION_ENDED",
        owner_absence=proof,
        exclusivity=proof,
        no_activation=proof,
        code_sha256=sha256_hex(Path(migration_module.__file__).read_bytes()),
        policy_sha256=policy,
        recorded_at=datetime.now(UTC),
    )
    # Only the OS/deployment boundary is simulated. All data/proof production is real.
    monkeypatch.setattr(
        LocalReleaseService, "load_workspace_bindings", lambda self: DeploymentWorkspaceBindings()
    )
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        invalidation = ledger.invalidate(event, admission=admission)
        tree, environment = historical_transition_fingerprints(256, "shared-reference-verifier-v1")
        generation = ProgressReconciliationV2(
            generation_id=generation_id,
            ordinal=1,
            previous_generation=old,
            invalidation=invalidation,
            source_root=source,
            source_workspace_id=previous_cut.workspace_id,
            source_created_at=previous_cut.created_at,
            destination_root=destination,
            destination_workspace_id=ledger.identity.workspace_id,
            predecessor=legacy.predecessor,
            previous_attempt=reconciliation_file_reference(
                destination / "state" / f"historical-transition.{previous}.json", "control"
            ),
            previous_reservation=event.reservation,
            previous_receipts=(event.historical_receipt,),
            parent_membership=legacy.parent_membership,
            parent_seal=legacy.parent_seal,
            verified_w=seed.baseline.certificate,
            database=event.database,
            wal=None,
            code=(reconciliation_file_reference(Path(migration_module.__file__), "code"),),
            tree_fingerprint=tree,
            environment_fingerprint=environment,
            resource_receipt=reconciliation_file_reference(admission_path, "resource"),
            prepared_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
        ledger.publish(generation, expected_head_sha256=None, admission=admission)
        relationships = ledger.verify_relationships(admission_path=admission_path)
        graph = tmp_path / "generation-relationships.json"
        graph.write_text(relationships.model_dump_json())
        content = verify_generation_contents(
            service,
            generation_id=generation_id,
            destination=destination,
            baseline_root=snapshot,
            baseline_certificate=seed.baseline.certificate,
            baseline_database_sha256=seed.baseline.reconciliation.database_after.sha256,
            admission_path=admission_path,
            relationships_path=graph,
        )
        content_path = tmp_path / "generation-content.json"
        content_path.write_text(content.model_dump_json())
        blob = next((destination / "storage/v2/raw/raw/sha256").rglob("?" * 64))
        member = PhysicalDeltaFile(
            path=blob.relative_to(destination).as_posix(),
            size_bytes=blob.stat().st_size,
            sha256=blob.name,
            blocks=(
                PhysicalDeltaBlock(
                    offset_bytes=0,
                    length_bytes=blob.stat().st_size,
                    sha256=blob.name,
                    origin="parent",
                ),
            ),
        )
        files = tmp_path / "raw-members.jsonl"
        files.write_text(member.model_dump_json() + "\n")
        link_slot = tmp_path / "raw-witness-slot"
        link_slot.mkdir()
        header = ImmutableLinkHeader(
            operation_id=generation_id,
            action="LINK",
            retained_root=destination,
            retained_workspace_id=ledger.identity.workspace_id,
            slot=link_slot,
            inventory=reconciliation_file_reference(files, "inventory"),
            parent_manifest=reconciliation_file_reference(parent_manifest, "control"),
            parent_index=reconciliation_file_reference(
                index_root / "physical-delta-index.jsonl", "inventory"
            ),
            parent_proof=reconciliation_file_reference(previous_receipt, "control"),
            verifier_sha256=_immutable_link_verifier_sha256(),
            byte_budget=1_000_000,
            created_at=datetime.now(UTC),
        )
        journal = ImmutableLinkJournal(tmp_path / "raw-witness", header).open()
        journal.apply_page(((0, member),))
        witness = reconciliation_file_reference(
            journal.root / "page-00000000.trailer.json", "control"
        )
        raw = verify_generation_raw_files(
            service,
            destination=destination,
            content_path=content_path,
            link_witness=witness,
            admission_path=admission_path,
        )
        raw_path = tmp_path / "generation-raw.json"
        raw_path.write_text(raw.model_dump_json())
        ledger.certify_baseline(
            content_path=content_path,
            raw_coverage_path=raw_path,
            relationships_path=graph,
            admission_path=admission_path,
            code=generation.code,
        )
    return (
        seed,
        ledger,
        admission,
        parent_root,
        index_root / "physical-delta-index.jsonl",
        parent_manifest,
    )


@pytest.mark.parametrize("fault", [None, "epoch", "root"])
def test_coverage_only_measures_retained_generation_without_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
):
    import importlib.util

    from investment_analyst.workspace.workspace_v2_physical_delta import (
        WorkspaceV2PhysicalDeltaService,
    )

    seed, ledger, admission, _, index, parent = _certified_successor_fixture(tmp_path, monkeypatch)
    generation = ledger.resolve()[1].generation_id
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "coverage_closure", scripts / "close_data_chassis.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    controls = tmp_path / "coverage-controls"
    controls.mkdir()
    operation = uuid4() if fault == "epoch" else generation
    budget = admission.budget.model_copy(
        update={
            "operation_id": operation,
            "assignments": tuple(
                item.model_copy(
                    update={
                        "policy": item.policy.model_copy(
                            update={
                                "temp_directory": controls
                                / "resource-temporaries"
                                / str(operation)
                                / str(i)
                            }
                        )
                    }
                )
                for i, item in enumerate(admission.budget.assignments)
                if item.database == seed.destination / "storage/v2/index.duckdb"
            ),
        }
    )
    for item in budget.assignments:
        item.policy.temp_directory.mkdir(parents=True)
    budget_path = controls / "budget.json"
    budget_path.write_text(budget.model_dump_json())
    args = module._parser().parse_args(
        [
            "recovery-readback",
            "--coverage-only",
            "--source",
            str(seed.source),
            "--destination",
            str(seed.destination),
            "--controls-root",
            str(controls),
            "--operation-id",
            str(operation),
            "--code-sha",
            "a" * 40,
            "--delta-root",
            str(tmp_path / "coverage-delta"),
            "--resource-budget",
            str(budget_path),
            "--recovery-root",
            str(seed.destination if fault != "root" else tmp_path / "absent-slot"),
        ]
    )
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        manifest = WorkspaceV2PhysicalDeltaService(seed.service).plan(
            seed.destination,
            index,
            args.delta_root,
            parent_manifest_sha256=hashlib.sha256(parent.read_bytes()).hexdigest(),
            epoch=operation,
            allow_incomplete_historical=True,
        )
        before = (seed.destination / "storage/v2/index.duckdb").read_bytes()
        if fault is not None:
            with pytest.raises(
                module.MaintenancePhaseError, match="coverage_measurement_requires_retained_w2"
            ):
                module._execute(args)
        else:
            result = module._execute(args)
            assert result["coverage_only"] and result["new_restore"] is False
            assert result["coverage"]["physical_files"] == manifest.file_count
            assert result["coverage"]["raw_files"] > 0
            assert result["coverage"]["raw_files"] == result["coverage"]["raw_indexed"]
        assert (seed.destination / "storage/v2/index.duckdb").read_bytes() == before


@pytest.mark.parametrize("family", ["metrics", "diagnostics"])
def test_successor_typed_fallback_batches_models_by_bytes_and_preserves_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str
) -> None:
    from investment_analyst.storage.resource_policy import connect_duckdb

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace = service.initialize(tmp_path / "source", format_version=1)
    models = [
        make_metric_result(observation_id=uuid4()).model_copy(
            update={"parameters": {"payload": "雪" * 6000}}
        )
        if family == "metrics"
        else make_diagnostic_result(metric_result_id=uuid4())
        for _ in range(256)
    ]
    with service.open_storage(workspace.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        repository = storage.metric_results if family == "metrics" else storage.diagnostics
        repository.save_many(models)
    database = workspace.paths.root / "storage/data/processed/investment_analyst.duckdb"
    identifiers = [
        model.result_id if family == "metrics" else model.diagnostic_id for model in models
    ]
    sizes = {
        identifier: len(canonical_json_bytes(model))
        for identifier, model in zip(identifiers, models, strict=True)
    }
    rows = [
        (str(identifier), model.available_at.isoformat(), "unused", None, False, sizes[identifier])
        for identifier, model in zip(identifiers, models, strict=True)
    ]
    original = migration_module.execute_bounded
    batches = []

    def observe(connection, query, parameters=None):
        if "document_json," in query and " IN (" in query:
            requested = [UUID(value) for value in parameters[:-2]]
            assert len(requested) <= 256
            assert sum(sizes[identifier] for identifier in requested) <= 1 << 20
            assert "available_at BETWEEN" in query
            batches.append(len(requested))
        return original(connection, query, parameters)

    monkeypatch.setattr(migration_module, "execute_bounded", observe)
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(),
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=database,
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=256_000_000,
                    threads=1,
                    spill_limit_bytes=0,
                    temp_directory=tmp_path / "typed-batches-temp",
                ),
            ),
        ),
    )
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
        connect_duckdb(database, read_only=True) as connection,
    ):
        result = migration_module._validate_successor_comparison_rows(connection, family, rows)
        assert result == {
            identifier: sha256_hex(canonical_json_bytes(model))
            for identifier, model in zip(identifiers, models, strict=True)
        }
        assert sum(batches) == 256
        assert (1 < len(batches) < 256) if family == "metrics" else len(batches) == 1
        with pytest.raises(migration_module.WorkspaceV2MigrationError, match="page exceeds bounds"):
            migration_module._validate_successor_comparison_rows(
                connection, family, rows + rows[:1]
            )


@pytest.mark.parametrize(
    "additions,fault",
    [
        (0, None),
        (1, None),
        (3, None),
        (0, "wal"),
        (0, "changed"),
        (0, "generation"),
        (0, "noncanonical"),
        (0, "bad_metadata"),
    ],
)
def test_comparison_measurement_is_read_only_unreserved_and_bound_to_current_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, additions: int, fault: str | None
) -> None:
    import os

    from investment_analyst.workspace import workspace_v2_migration as migration

    seed, ledger, admission, *_ = _certified_successor_fixture(tmp_path, monkeypatch)
    baseline = ledger.resolve()[1]
    for ordinal in range(additions):
        raw = make_raw_record(source_id=f"fixture:measurement:{ordinal}")
        observation = make_observation(raw_record_id=raw.record_id)
        metric = make_metric_result(observation_id=observation.observation_id)
        diagnostic = make_diagnostic_result(metric_result_id=metric.result_id)
        with seed.service.open_storage(
            seed.service.resolve(seed.source), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            storage.raw_records.save(raw)
            storage.observations.save(observation)
            storage.metric_results.save(metric)
            storage.diagnostics.save(diagnostic)
    source_db = seed.source / "storage/data/processed/investment_analyst.duckdb"
    candidate_db = seed.destination / "storage/v2/index.duckdb"
    if fault in {"noncanonical", "bad_metadata"}:
        with seed.service.open_storage(
            seed.service.resolve(seed.source), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            key, document = storage.store.connection.execute(
                "SELECT result_id,document_json FROM metric_results ORDER BY result_id LIMIT 1"
            ).fetchone()
            if fault == "noncanonical":
                storage.store.connection.execute(
                    "UPDATE metric_results SET document_json=? WHERE result_id=?",
                    [json.dumps(json.loads(document), indent=2), key],
                )
            else:
                storage.store.connection.execute(
                    "UPDATE metric_results SET metric_key='changed metadata' WHERE result_id=?",
                    [key],
                )
    budget = admission.budget.model_copy(
        update={
            "assignments": (
                admission.budget.assignments[0],
                DatabaseResourceAssignment(
                    database=source_db,
                    policy=DuckDBResourcePolicy(
                        memory_limit_bytes=256_000_000,
                        threads=1,
                        spill_limit_bytes=0,
                        temp_directory=tmp_path / "measurement-source-temp",
                    ),
                ),
            )
        }
    )
    path = tmp_path / "measurement-admission.json"
    path.write_text(admission.model_copy(update={"budget": budget}).model_dump_json())
    before = {p: p.read_bytes() for p in (source_db, candidate_db, *ledger.state.glob("*.json"))}
    if fault == "wal":
        source_db.with_suffix(".duckdb.wal").write_bytes(b"unreplayed WAL")
    if fault == "changed":
        original = migration._core_native_pages
        touched = False

        def change_during_read(*args, **kwargs):
            nonlocal touched
            for page in original(*args, **kwargs):
                if not touched:
                    touched = True
                    metadata = source_db.stat()
                    os.utime(source_db, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
                yield page

        monkeypatch.setattr(migration, "_core_native_pages", change_during_read)
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        arguments = (
            seed.service,
            seed.source,
            seed.destination,
            uuid4() if fault == "generation" else baseline.generation_id,
            path,
        )
        if fault and fault != "noncanonical":
            with pytest.raises(
                migration.WorkspaceV2MigrationError,
                match="not closed|inputs changed|binding differs|metadata differs",
            ):
                migration.measure_recovered_comparison(*arguments)
        else:
            measured = migration.measure_recovered_comparison(*arguments)
            assert measured["calibration_only"] is True
            assert measured["quiescence_claimed"] is False
            assert measured["successor_reserved"] is False
            assert measured["imports_performed"] is False
            for family, count in (
                ("raw", 1),
                ("observations", 1),
                ("metrics", 2),
                ("diagnostics", 1),
            ):
                data = measured["families"][family]
                assert data["source_count"] == count + additions
                assert data["baseline_known_count"] == count
                assert data["requires_typed_validation"] == additions + (
                    family == "metrics" and fault == "noncanonical"
                )
            if fault == "noncanonical":
                metrics = measured["families"]["metrics"]
                assert metrics["known_source_hash_mismatch"] == 1
                assert metrics["known_metadata_mismatch"] == 0
                assert metrics["canonical_digest"] != metrics["source_document_digest"]
    assert ledger.resolve()[1] == baseline
    assert all(p.read_bytes() == document for p, document in before.items())
    assert not list((tmp_path / "measurement-source-temp").glob("recovered-core-index-*"))


@pytest.mark.parametrize("additions,noncanonical", [(0, False), (1, False), (3, False), (0, True)])
def test_certified_generation_reaches_real_incremental_financial_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, additions: int, noncanonical: bool
) -> None:
    seed, ledger, admission, parent_root, index, parent_manifest = _certified_successor_fixture(
        tmp_path, monkeypatch
    )
    from investment_analyst.workspace.workspace_v2_migration_models import ProgressReconciliationV2

    baseline = ledger.resolve()[1]
    original = {p: p.read_bytes() for p in ledger.state.glob("progress-reconciliation*.json")}
    source = seed.source
    destination = seed.destination
    if noncanonical:
        with seed.service.open_storage(
            seed.service.resolve(source), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            key, document = storage.store.connection.execute(
                "SELECT result_id,document_json FROM metric_results ORDER BY result_id LIMIT 1"
            ).fetchone()
            storage.store.connection.execute(
                "UPDATE metric_results SET document_json=? WHERE result_id=?",
                [json.dumps(json.loads(document), indent=2), key],
            )
    for ordinal in range(additions):
        raw = make_raw_record(source_id=f"fixture:new:{ordinal}")
        observation = make_observation(raw_record_id=raw.record_id)
        metric = make_metric_result(observation_id=observation.observation_id)
        diagnostic = make_diagnostic_result(metric_result_id=metric.result_id)
        with seed.service.open_storage(
            seed.service.resolve(source), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            storage.raw_records.save(raw)
            storage.observations.save(observation)
            storage.metric_results.save(metric)
            storage.diagnostics.save(diagnostic)
    operation = uuid4()
    budget = admission.budget.model_copy(
        update={
            "operation_id": operation,
            "assignments": (
                admission.budget.assignments[0],
                DatabaseResourceAssignment(
                    database=source / "storage/data/processed/investment_analyst.duckdb",
                    policy=DuckDBResourcePolicy(
                        memory_limit_bytes=256_000_000,
                        threads=1,
                        spill_limit_bytes=0,
                        temp_directory=tmp_path / "successor-temp",
                    ),
                ),
            ),
        }
    )
    receipt = admission.model_copy(update={"budget": budget})
    admission_path = tmp_path / "new-successor-admission.json"
    admission_path.write_text(receipt.model_dump_json())
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
        quiescent_source_cut(seed.service, source, operation_id=operation) as cut,
    ):
        reserved = reserve_recovered_successor(
            service=seed.service,
            destination_root=destination,
            reconciliation_id=baseline.generation_id,
            frozen_source=cut,
            admission_path=admission_path,
        )
        assert isinstance(reserved, ProgressReconciliationV2)
        assert reserved.reserved_successor == cut and reserved.phase == "BASELINE_VERIFIED"
        result = migrate_workspace_v1_to_v2(
            service=seed.service,
            source_root=source,
            destination_root=destination,
            page_limit=1,
            host_free_bytes=lambda: 30_000_000_000,
            frozen_source=cut,
            reconciliation_id=baseline.generation_id,
            core_baseline_path=baseline.core_proof.path,
            reconciliation_admission_path=admission_path,
        )
        assert result.historical.complete
        assert result.raw.imported_count == 1 + additions
        bound = ledger.resolve()[1]
        assert bound.phase == "SUCCESSOR_BOUND" and bound.reserved_successor == cut
    terminal = HistoricalTransitionReceipt.model_validate_json(
        (destination / "state" / f"historical-transition-receipt.{operation}.json").read_bytes()
    )
    assert terminal.transition.phase == "COMPLETE"
    assert terminal.final_seal.metric_count == 2 + additions
    with seed.service.open_storage(
        seed.service.resolve(destination), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        assert (
            CompactAnalyticalStore(storage.store.connection).verify_complete()[
                "historical_metric_count"
            ]
            == 2 + additions
        )
        assert storage.raw_records.count() == 1 + additions
    assert all(p.read_bytes() == data for p, data in original.items())
    # Same final epoch, real physical manifest and recovery independent from staging.
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        WorkspaceV2PhysicalDeltaService,
        _verify_full_recovery_coverage,
    )

    class IndependentDrive:
        def __init__(self, root):
            self.root = root
            self.fetches = []

        def copy_immutable_object(self, path, key):
            target = self.root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                assert target.read_bytes() == path.read_bytes()
            else:
                shutil.copyfile(path, target)
            return ()

        def fetch_sealed_object(self, key, path):
            shutil.copyfile(self.root / key, path)
            self.fetches.append(key)
            return ()

    physical = WorkspaceV2PhysicalDeltaService(seed.service)
    delta = tmp_path / "final-delta"
    manifest_sha = sha256_hex(parent_manifest.read_bytes())
    transport = IndependentDrive(tmp_path / "external-drive")
    plan_budget = budget.model_copy(update={"assignments": (budget.assignments[0],)})
    with (
        maintenance_resource_budget(plan_budget),
        maintenance_execution(MaintenanceExecution(plan_budget, lambda *_: None)),
    ):
        manifest = physical.plan(
            destination, index, delta, parent_manifest_sha256=manifest_sha, epoch=operation
        )
    assert manifest.historical_state == "COMPLETE"
    upload = physical.upload(
        delta,
        transport,
        checkpoint_path=delta / "upload-cursor.json",
        receipt_path=delta / "upload-receipt.json",
    )
    hidden = tmp_path / "unavailable-upload-payload"
    (delta / "physical-delta/objects").rename(hidden)
    assert historical_transition_status(destination) == "COMPLETE"
    destination.rename(tmp_path / "unavailable-candidate")
    recovered = tmp_path / "same-epoch-recovery"
    recovered_receipt = physical.restore(
        delta,
        parent_root=parent_root,
        parent_index=index,
        parent_manifest_sha256=manifest_sha,
        destination=recovered,
        transport=transport,
        checkpoint_path=delta / "restore-cursor.json",
        receipt_path=delta / "restore-receipt.json",
    )
    assert recovered_receipt.epoch == upload.epoch == operation
    assert recovered_receipt.manifest_sha256 == upload.manifest_sha256
    assert recovered_receipt.historical_state == "COMPLETE"
    # A recovered photo is inspectable evidence, not a new live C1 owner.
    # Preserve the ordinary operational-root rejection and all original bytes.
    with pytest.raises(WorkspaceV2InventoryError, match="head workspace differs"):
        historical_transition_status(recovered)
    read_budget = plan_budget.model_copy(
        update={
            "assignments": (
                DatabaseResourceAssignment(
                    database=recovered / "storage/v2/index.duckdb",
                    policy=plan_budget.assignments[0].policy,
                ),
            )
        }
    )
    with (
        maintenance_resource_budget(read_budget),
        maintenance_execution(MaintenanceExecution(read_budget, lambda *_: None)),
        seed.service.open_storage(
            seed.service.resolve(recovered), WorkspaceAccessMode.READ_ONLY
        ) as storage,
    ):
        coverage = _verify_full_recovery_coverage(
            recovered, delta / "files.jsonl", manifest, storage.store.connection
        )
        assert coverage["physical_files"] == manifest.file_count
        assert coverage["raw_indexed"] == coverage["raw_files"] == 1 + additions
        assert storage.metric_results.count() == 2 + additions
    assert any(key.startswith("physical-delta/objects/") for key in transport.fetches)
    # Actual installed compatible reader, when this external runtime is present.
    # CI still exercises all data/recovery checks above without depending on it.
    from investment_analyst.application.local_release import LocalReleaseService

    compatible = LocalReleaseService().paths.releases / "dbbdfd021b0fee20be31b2d2a845874b0e861067"
    if (compatible / ".venv/bin/python").is_file():
        import importlib.util
        from types import SimpleNamespace

        scripts = Path(__file__).resolve().parents[3] / "scripts"
        monkeypatch.syspath_prepend(str(scripts))
        spec = importlib.util.spec_from_file_location(
            "generation_closure", scripts / "close_data_chassis.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        controls = tmp_path / "compatible-readback"
        controls.mkdir()
        profile = read_budget.model_copy(
            update={
                "profile": "compatible_44",
                "assignments": (
                    DatabaseResourceAssignment(
                        database=recovered / "storage/v2/index.duckdb",
                        policy=read_budget.assignments[0].policy.model_copy(
                            update={
                                "threads": 2,
                                "memory_limit_bytes": 2_000_000_000,
                                "temp_directory": controls
                                / "resource-temporaries"
                                / str(operation)
                                / "reader",
                            }
                        ),
                    ),
                ),
            }
        )
        profile_path = controls / "budget.json"
        profile_path.write_text(profile.model_dump_json())
        args = SimpleNamespace(
            operation_id=operation,
            source=source,
            destination=destination,
            controls_root=controls,
            resource_budget=profile_path,
            recovery_root=recovered,
            delta_root=delta,
            core_baseline_path=None,
        )
        with (
            maintenance_resource_budget(profile),
            maintenance_execution(MaintenanceExecution(profile, lambda *_: None)),
        ):
            readback = module._complete_recovery(args, recovered_receipt, manifest)
        assert readback["readback"]["raw_count"] == 1 + additions
        assert readback["coverage"]["physical_files"] == manifest.file_count


@pytest.mark.parametrize(
    "additions,fault",
    [
        (0, None),
        (1, None),
        (3, None),
        (0, "before_reservation"),
        (0, "after_reservation"),
        (1, "after_writes"),
        (0, "sql"),
        (0, "network"),
        (0, "host"),
        (1, "after_activating"),
        (0, "known_changed"),
        (0, "known_missing"),
        (0, "extra_invalid"),
        (0, "sql_stall"),
        (0, "prepared_epoch"),
    ],
)
def test_global_controller_runs_certified_delta_recovery_and_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, additions: int, fault: str | None
):
    """Real data/ledger/clock/44 reader/release API; simulate only OS/Drive boundaries."""
    import importlib.util
    import shlex
    import signal
    import threading
    import time
    import traceback
    import urllib.request
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from investment_analyst.application.local_release import (
        DeploymentState,
        LocalReleaseService,
        RealHealthChecker,
    )
    from investment_analyst.storage.resource_policy import MaintenanceResourceSample
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutWindowBound,
        CutWindowGo,
    )

    production = LocalReleaseService()
    release_sha = "dbbdfd021b0fee20be31b2d2a845874b0e861067"
    if not (production.paths.releases / release_sha / ".venv/bin/python").is_file():
        pytest.skip(
            "external prepared release 44 unavailable; portable data route tested separately"
        )
    real_bindings = LocalReleaseService.load_workspace_bindings
    old_state = production.load_deployment_state()
    seed, ledger, admission, _, index, parent_manifest = _certified_successor_fixture(
        tmp_path, monkeypatch
    )
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        WorkspaceV2PhysicalDeltaService,
    )

    prepared_delta = tmp_path / "prepared-W2"
    WorkspaceV2PhysicalDeltaService(seed.service).plan(
        seed.destination,
        index,
        prepared_delta,
        parent_manifest_sha256=hashlib.sha256(parent_manifest.read_bytes()).hexdigest(),
        epoch=ledger.resolve()[1].generation_id,
        allow_incomplete_historical=True,
    )
    monkeypatch.setattr(LocalReleaseService, "load_workspace_bindings", real_bindings)
    for ordinal in range(additions):
        raw = make_raw_record(source_id=f"fixture:controller:{ordinal}")
        observation = make_observation(raw_record_id=raw.record_id)
        metric = make_metric_result(observation_id=observation.observation_id)
        with seed.service.open_storage(
            seed.service.resolve(seed.source), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            storage.raw_records.save(raw)
            storage.observations.save(observation)
            storage.metric_results.save(metric)
            storage.diagnostics.save(make_diagnostic_result(metric_result_id=metric.result_id))
    if fault in {"known_changed", "known_missing", "extra_invalid"}:
        with seed.service.open_storage(
            seed.service.resolve(seed.source), WorkspaceAccessMode.READ_WRITE
        ) as storage:
            if fault == "known_missing":
                storage.store.connection.execute(
                    "DELETE FROM metric_results WHERE result_id=?", [str(seed.metric.result_id)]
                )
            elif fault == "known_changed":
                document = canonical_json_bytes(
                    seed.metric.model_copy(update={"value": Decimal("999")})
                ).decode()
                storage.store.connection.execute(
                    "UPDATE metric_results SET document_json=? WHERE result_id=?",
                    [document, str(seed.metric.result_id)],
                )
            else:
                extra = seed.metric.model_copy(update={"result_id": uuid4()})
                storage.metric_results.save(extra)
                document = json.loads(canonical_json_bytes(extra))
                document["quality"] = "invalid"
                storage.store.connection.execute(
                    "UPDATE metric_results SET document_json=? WHERE result_id=?",
                    [json.dumps(document), str(extra.result_id)],
                )
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "controller_closure", scripts / "close_data_chassis.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    controls = tmp_path / "controller-controls"
    controls.mkdir()
    operation = uuid4()
    recovery_root = tmp_path / "controller-recovery"
    budget = MaintenanceResourceBudget(
        operation_id=operation,
        profile="verification",
        assignments=tuple(
            DatabaseResourceAssignment(
                database=database,
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    temp_directory=controls / "resource-temporaries" / str(operation) / label,
                    spill_limit_bytes=0,
                ),
            )
            for label, database in (
                ("source", seed.source / "storage/data/processed/investment_analyst.duckdb"),
                ("destination", seed.destination / "storage/v2/index.duckdb"),
            )
        ),
    )
    read_budget = MaintenanceResourceBudget(
        operation_id=operation,
        profile="compatible_44",
        assignments=(
            DatabaseResourceAssignment(
                database=recovery_root / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=2_000_000_000,
                    threads=2,
                    spill_limit_bytes=0,
                    temp_directory=controls / "resource-temporaries" / str(operation) / "reader",
                ),
            ),
        ),
    )
    for path, value in (
        (controls / "budget.json", budget),
        (controls / "reader-budget.json", read_budget),
    ):
        path.write_text(value.model_dump_json())
    proof = controls / "fixture-measurement.json"
    proof.write_text('{"universe":"small fixture only"}')
    ref = reconciliation_file_reference(proof, "control")
    head, baseline = ledger.resolve()
    go = CutWindowGo(
        bounds=tuple(
            CutWindowBound(
                step=step,
                upper_seconds=100,
                measurement=ref,
                measured_at=datetime.now(UTC),
                universe="fixture",
                workload="nonempty fixture",
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
        ),
        margin_seconds=100,
        baseline=head.certificate,
        prerequisites=(
            ref,
            reconciliation_file_reference(prepared_delta / "delta-manifest.json", "control"),
        ),
        guard=ref,
        capacity=ref,
        source_workspace_id=baseline.source_workspace_id,
        destination_workspace_id=baseline.destination_workspace_id,
        admitted_at=datetime.now(UTC),
    )
    go_path = controls / "go.json"
    go_path.write_text(go.model_dump_json())
    if fault == "prepared_epoch":
        path = prepared_delta / "delta-manifest.json"
        data = json.loads(path.read_bytes())
        data["epoch"] = str(uuid4())
        path.write_text(json.dumps(data))
    run = controls / "run"
    (run / "code/scripts").mkdir(parents=True)
    shutil.copyfile(scripts / "close_data_chassis.py", run / "code/scripts/close_data_chassis.py")
    args = module._parser().parse_args(
        [
            "cut",
            "--source",
            str(seed.source),
            "--destination",
            str(seed.destination),
            "--controls-root",
            str(controls),
            "--operation-id",
            str(operation),
            "--code-sha",
            "a" * 40,
            "--resource-budget",
            str(controls / "budget.json"),
            "--cut-readback-budget",
            str(controls / "reader-budget.json"),
            "--cut-go",
            str(go_path),
            "--cut-window",
            str(controls / "window.json"),
            "--parent-index",
            str(index),
            "--parent-manifest",
            str(parent_manifest),
            "--parent-manifest-sha256",
            sha256_hex(parent_manifest.read_bytes()),
            "--delta-root",
            str(tmp_path / "controller-delta"),
            "--prepared-delta",
            str(prepared_delta),
            "--recovery-root",
            str(recovery_root),
            "--reconciliation-id",
            str(baseline.generation_id),
            "--supervised-worker",
            "--resource-run-root",
            str(run),
        ]
    )
    unit = tmp_path / "fixture.service"
    env = tmp_path / "fixture.env"
    env.write_text("SEC_USER_AGENT=Fixture fixture@example.test\n")
    env.chmod(0o600)
    previous = production.paths.releases / old_state.current
    unit.write_text(
        f"[Service]\nWorkingDirectory={previous}\nEnvironmentFile={env}\n"
        f'ExecStart="{previous}/.venv/bin/python" '
        f'"{previous}/scripts/serve_investment_analyst.py" '
        f'"--workspace" "{seed.source}" "--port" "8765"\n'
    )

    active_path = tmp_path / "fixture-active"
    active_path.write_text("1")
    stops_path = tmp_path / "fixture-stops"
    stops_path.write_text("0")

    class FixtureSystemctl:
        @property
        def active(self):
            return active_path.read_text() == "1"

        @property
        def stops(self):
            return int(stops_path.read_text())

        def show_properties(self, service, properties):
            fields = dict(
                line.split("=", 1) for line in unit.read_text().splitlines() if "=" in line
            )
            fields.update(
                ActiveState="active" if self.active else "inactive",
                MainPID=str(os.getpid()) if self.active else "0",
            )
            return fields

        def stop(self, service):
            active_path.write_text("0")
            stops_path.write_text(str(self.stops + 1))

        def restart(self, service):
            active_path.write_text("1")

        def is_active(self, service):
            return self.active

        def daemon_reload(self):
            pass

    runner = FixtureSystemctl()
    release = LocalReleaseService(
        paths=tmp_path / "fixture-runtime",
        systemctl=runner,
        systemd_unit_path=unit,
        service_env_path=env,
        health_checker=RealHealthChecker(),
    )
    release.paths = replace(release.paths, releases=production.paths.releases)
    release._save_deployment_state(
        DeploymentState(
            current=old_state.current,
            current_release_path=str(previous),
            updated_at=datetime.now(UTC),
        )
    )
    monkeypatch.setattr(module, "LocalReleaseService", lambda: release)
    monkeypatch.setattr(
        LocalReleaseService,
        "load_workspace_bindings",
        lambda self: (
            real_bindings(release)
            if self.paths.root == production.paths.root
            else real_bindings(self)
        ),
    )
    unit_started = time.monotonic_ns() // 1000
    monkeypatch.setattr(
        module,
        "_unit_properties",
        lambda: {
            "ActiveState": "active",
            "MainPID": str(os.getpid()),
            "ControlPID": "0",
            "ActiveEnterTimestampMonotonic": str(unit_started),
        },
    )
    monkeypatch.setattr(module, "_capacity", lambda: 40_000_000_000)
    monkeypatch.setattr(module, "_memory_composition", lambda *_: None)
    sample = MaintenanceResourceSample(
        host_free_bytes=40_000_000_000,
        memory_high_bytes=budget.memory_high_bytes,
        memory_max_bytes=budget.memory_max_bytes,
        swap_max_bytes=0,
        memory_current_bytes=100_000_000,
        available_memory_bytes=8_000_000_000,
        parent_headroom_bytes=(8_000_000_000,),
        temp_allocated_bytes=0,
        temp_logical_bytes=0,
        log_bytes=0,
        heartbeat_age_seconds=0,
        events=(0, 0, 0),
    )
    monkeypatch.setattr(
        module,
        "sample_cgroup_resources",
        lambda *_a, **kw: sample.model_copy(update={"host_free_bytes": kw["host_free_bytes"]}),
    )
    native_run = subprocess.run

    def fixture_run(command, **kwargs):
        if any("check_workflow_guards.py" in str(arg) for arg in command):
            return subprocess.CompletedProcess(
                command, 0, '{"authoritative":true,"decision":"CONTINUE"}', ""
            )
        return native_run(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", fixture_run)
    real_read = Path.read_bytes

    def process_bytes(path):
        if path == Path(f"/proc/{os.getpid()}/cmdline"):
            command = runner.show_properties("fixture", ())["ExecStart"]
            return b"\0".join(arg.encode() for arg in shlex.split(command)) + b"\0"
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", process_bytes)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            properties = runner.show_properties("fixture", ())
            arguments = shlex.split(properties["ExecStart"])
            root = Path(arguments[arguments.index("--workspace") + 1])
            self.send_response(200 if runner.active else 503)
            self.end_headers()
            self.wfile.write(
                json.dumps({"workspace_id": str(seed.service.manifest(root).workspace_id)}).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_urlopen = urllib.request.urlopen

    def fixture_http(request, **kwargs):
        address = request.full_url if isinstance(request, urllib.request.Request) else request
        assert address.startswith("http://127.0.0.1:8765/")
        return real_urlopen(address.replace(":8765/", f":{server.server_port}/"), **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", fixture_http)

    class Drive:
        def copy_immutable_object(self, path, key):
            target = tmp_path / "fixture-drive" / key
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            return ()

        def fetch_sealed_object(self, key, path):
            if fault == "network":
                raise ConnectionError("fixture remote read interrupted")
            shutil.copyfile(tmp_path / "fixture-drive" / key, path)
            return ()

    monkeypatch.setattr(module, "_transport", lambda *_: Drive())
    try:
        # All native handles from fixture construction are closed before fork.
        # The child owns the actual cut PID/session; SIGKILL cannot kill Pytest.
        child = os.fork()
        if child == 0:
            try:
                if fault in {"before_reservation", "after_reservation"}:
                    reserve = migration_module.reserve_recovered_successor

                    def crash_reservation(**kwargs):
                        if fault == "before_reservation":
                            os._exit(70)
                        reserve(**kwargs)
                        os._exit(71)

                    monkeypatch.setattr(
                        migration_module, "reserve_recovered_successor", crash_reservation
                    )
                if fault == "after_writes":
                    step = module._cut_step

                    def crash_after_writes(*a, **kw):
                        result = step(*a, **kw)
                        if a[1] == "catchup":
                            os._exit(72)
                        return result

                    monkeypatch.setattr(module, "_cut_step", crash_after_writes)
                if fault in {"sql", "sql_stall"}:
                    execute = migration_module.execute_bounded
                    actual_ns = time.monotonic_ns
                    stalled = [False]
                    if fault == "sql_stall":
                        monkeypatch.setattr(
                            module.time,
                            "monotonic_ns",
                            lambda: actual_ns() + (1701_000_000_000 if stalled[0] else 0),
                        )

                    def fail_sql(connection, query, *a, **kw):
                        if query.startswith("CREATE TEMP TABLE _successor_w"):
                            if fault == "sql_stall":
                                stalled[0] = True
                                return execute(
                                    connection, "SELECT sum(i) FROM range(1000000000000) t(i)"
                                )
                            connection.execute("SELECT * FROM fixture_missing_sql_table")
                        return execute(connection, query, *a, **kw)

                    monkeypatch.setattr(migration_module, "execute_bounded", fail_sql)
                if fault == "host":
                    monkeypatch.setattr(
                        module, "_capacity", lambda: 40_000_000_000 if runner.active else 0
                    )
                if fault == "after_activating":
                    monkeypatch.setattr(
                        LocalReleaseService, "_save_workspace_bindings", lambda *_: os._exit(73)
                    )
                result = module._run_global_cut(args)
                (tmp_path / "controller-result.json").write_text(json.dumps(result))
                os._exit(0)
            except BaseException:
                (tmp_path / "controller-error.txt").write_text(traceback.format_exc())
                os._exit(74)
        deadline = time.monotonic() + 60
        status = None
        while time.monotonic() < deadline:
            waited, code = os.waitpid(child, os.WNOHANG)
            if waited:
                status = code
                break
            time.sleep(0.05)
        if status is None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            pytest.fail("fixture controller exceeded its bounded test time")
        if fault is None:
            assert status == 0, (tmp_path / "controller-error.txt").read_text()
            result = json.loads((tmp_path / "controller-result.json").read_bytes())
        else:
            assert status != 0
            if fault == "prepared_epoch":
                assert not args.cut_window.exists()
                assert runner.active and runner.stops == 0
                assert (
                    hashlib.sha256(
                        (seed.destination / "storage/v2/index.duckdb").read_bytes()
                    ).hexdigest()
                    == baseline.database.sha256
                )
                return
            before = (seed.destination / "storage/v2/index.duckdb").read_bytes()
            reservations = {
                p: p.read_bytes()
                for p in ledger.state.glob("progress-successor-reservation.*.json")
            }
            monkeypatch.setattr(
                module,
                "_unit_properties",
                lambda: {
                    "ActiveState": "deactivating",
                    "MainPID": "0",
                    "ControlPID": str(os.getpid()),
                },
            )
            args.cut_recover = True
            result = module._recover_global_cut(args)
            assert before == (seed.destination / "storage/v2/index.duckdb").read_bytes()
            assert all(path.read_bytes() == data for path, data in reservations.items())
            if fault != "after_activating":
                assert runner.active
                assert '"--workspace" "' + str(seed.source) + '"' in unit.read_text()
                assert result["window_receipt"]["phase"] == "HEALTHY_V1"
                if fault not in {"before_reservation", "host"}:
                    assert ledger.attempt_invalidated(operation)
                return
        assert result["window_receipt"]["phase"] == "HEALTHY_V2"
        assert ledger.resolve()[1].phase == "SUCCESSOR_BOUND"
        assert runner.stops == 1
        assert (
            release.load_workspace_bindings().current.workspace_id
            == baseline.destination_workspace_id
        )
        receipt = json.loads((args.delta_root / "restore-receipt.json").read_bytes())
        assert receipt["epoch"] == str(operation) and receipt["historical_state"] == "COMPLETE"
        assert result["health"]["http"]["/api/v1/overview"] == 200
        capture = json.loads((args.controls_root / "paired-cut-byte-capture.json").read_bytes())
        assert capture["operation_id"] == str(operation)
        assert capture["source_cut"]["operation_id"] == str(operation)
        assert (
            capture["source_session"]
            == json.loads(args.cut_window.read_bytes())["owner"]["source_session"]
        )
        source_bytes, target_bytes = capture["pairs"]
        assert source_bytes["format_version"] == 1 and target_bytes["format_version"] == 2
        assert [m["row_count"] for m in source_bytes["analytical_payloads"]] == [
            m["row_count"] for m in target_bytes["analytical_payloads"]
        ]
        assert source_bytes["workspace_bytes"]["logical_bytes"] > 0
        assert capture["collector_elapsed_seconds"] > 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
