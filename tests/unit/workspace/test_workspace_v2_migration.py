"""Tests for the v1 to v2 migration coordinator."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import duckdb
import pytest

import investment_analyst.workspace.workspace_v2_migration as migration_module
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.observation_v2 import (
    OBSERVATION_V2_COLUMNS,
    ensure_observation_v2_table,
    observation_to_row,
)
from investment_analyst.storage.serialization import canonical_json_bytes
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    create_v1_migration_source_snapshot,
    migrate_workspace_v1_to_v2,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV2MigrationVerificationProof,
    WorkspaceV2MigrationWorkCheckpoint,
    WorkspaceV2MigrationWorkPhase,
)

_MOMENT = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.mark.parametrize("row_count", [0, 1, 257, 2000])
@pytest.mark.parametrize("skew", [False, True])
def test_catchup_partitions_are_bounded_ordered_and_complete(
    tmp_path: Path, row_count: int, skew: bool
) -> None:
    from uuid import uuid4

    from investment_analyst.storage.resource_policy import (
        DatabaseResourceAssignment,
        DuckDBResourcePolicy,
        MaintenanceExecution,
        MaintenanceResourceBudget,
        connect_duckdb,
        maintenance_execution,
        maintenance_resource_budget,
    )

    database = tmp_path / "source-db/index.duckdb"
    database.parent.mkdir()
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(),
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=database,
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    spill_limit_bytes=500_000_000,
                    temp_directory=tmp_path / "source-temp",
                ),
            ),
        ),
    )
    execution = MaintenanceExecution(budget, lambda *_: None)
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(execution),
        connect_duckdb(database, read_only=False) as connection,
    ):
        connection.execute(
            "CREATE TABLE raw_record_index("
            "record_id VARCHAR PRIMARY KEY,received_at TIMESTAMPTZ NOT NULL,"
            "document_json VARCHAR NOT NULL)"
        )
        if row_count:
            document = (
                '{"payload":"' + ("x" * (3 * 1024 * 1024)) + '"}'
                if row_count == 1
                else '{"payload":"short"}'
            )
            rows = [
                (
                    str(uuid5(NAMESPACE_URL, f"catchup-{index}")),
                    (
                        _MOMENT
                        if skew
                        else _MOMENT + timedelta(days=index % 7, microseconds=index // 7)
                    ),
                    document,
                )
                for index in range(row_count)
            ]
            connection.executemany("INSERT INTO raw_record_index VALUES (?, ?, ?)", rows)
            reference = [
                str(row[0])
                for row in connection.execute(
                    "SELECT record_id FROM raw_record_index ORDER BY received_at,record_id"
                ).fetchall()
            ]
            particles = list(
                migration_module._iter_catchup_partitions(
                    connection,
                    execution,
                    family="fixture",
                    table="raw_record_index",
                    order_column="received_at",
                    id_column="record_id",
                    width_expression="octet_length(encode(document_json))+128",
                    row_budget=32,
                    byte_budget=256 * 1024,
                )
            )
            assert particles
            assert all(particle.row_count > 0 for particle in particles)
            observed: list[str] = []
            for particle in particles:
                measured = connection.execute(
                    "SELECT count(*),coalesce(sum(octet_length(encode(document_json))+128),0) "
                    f"FROM raw_record_index WHERE {particle.predicate}",
                    list(particle.parameters),
                ).fetchone()
                assert measured == (particle.row_count, particle.byte_count)
                observed.extend(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT record_id FROM raw_record_index "
                        f"WHERE {particle.predicate} ORDER BY received_at,record_id",
                        list(particle.parameters),
                    ).fetchall()
                )
            assert observed == reference
            if skew and row_count > 32:
                # The single timestamp can only be split by identity, never by time.
                assert len(particles) > 1
            if row_count == 1:
                assert particles[0].byte_count > 256 * 1024
        else:
            assert (
                list(
                    migration_module._iter_catchup_partitions(
                        connection,
                        execution,
                        family="fixture",
                        table="raw_record_index",
                        order_column="received_at",
                        id_column="record_id",
                        width_expression="octet_length(encode(document_json))+128",
                    )
                )
                == []
            )


def test_catchup_partitions_survive_spill_quota_that_breaks_global_projection(
    tmp_path: Path,
) -> None:
    """Small quota: the old full projection/ORDER BY dies, bounded slices complete."""
    from uuid import uuid4

    from investment_analyst.storage.resource_policy import (
        DatabaseResourceAssignment,
        DuckDBResourcePolicy,
        MaintenanceExecution,
        MaintenanceResourceBudget,
        connect_duckdb,
        maintenance_execution,
        maintenance_resource_budget,
    )

    database = tmp_path / "source-db/index.duckdb"
    database.parent.mkdir()
    with duckdb.connect(str(database)) as build:
        build.execute(
            "CREATE TABLE raw_record_index("
            "record_id VARCHAR PRIMARY KEY,received_at TIMESTAMPTZ NOT NULL,"
            "document_json VARCHAR NOT NULL)"
        )
        build.execute(
            "INSERT INTO raw_record_index SELECT uuid()::VARCHAR,"
            "TIMESTAMPTZ '2026-01-01 00:00:00+00' + (i % 28) * INTERVAL 1 DAY "
            "+ (i % 60) * INTERVAL 1 SECOND, repeat('x', 4096) FROM range(20000) t(i)"
        )
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(),
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=database,
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=64_000_000,
                    threads=1,
                    spill_limit_bytes=1_000_000,
                    temp_directory=tmp_path / "source-temp",
                ),
            ),
        ),
    )
    execution = MaintenanceExecution(budget, lambda *_: None)
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(execution),
        connect_duckdb(database, read_only=False) as connection,
    ):
        with pytest.raises(duckdb.Error):
            connection.execute(
                "CREATE TEMP TABLE _old_projection AS SELECT record_id,"
                "md5(document_json) AS digest,document_json AS metadata FROM raw_record_index"
            )
            connection.execute(
                "CREATE TEMP TABLE _old_sorted AS SELECT r.record_id,w.digest,w.metadata "
                "FROM raw_record_index r LEFT JOIN _old_projection w "
                "ON r.record_id=w.record_id ORDER BY r.received_at,r.record_id"
            )
        connection.execute("DROP TABLE IF EXISTS _old_sorted")
        connection.execute("DROP TABLE IF EXISTS _old_projection")
        observed = 0
        for particle in migration_module._iter_catchup_partitions(
            connection,
            execution,
            family="quota",
            table="raw_record_index",
            order_column="received_at",
            id_column="record_id",
            width_expression="octet_length(encode(document_json))+128",
            row_budget=512,
            byte_budget=1024 * 1024,
        ):
            rows = connection.execute(
                "SELECT record_id FROM raw_record_index "
                f"WHERE {particle.predicate} ORDER BY received_at,record_id",
                list(particle.parameters),
            ).fetchall()
            assert len(rows) == particle.row_count
            observed += len(rows)
        assert observed == 20000


@pytest.mark.parametrize("value", ["1.00", "3E-12", "-0.00", "123456789012345678.123456789"])
@pytest.mark.parametrize("microseconds", [0, 1000, 123456])
def test_observation_native_canonical_document_preserves_exact_model(
    value: str, microseconds: int
) -> None:
    instant = _MOMENT + timedelta(microseconds=microseconds)
    source = SourceReference(
        source_id="fixture:雪",
        record_key="acción\n雪",
        retrieved_at=instant,
        raw_uri='fixture://雪/"\\',
        checksum_sha256="A" * 64,
    )
    model = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, "native-observation"),
        raw_record_id=uuid5(NAMESPACE_URL, "native-raw"),
        asset_id="equity:us:aapl",
        field_name="acción\n雪",
        value=Decimal(value),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=None,
        period_start=instant - timedelta(days=1),
        period_end=instant,
        available_at=instant,
        normalized_at=instant + timedelta(hours=1),
        source=source,
        quality=DataQuality.VALID,
        transformation_version="fixture-native-v1",
    )
    with duckdb.connect(":memory:") as connection:
        ensure_observation_v2_table(connection, create=True)
        connection.execute(
            "INSERT INTO normalized_observations_v2 ("
            + ",".join(OBSERVATION_V2_COLUMNS)
            + ") VALUES ("
            + ",".join("?" for _ in OBSERVATION_V2_COLUMNS)
            + ")",
            observation_to_row(model),
        )
        actual = connection.execute(
            "SELECT "
            + migration_module._observation_canonical_sql("o")
            + " FROM normalized_observations_v2 o"
        ).fetchone()[0]
    assert actual.encode() == canonical_json_bytes(model)


@pytest.mark.parametrize(
    "module,baseline,candidate,compatible",
    [
        (
            "storage/workspace_v2_inventory.py",
            b"class WorkspaceV2ProgressReconciler:\n def verify(self): return 1\n"
            b"class WorkspaceV2AnalyticalImporter:\n def run(self): return 1\n",
            b"class WorkspaceV2ProgressReconciler:\n def verify(self): return 1\n"
            b"class WorkspaceV2AnalyticalImporter:\n def run(self): return 2\n",
            True,
        ),
        (
            "storage/workspace_v2_inventory.py",
            b"class WorkspaceV2ProgressReconciler:\n def verify(self): return 1\n",
            b"class WorkspaceV2ProgressReconciler:\n def verify(self): return 2\n",
            False,
        ),
        (
            "storage/workspace_v2_inventory.py",
            b"class WorkspaceV2AnalyticalImporter:\n def run(self): return 1\n",
            b"@side_effect\nclass WorkspaceV2AnalyticalImporter:\n def run(self): return 1\n",
            False,
        ),
        (
            "storage/workspace_v2_inventory.py",
            b"class WorkspaceV2AnalyticalImporter:\n def run(self): return 1\n",
            b"class WorkspaceV2AnalyticalImporter:\n def run(self, x=execute()): return 1\n",
            False,
        ),
        (
            "storage/compact_analytical_v2.py",
            b"class CompactAnalyticalStore:\n def verify(self): return 1\n",
            b"class CompactAnalyticalStore:\n def verify(self): return 1\n"
            b" def recovered_historical_transition(self, proof): return proof\n",
            True,
        ),
        (
            "storage/compact_analytical_v2.py",
            b"class CompactAnalyticalStore:\n def verify(self): return 1\n",
            b"class CompactAnalyticalStore:\n def verify(self): return 2\n"
            b" def recovered_historical_transition(self, proof): return proof\n",
            False,
        ),
        (
            "storage/compact_analytical_v2.py",
            b"THRESHOLD=1\nclass CompactAnalyticalStore: pass\n",
            b"THRESHOLD=2\nclass CompactAnalyticalStore: pass\n",
            False,
        ),
        (
            "storage/historical_analytical_import.py",
            b"def read(): return 1\n",
            b"def read(): return 2\n",
            False,
        ),
        (
            "workspace/workspace_v2_migration_models.py",
            b"class Model: field=1\n",
            b"class Model: field=2\n",
            False,
        ),
    ],
)
def test_baseline_verifier_compatibility_preserves_code_dependencies(
    module: str, baseline: bytes, candidate: bytes, compatible: bool
) -> None:
    assert (
        migration_module._baseline_verifier_ast(baseline, module, candidate=False)
        == migration_module._baseline_verifier_ast(candidate, module, candidate=True)
    ) is compatible


@pytest.mark.parametrize(
    "addition",
    [
        b" @execute\n def recovered_historical_transition(self): pass\n",
        b" def recovered_historical_transition(self, proof=execute()): pass\n",
        b" def recovered_historical_transition(self, *, proof=execute()): pass\n",
    ],
)
def test_baseline_verifier_compatibility_rejects_import_time_successor_effects(
    addition: bytes,
) -> None:
    with pytest.raises(WorkspaceV2MigrationError, match="definition is unsafe"):
        migration_module._baseline_verifier_ast(
            b"class CompactAnalyticalStore:\n" + addition,
            "storage/compact_analytical_v2.py",
            candidate=True,
        )


def _seed(service: WorkspaceService, root: Path) -> None:
    initialized = service.initialize(root)
    source = SourceReference(
        source_id="fixture:migration",
        record_key="record",
        retrieved_at=_MOMENT,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "migration-raw"),
        asset_id="equity:us:aapl",
        source=source,
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"close": "200.00"},
        schema_version="migration-test-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, "migration-observation"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="close",
        value=Decimal("200.00"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_MOMENT,
        available_at=_MOMENT,
        normalized_at=_MOMENT,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="migration-test-v1",
    )
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)


def test_migration_copies_v1_models_to_a_disjoint_v2_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed(service, source)
    original_copy = migration_module._copy_documents

    def copy_with_bounded_sql(documents: Path, target: LocalStorage) -> int:
        assert target.store.connection.execute(
            "SELECT current_setting('max_temp_directory_size')"
        ).fetchone() == ("953.6 MiB",)
        return original_copy(documents, target)

    monkeypatch.setattr(migration_module, "_copy_documents", copy_with_bounded_sql)

    receipt = migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )

    assert receipt.raw.imported_count == 1
    assert receipt.observations.imported_count == 1
    assert receipt.historical.metric_count == 0
    assert receipt.analytical.metric_count == 0
    assert service.inspect(source).format_version == 1
    target = service.inspect(destination)
    assert target.format_version == 2
    assert target.raw_record_count == target.observation_count == 1


def test_migration_rejects_destination_inside_source(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    _seed(service, source)

    with pytest.raises(WorkspaceV2MigrationError, match="disjoint"):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=source / "destination",
            host_free_bytes=lambda: 30_000_000_000,
        )


def test_work_checkpoint_is_strict_and_json_serializable() -> None:
    checkpoint = WorkspaceV2MigrationWorkCheckpoint(
        operation_id=uuid5(NAMESPACE_URL, "operation"),
        phase=WorkspaceV2MigrationWorkPhase.IMPORT,
        snapshot_id=uuid5(NAMESPACE_URL, "snapshot"),
        source_fingerprint="a" * 64,
        destination_workspace_id=uuid5(NAMESPACE_URL, "destination"),
        source_root=Path("/tmp/source"),
        destination_root=Path("/tmp/destination"),
        staging_root=Path("/tmp/staging"),
        policy_version="workspace-v2-migration-v1",
        reader_policy="snapshot-raw-readonly-v1",
        verifier_policy="full-model-lineage-v1",
        tree_fingerprint="c" * 64,
        environment_fingerprint="d" * 64,
        cursor="raw:256",
        ordinal=1,
        relative_path="storage/v2/raw/page-0001",
        offset_bytes=4096,
        page_limit=256,
        confirmed_count=256,
        confirmed_bytes=4096,
        accumulated_digest="b" * 64,
        input_references=("source-inventory:" + "e" * 64,),
        updated_at=_MOMENT,
    )

    assert (
        WorkspaceV2MigrationWorkCheckpoint.model_validate_json(checkpoint.model_dump_json())
        == checkpoint
    )


def test_verification_proof_is_strict_and_json_serializable() -> None:
    proof = WorkspaceV2MigrationVerificationProof(
        operation_id=uuid5(NAMESPACE_URL, "verification-operation"),
        snapshot_id=uuid5(NAMESPACE_URL, "verification-snapshot"),
        source_workspace_id=uuid5(NAMESPACE_URL, "verification-source"),
        destination_workspace_id=uuid5(NAMESPACE_URL, "verification-destination"),
        source_root=Path("/tmp/source"),
        destination_root=Path("/tmp/destination"),
        source_fingerprint="a" * 64,
        inventory_digest="b" * 64,
        family="analytical",
        subphase="paired_models",
        reader_policy="snapshot-raw-readonly-v1",
        verifier_policy="full-model-lineage-v1",
        tree_fingerprint="c" * 64,
        environment_fingerprint="d" * 64,
        coverage_count=256,
        source_digest="e" * 64,
        destination_digest="f" * 64,
        ordered_unit_proof_root="g" * 64,
        physical_binding="h" * 64,
        verified_at=_MOMENT,
    )

    assert (
        WorkspaceV2MigrationVerificationProof.model_validate_json(proof.model_dump_json()) == proof
    )


def test_snapshot_transition_preserves_a_checkpoint_and_imports_b_suffix(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed(service, source)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    reference = SourceReference(
        source_id="fixture:migration",
        record_key="record-b",
        retrieved_at=_MOMENT,
    )
    appended = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "migration-raw-b"),
        asset_id="equity:us:aapl",
        source=reference,
        event_time=_MOMENT.replace(hour=1),
        available_at=_MOMENT.replace(hour=1),
        received_at=_MOMENT.replace(hour=1),
        payload={"close": "201.00"},
        schema_version="migration-test-v1",
    )
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(appended)
    snapshot = create_v1_migration_source_snapshot(
        service=service,
        source_root=source,
        snapshot_root=tmp_path / "source-copy",
        host_free_bytes=lambda: 30_000_000_000,
    )

    receipt = migrate_workspace_v1_to_v2(
        service=service,
        source_root=snapshot.snapshot_root,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        reconcile_source=True,
        clock=lambda: _MOMENT,
    )

    assert receipt.source_snapshot_id == str(snapshot.snapshot_id)
    assert service.inspect(destination).raw_record_count == 2
    transition = service.v2_source_transition(service.resolve(destination))
    assert transition is not None
    assert transition.phase.value == "COMPLETE"
    assert transition.confirmed_equivalent_count == 2
    assert list((destination / "storage" / "v2").glob("raw-v2-import-state.A.*.json"))


def test_cut_session_intent_is_one_use_and_native_handoff_keeps_writer_exclusion(tmp_path: Path):
    import subprocess
    import sys
    from uuid import uuid4

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root = tmp_path / "source"
    service.initialize(root, format_version=1)
    operation = uuid4()
    intent = migration_module._QuiescentSourceIntent(root, operation)
    with migration_module.quiescent_source_cut(
        service, root, operation_id=operation, _session_intent=intent
    ) as cut:
        proof = migration_module._live_quiescent_source(root, cut)
        assert proof.writer_generation == intent.writer_generation
        migration_module.close_quiescent_native_reader(root, cut)
        migration_module.close_quiescent_native_reader(root, cut)
        assert (
            migration_module._live_quiescent_source(root, cut).writer_generation
            == intent.writer_generation
        )
        code = (
            "import duckdb,sys; duckdb.connect(sys.argv[1],"
            "config={'memory_limit':'64000000B','threads':'1',"
            "'max_temp_directory_size':'0B'}).close()"
        )
        arguments = [
            sys.executable,
            "-c",
            code,
            str(root / "storage/data/processed/investment_analyst.duckdb"),
        ]
        blocked = subprocess.run(arguments, capture_output=True, text=True, timeout=10)
        assert blocked.returncode != 0 and "lock" in blocked.stderr.lower()
    opened = subprocess.run(arguments, capture_output=True, text=True, timeout=10)
    assert opened.returncode == 0, opened.stderr
    with (
        pytest.raises(WorkspaceV2MigrationError, match="consumed"),
        migration_module.quiescent_source_cut(
            service, root, operation_id=operation, _session_intent=intent
        ),
    ):
        pass
    with migration_module.quiescent_source_cut(service, root, operation_id=operation) as fresh:
        assert (
            migration_module._live_quiescent_source(root, fresh).writer_generation
            != intent.writer_generation
        )
    with pytest.raises(TypeError):
        migration_module._QuiescentSourceIntent(
            root, operation, writer_generation=intent.writer_generation
        )
