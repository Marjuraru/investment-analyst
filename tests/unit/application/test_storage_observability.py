"""Tests for additive per-attempt storage observability."""

import json
import time as monotonic_time
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

import investment_analyst.application.storage_observability as storage_observability_module
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobInvocation,
)
from investment_analyst.application.storage_observability import (
    ScheduledJobObservation,
    StorageObservabilityCollector,
    StorageObservabilityDurations,
    StorageObservabilityDurationsV1,
    StorageObservabilityDurationsV3,
    StorageObservabilityError,
    StorageObservabilityGrowthClassification,
    StorageObservabilityRecord,
    StorageObservabilityRecordV1,
    StorageObservabilityRecordV3,
    StorageObservabilityState,
    StorageObservabilityTableBytes,
    parse_storage_observability_state,
)

_BASE = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
_ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000001")
_JOB_ID = "equity:us:aapl:market-daily"
_ARTIFACT_NAME = "storage_observability_v1.jsonl"
_LIMA_OFFSET = timezone(timedelta(hours=-5))


class _ScriptedClock:
    """Advance deterministically so no test depends on wall-clock time."""

    def __init__(self, start: datetime, step: timedelta = timedelta(seconds=1)) -> None:
        self._current = start - step
        self._step = step

    def advance_to(self, moment: datetime) -> None:
        self._current = moment - self._step

    def __call__(self) -> datetime:
        self._current += self._step
        return self._current


def _database_path(root: Path) -> Path:
    return root / "storage" / "data" / "processed" / "investment_analyst.duckdb"


def _create_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE raw_record_index (record_id VARCHAR, document_json VARCHAR)"
        )
        connection.execute("CREATE TABLE metric_results (result_id VARCHAR, document_json VARCHAR)")
        connection.execute("INSERT INTO metric_results VALUES ('a', '{\"x\": 1}'), ('b', 'hola')")
    finally:
        connection.close()


def _create_classification_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE raw_record_index (record_id VARCHAR, document_json VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE normalized_observations (observation_id VARCHAR, document_json VARCHAR)"
        )
        connection.execute("CREATE TABLE metric_results (result_id VARCHAR, document_json VARCHAR)")
        connection.execute(
            "CREATE TABLE diagnostic_results (diagnostic_id VARCHAR, document_json VARCHAR)"
        )
        connection.execute("CREATE TABLE assets (asset_id VARCHAR, document_json VARCHAR)")
    finally:
        connection.close()


def _insert(database: Path, statements: tuple[str, ...]) -> None:
    connection = duckdb.connect(str(database))
    try:
        for statement in statements:
            connection.execute(statement)
    finally:
        connection.close()


def _collector(
    root: Path,
    *,
    database_path: Path | None = None,
    clock: _ScriptedClock | None = None,
) -> StorageObservabilityCollector:
    return StorageObservabilityCollector(
        state_root=root / "state",
        database_path=database_path if database_path is not None else _database_path(root),
        clock=clock if clock is not None else _ScriptedClock(_BASE),
    )


def _observation(
    *,
    attempt_status: str = "succeeded",
    evidence_changed: bool | None = True,
    rows_created: int | None = 3,
    rows_reused: int | None = 0,
    local_date: date | None = None,
    attempt_id: UUID = _ATTEMPT_ID,
) -> ScheduledJobObservation:
    return ScheduledJobObservation(
        attempt_id=attempt_id,
        job_id=_JOB_ID,
        attempt_number=1,
        local_date=local_date if local_date is not None else _BASE.date(),
        attempt_status=attempt_status,
        evidence_changed=evidence_changed,
        rows_created=rows_created,
        rows_reused=rows_reused,
    )


def _record(root: Path, clock: _ScriptedClock | None = None) -> StorageObservabilityRecordV3:
    collector = _collector(root, clock=clock)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    return collector.complete_attempt(handle, _observation(), job_execution_ms=0)


def test_storage_observability_contract_is_frozen_and_versioned(tmp_path: Path) -> None:
    record = _record(tmp_path)

    assert record.schema_version == "storage-observability-v3"
    assert StorageObservabilityRecordV3.model_config["frozen"] is True
    assert StorageObservabilityRecordV3.model_config["extra"] == "forbid"
    assert StorageObservabilityRecord.model_config["frozen"] is True
    assert StorageObservabilityRecord.model_config["extra"] == "forbid"
    assert StorageObservabilityRecordV1.model_config["frozen"] is True
    assert StorageObservabilityRecordV1.model_config["extra"] == "forbid"
    assert StorageObservabilityDurations.model_config["frozen"] is True
    assert StorageObservabilityDurationsV1.model_config["frozen"] is True
    assert StorageObservabilityState.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        record.attempt_status = "changed"
    with pytest.raises(ValidationError):
        StorageObservabilityRecordV3(**{**record.model_dump(), "unexpected_field": 1})
    with pytest.raises(ValidationError):
        StorageObservabilityRecordV3(
            **{**record.model_dump(), "schema_version": "storage-observability-v2"}
        )

    for model in (
        ScheduledJobObservation,
        StorageObservabilityDurations,
        StorageObservabilityDurationsV1,
        StorageObservabilityGrowthClassification,
        StorageObservabilityRecord,
        StorageObservabilityRecordV1,
        StorageObservabilityRecordV3,
        StorageObservabilityTableBytes,
        StorageObservabilityState,
    ):
        for field in model.model_fields.values():
            assert "Any" not in str(field.annotation)


def test_physical_database_and_wal_bytes_captured_before_and_after(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    collector = _collector(tmp_path)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)

    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "INSERT INTO metric_results VALUES ('c', '{\"x\": 3}'), ('d', '{\"x\": 4}')"
        )
    finally:
        connection.close()
    database_bytes = database.stat().st_size
    wal = Path(f"{database}.wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0

    record = collector.complete_attempt(handle, _observation())

    assert record.database_bytes_before > 0
    assert record.database_bytes_after == database_bytes == database.stat().st_size
    assert record.database_delta_bytes == database_bytes - record.database_bytes_before
    assert record.wal_bytes_before == 0
    assert record.wal_bytes_after == wal_bytes
    assert type(record.database_bytes_after) is int
    assert type(record.wal_bytes_after) is int


def test_stage_durations_reconcile_with_total_duration(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    record = _record(tmp_path, _ScriptedClock(_BASE))

    durations = record.durations
    assert durations is not None
    assert durations.query_open_ms + durations.query_select_ms <= durations.query_ms
    assert record.measurement_state == "complete"
    assert (
        durations.job_execution_ms
        + durations.query_ms
        + durations.collector_unattributed_ms
        + durations.persistence_ms
        + durations.verification_ms
        == durations.total_ms
    )
    with pytest.raises(ValidationError, match="reconcile"):
        StorageObservabilityDurations(
            total_ms=10,
            job_execution_ms=1,
            query_ms=1,
            collector_unattributed_ms=1,
            persistence_ms=1,
            verification_ms=1,
        )
    with pytest.raises(ValidationError, match="reconcile"):
        StorageObservabilityDurations(
            total_ms=9,
            job_execution_ms=1,
            query_ms=1,
            collector_unattributed_ms=1,
            persistence_ms=1,
            verification_ms=1,
        )


def test_terminal_state_persistence_is_outside_job_and_query_durations(tmp_path: Path) -> None:
    clock = _ScriptedClock(_BASE)
    collector = _collector(tmp_path, clock=clock)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    execution_completed_at = _BASE + timedelta(seconds=5)
    result_persisted_at = _BASE + timedelta(seconds=9)
    clock.advance_to(_BASE + timedelta(seconds=10))

    record = collector.complete_attempt(
        handle,
        _observation(),
        execution_completed_at=execution_completed_at,
        result_persisted_at=result_persisted_at,
    )

    assert record.observed_at == execution_completed_at
    assert record.durations is not None
    assert record.durations.job_execution_ms == 0
    assert record.durations.query_ms >= (
        record.durations.query_open_ms + record.durations.query_select_ms
    )
    assert record.durations.total_ms == (
        record.durations.job_execution_ms
        + record.durations.query_ms
        + record.durations.collector_unattributed_ms
        + record.durations.persistence_ms
        + record.durations.verification_ms
    )


def test_collector_does_not_mix_incompatible_lifecycle_clocks(tmp_path: Path) -> None:
    clock = _ScriptedClock(_BASE + timedelta(days=30))
    collector = _collector(tmp_path, clock=clock)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    execution_completed_at = _BASE + timedelta(seconds=5)
    result_persisted_at = _BASE + timedelta(seconds=9)
    clock.advance_to(_BASE + timedelta(days=30, seconds=10))

    record = collector.complete_attempt(
        handle,
        _observation(),
        execution_completed_at=execution_completed_at,
        result_persisted_at=result_persisted_at,
    )

    assert record.observed_at == execution_completed_at
    assert record.durations is not None
    assert record.durations.job_execution_ms == 0
    assert record.durations.query_ms >= 0
    assert record.durations.collector_unattributed_ms >= 0
    assert record.collector_overhead_ms is None
    assert record.durations.total_ms == (
        record.durations.job_execution_ms
        + record.durations.query_ms
        + record.durations.collector_unattributed_ms
        + record.durations.persistence_ms
        + record.durations.verification_ms
    )


def test_collector_keeps_scheduler_clock_ahead_interval_unattributed(tmp_path: Path) -> None:
    collector = _collector(tmp_path, clock=_ScriptedClock(_BASE))
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)

    record = collector.complete_attempt(
        handle,
        _observation(),
        execution_completed_at=_BASE + timedelta(minutes=5),
        result_persisted_at=_BASE + timedelta(minutes=5),
    )

    assert record.durations.job_execution_ms == 0
    assert record.durations.query_ms >= 0
    assert record.durations.collector_unattributed_ms >= 0
    assert record.collector_overhead_ms is None
    assert record.durations.total_ms == (
        record.durations.job_execution_ms
        + record.durations.query_ms
        + record.durations.collector_unattributed_ms
        + record.durations.persistence_ms
        + record.durations.verification_ms
    )


def test_collector_rejects_naive_or_reversed_explicit_timestamps(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    with pytest.raises(StorageObservabilityError, match="timezone-aware"):
        collector.complete_attempt(
            handle,
            _observation(),
            execution_completed_at=datetime(2026, 9, 16, 12),
        )

    later = _BASE + timedelta(seconds=2)
    with pytest.raises(StorageObservabilityError, match="predates"):
        collector.complete_attempt(
            handle,
            _observation(),
            execution_completed_at=later,
            result_persisted_at=_BASE,
        )


def test_explicit_logical_bytes_measurement_is_read_only(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    original = database.read_bytes()
    collector = _collector(tmp_path)

    measured = {item.table_name: item for item in collector._measure_table_bytes()}
    assert tuple(measured) == ("metric_results", "raw_record_index")
    assert measured["metric_results"].row_count == 2
    assert measured["metric_results"].document_bytes == 12
    assert measured["raw_record_index"].row_count == 0
    assert measured["raw_record_index"].document_bytes == 0
    assert database.read_bytes() == original
    assert not Path(f"{database}.wal").exists()


def test_rows_created_versus_reused_correlate_with_attempt_id(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    collector = _collector(tmp_path)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)

    record = collector.complete_attempt(
        handle,
        _observation(evidence_changed=True, rows_created=4, rows_reused=6),
    )

    assert record.attempt_id == _ATTEMPT_ID
    assert record.job_id == _JOB_ID
    assert record.attempt_status == "succeeded"
    assert record.rows_created == 4
    assert record.rows_reused == 6
    assert record.evidence_changed is True
    assert _record(tmp_path / "reused").rows_created == 3

    with pytest.raises(ValidationError, match="together"):
        _observation(evidence_changed=None, rows_created=4, rows_reused=0)
    with pytest.raises(ValidationError, match="created"):
        _observation(evidence_changed=False, rows_created=4, rows_reused=1)
    failed = _observation(
        attempt_status="failed",
        evidence_changed=None,
        rows_created=None,
        rows_reused=None,
    )
    assert failed.rows_created is None and failed.rows_reused is None


def test_growth_is_classified_into_new_revision_and_derived(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_classification_database(database)
    collector = _collector(tmp_path)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)

    _insert(
        database,
        (
            "INSERT INTO raw_record_index VALUES ('r1', '{\"x\": 1}'), ('r2', '{\"x\": 2}')",
            "INSERT INTO metric_results VALUES ('m1', '{\"y\": 1}')",
            "INSERT INTO assets VALUES ('equity:us:aapl', '{\"z\": 1}')",
        ),
    )
    record = collector.complete_attempt(
        handle,
        _observation(evidence_changed=True, rows_created=7, rows_reused=4),
    )

    growth = record.growth
    assert growth is not None
    assert growth.new_evidence_rows == 2
    assert growth.derived_rows == 1
    assert growth.unclassified_rows == 1
    assert growth.revision_rows == 3
    assert growth.classified_rows == record.rows_created == 7
    assert record.to_json_dict()["growth"] == {
        "new_evidence_rows": 2,
        "revision_rows": 3,
        "derived_rows": 1,
        "unclassified_rows": 1,
    }
    assert record.table_bytes == ()


def test_growth_classification_declines_when_the_attempt_does_not_report_rows(
    tmp_path: Path,
) -> None:
    database = _database_path(tmp_path)
    _create_classification_database(database)
    collector = _collector(tmp_path)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)

    _insert(
        database,
        ("INSERT INTO normalized_observations VALUES ('o1', '{\"x\": 1}')",),
    )
    contradicted = collector.complete_attempt(
        handle,
        _observation(evidence_changed=False, rows_created=0, rows_reused=1),
    )

    failed_collector = _collector(tmp_path / "failed")
    failed = failed_collector.complete_attempt(
        failed_collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID),
        _observation(
            attempt_status="failed",
            evidence_changed=None,
            rows_created=None,
            rows_reused=None,
        ),
    )
    unmeasured = _record(tmp_path / "unmeasured")

    assert contradicted.growth is None
    assert failed.growth is None
    assert unmeasured.growth is None
    assert unmeasured.collector_overhead_ms is not None


def test_collector_records_its_own_overhead_separately(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    record = _record(tmp_path, _ScriptedClock(_BASE))

    durations = record.durations
    assert durations is not None
    assert record.collector_overhead_ms == durations.total_ms
    assert durations.job_execution_ms == 0
    assert record.collector_overhead_ms + durations.job_execution_ms == durations.total_ms
    assert record.collector_overhead_ms == sum(
        (
            durations.query_ms,
            durations.collector_unattributed_ms,
            durations.persistence_ms,
            durations.verification_ms,
        )
    )
    assert "collector_overhead_ms" in record.to_json_dict()
    with pytest.raises(ValidationError, match="separate"):
        StorageObservabilityRecordV3(**{**record.model_dump(), "collector_overhead_ms": 5000})


def test_added_classification_fields_are_optional_and_backward_readable(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    record = _record(tmp_path)
    legacy = record.to_json_dict()
    del legacy["growth"]
    del legacy["collector_overhead_ms"]

    parsed = parse_storage_observability_state(f"{json.dumps(legacy)}\n")

    assert len(parsed.records) == 1
    assert parsed.records[0].growth is None
    assert parsed.records[0].collector_overhead_ms is None
    assert parsed.records[0].rows_created == record.rows_created
    assert not StorageObservabilityGrowthClassification.model_fields[
        "new_evidence_rows"
    ].is_required()
    assert StorageObservabilityGrowthClassification().to_json_dict() == {
        "new_evidence_rows": None,
        "revision_rows": None,
        "derived_rows": None,
        "unclassified_rows": None,
    }
    with pytest.raises(ValidationError, match="as a whole"):
        StorageObservabilityGrowthClassification(new_evidence_rows=1)
    with pytest.raises(ValidationError, match="created rows"):
        StorageObservabilityRecordV3(
            **{
                **record.model_dump(),
                "evidence_changed": None,
                "rows_created": None,
                "rows_reused": None,
            }
        )


def test_daily_snapshot_is_compact_and_bounded(tmp_path: Path) -> None:
    clock = _ScriptedClock(_BASE)
    collector = _collector(tmp_path, clock=clock)
    artifact = collector.artifact_path
    measured_records: list[StorageObservabilityRecordV3] = []

    def collect(day_offset: int, attempt: int) -> None:
        moment = _BASE + timedelta(days=day_offset)
        clock.advance_to(moment)
        handle = collector.begin_attempt(
            job_id=_JOB_ID,
            attempt_id=UUID(int=day_offset * 10 + attempt),
        )
        measured_records.append(
            collector.complete_attempt(
                handle,
                _observation(
                    local_date=moment.date(), attempt_id=UUID(int=day_offset * 10 + attempt)
                ),
                job_execution_ms=0,
            )
        )

    collect(0, 1)
    opened_day = artifact.read_text(encoding="utf-8")
    collect(0, 2)
    appended = artifact.read_text(encoding="utf-8")
    assert appended.startswith(opened_day)
    assert len(appended.splitlines()) == len(opened_day.splitlines()) + 1

    collect(1, 1)
    folded = collector.state()
    assert len(folded.daily_snapshots) == 1
    assert folded.daily_snapshots[0].utc_date == _BASE.date()
    assert len(artifact.read_text(encoding="utf-8").splitlines()) == 2
    summary = folded.daily_snapshots[0].job_summaries[0]
    assert summary.job_id == _JOB_ID
    assert summary.attempt_count == 2
    assert summary.attempts_with_evidence == 2
    assert summary.rows_created == 6
    assert summary.measurement_complete_attempts == 2
    assert summary.measurement_partial_attempts == 0
    assert summary.measurement_unavailable_attempts == 0
    assert summary.total_ms == sum(item.durations.total_ms for item in measured_records[:2])

    for offset in range(2, 95):
        collect(offset, 1)

    state = collector.state()
    assert len(state.daily_snapshots) == 90
    assert len(state.records) == 1
    assert state.daily_snapshots[0].utc_date == _BASE.date() + timedelta(days=4)
    assert state.daily_snapshots[-1].utc_date == _BASE.date() + timedelta(days=93)
    assert state.records[0].observed_at.date() == _BASE.date() + timedelta(days=94)
    assert len(artifact.read_text(encoding="utf-8").splitlines()) == 91
    assert state.daily_snapshots[1].job_summaries[0].attempt_count == 1
    assert state.daily_snapshots[1].job_summaries[0].attempts_with_evidence == 1
    with pytest.raises(ValidationError, match="bound"):
        StorageObservabilityState(daily_snapshots=state.daily_snapshots + state.daily_snapshots)


def test_collector_creates_no_duckdb_object_or_migration(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    migrations = Path(__file__).resolve().parents[3] / "src/investment_analyst/storage/migrations"
    migration_files = sorted(item.name for item in migrations.iterdir())

    def catalog() -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
        connection = duckdb.connect(str(database), read_only=True)
        try:
            tables = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT table_name FROM duckdb_tables() ORDER BY table_name"
                ).fetchall()
            )
            columns = tuple(
                tuple(str(value) for value in row)
                for row in connection.execute(
                    "SELECT table_name, column_name FROM information_schema.columns"
                    " ORDER BY table_name, column_name"
                ).fetchall()
            )
        finally:
            connection.close()
        return tables, columns

    before = catalog()

    _record(tmp_path)

    assert catalog() == before
    assert sorted(item.name for item in migrations.iterdir()) == migration_files
    assert sorted(item.name for item in (tmp_path / "state").iterdir()) == [_ARTIFACT_NAME]
    assert sorted(item.name for item in database.parent.iterdir()) == [database.name]


def test_record_is_operational_and_never_analytical_evidence(tmp_path: Path) -> None:
    record = _record(tmp_path)
    payload = json.loads((tmp_path / "state" / _ARTIFACT_NAME).read_text(encoding="utf-8"))

    assert "available_at" not in StorageObservabilityRecordV3.model_fields
    assert "known_at" not in StorageObservabilityRecordV3.model_fields
    assert "available_at" not in payload
    assert payload["schema_version"] == "storage-observability-v3"
    assert record.schema_version == "storage-observability-v3"
    assert "measurement_state" in StorageObservabilityRecordV3.model_fields
    assert "table_rows_before" in StorageObservabilityRecordV3.model_fields
    assert "durations" in StorageObservabilityRecordV3.model_fields
    assert "storage" not in (tmp_path / "state").parts
    assert "storage_observability" in StorageObservabilityRecordV3.__module__


def test_record_uses_utc_and_exact_integer_bytes(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    record = _record(tmp_path, _ScriptedClock(datetime(2026, 9, 16, 7, 0, tzinfo=_LIMA_OFFSET)))

    assert record.observed_at.tzinfo is UTC
    assert record.observed_at.utcoffset() == timedelta(0)
    assert record.observed_at.hour == 12
    assert record.database_bytes_after == database.stat().st_size
    assert {
        type(record.database_bytes_before),
        type(record.database_bytes_after),
        type(record.wal_bytes_before),
        type(record.wal_bytes_after),
    } == {int}
    assert record.table_bytes == ()
    with pytest.raises(ValidationError):
        StorageObservabilityTableBytes(table_name="metric_results", row_count=1.5, document_bytes=1)
    with pytest.raises(ValidationError):
        StorageObservabilityTableBytes.model_validate(
            {"table_name": "metric_results", "row_count": 1, "document_bytes": 1.5}
        )


def test_collector_uses_a_single_writer_and_read_only_measurement(
    tmp_path: Path,
) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    original = database.read_bytes()
    collector = _collector(tmp_path, database_path=database)
    collector.start_cycle()
    try:
        handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
        assert collector._measurement_open_count == 1
        assert collector._measurement_select_count == 2
        record = collector.complete_attempt(handle, _observation(), job_execution_ms=0)
        assert collector._measurement_open_count == 2
        assert collector._measurement_select_count == 3
        assert record.measurement_state == "complete"
        assert record.table_bytes == ()
    finally:
        collector.close_cycle()

    assert collector._measurement_process is None
    assert database.read_bytes() == original
    assert sorted(item.name for item in (tmp_path / "state").iterdir()) == [_ARTIFACT_NAME]


def test_read_deadline_includes_worker_acquisition_and_reaps_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database_path(tmp_path)
    _create_database(database)

    class FakeChannel:
        closed = False

        def send(self, _value: object) -> None:
            return None

        def poll(self, _timeout: float) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        alive = True
        exitcode: int | None = None
        terminated = False

        def is_alive(self) -> bool:
            return self.alive

        def join(self, *, timeout: float) -> None:
            assert timeout <= 0.25

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False
            self.exitcode = -15

        def kill(self) -> None:
            self.alive = False
            self.exitcode = -9

        def close(self) -> None:
            return None

    collector = StorageObservabilityCollector(
        state_root=tmp_path / "state",
        database_path=database,
        measurement_timeout_seconds=0.005,
    )
    process = FakeProcess()
    channel = FakeChannel()
    collector._measurement_process = process  # type: ignore[assignment]
    collector._measurement_channel = channel  # type: ignore[assignment]

    def delayed_acquisition():
        monotonic_time.sleep(0.02)
        return process, channel

    monkeypatch.setattr(collector, "_ensure_measurement_worker", delayed_acquisition)

    with pytest.raises(StorageObservabilityError) as error:
        collector._request_read_measurement()

    assert error.value.reason_code == "measurement_timeout"
    assert process.terminated is True
    assert process.alive is False
    assert channel.closed is True
    assert collector._measurement_worker_exit_codes == [-15]


def test_read_deadline_expires_while_query_is_pending_and_reaps_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    polls: list[float] = []

    class FakeChannel:
        closed = False
        messages: list[object] = []

        def send(self, value: object) -> None:
            self.messages.append(value)

        def poll(self, timeout: float) -> bool:
            polls.append(timeout)
            return False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        alive = True
        exitcode: int | None = None
        terminated = False

        def is_alive(self) -> bool:
            return self.alive

        def join(self, *, timeout: float) -> None:
            assert timeout <= 0.25

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False
            self.exitcode = -15

        def kill(self) -> None:
            self.alive = False
            self.exitcode = -9

        def close(self) -> None:
            return None

    collector = StorageObservabilityCollector(
        state_root=tmp_path / "state",
        database_path=database,
        measurement_timeout_seconds=0.05,
    )
    process = FakeProcess()
    channel = FakeChannel()
    collector._measurement_process = process  # type: ignore[assignment]
    collector._measurement_channel = channel  # type: ignore[assignment]
    monkeypatch.setattr(
        collector,
        "_ensure_measurement_worker",
        lambda: (process, channel),
    )

    with pytest.raises(StorageObservabilityError) as error:
        collector._request_read_measurement()

    assert error.value.reason_code == "measurement_timeout"
    assert len(polls) == 1
    assert 0 < polls[0] <= 0.05
    assert channel.messages == ["measure", None]
    assert channel.closed is True
    assert process.terminated is True
    assert collector._measurement_worker_exit_codes == [-15]


def test_begin_failure_keeps_known_bytes_and_persists_a_partial_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    clock = _ScriptedClock(_BASE)
    collector = _collector(tmp_path, database_path=database, clock=clock)

    def blocked_read() -> None:
        raise StorageObservabilityError("query unavailable", reason_code="engine_error")

    monkeypatch.setattr(collector, "_request_read_measurement", blocked_read)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    assert handle.database_bytes_before is not None
    assert handle.table_rows_before is None

    record = collector.complete_attempt(
        handle,
        _observation(),
        job_execution_ms=0,
    )

    assert record.measurement_state == "partial"
    assert record.failure_phase == "begin"
    assert record.failure_reason == "engine_error"
    assert record.database_bytes_before is not None
    assert record.database_bytes_after is not None
    assert record.database_delta_bytes == 0
    assert record.table_rows_before is None
    assert record.table_rows_after is None
    assert record.durations is not None
    assert collector.state().records == (record,)

    next_day = _BASE + timedelta(days=1)
    clock.advance_to(next_day)
    next_id = UUID("00000000-0000-4000-8000-000000000002")
    next_record = collector.complete_attempt(
        collector.begin_attempt(job_id=_JOB_ID, attempt_id=next_id),
        _observation(attempt_id=next_id, local_date=next_day.date()),
        job_execution_ms=0,
    )
    state = collector.state()
    snapshot = state.daily_snapshots[0]
    assert snapshot.schema_version == "storage-observability-daily-snapshot-v2"
    assert snapshot.measurement_partial_attempts == 1
    assert snapshot.measurement_unavailable_attempts == 0
    assert snapshot.failure_summaries[0].phase == "begin"
    assert snapshot.failure_summaries[0].reason == "engine_error"
    assert snapshot.failure_summaries[0].attempt_count == 1
    assert snapshot.job_summaries[0].failure_summaries == snapshot.failure_summaries
    assert state.records == (next_record,)


def test_collector_failure_never_degrades_the_measured_job(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    blocked_root = tmp_path / "blocked"
    (blocked_root / "state").mkdir(parents=True)
    (blocked_root / "state" / _ARTIFACT_NAME).mkdir()
    blocked = _collector(blocked_root, database_path=database)

    unwritable_root = tmp_path / "unwritable"
    unwritable_root.mkdir()
    (unwritable_root / "state").write_text("", encoding="utf-8")
    unwritable = _collector(unwritable_root, database_path=database)

    now = _BASE + timedelta(minutes=5)

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        return ScheduledJobExecution(
            job_id=invocation.definition.job_id,
            effective_known_at=invocation.started_at,
            evidence_changed=True,
            source_ids=(f"source:{invocation.definition.job_id}",),
            created_count=1,
            reused_count=0,
        )

    definition = ScheduledJobDefinition(
        job_id=_JOB_ID,
        asset_id="equity:us:aapl",
        provider="alpaca-market-data",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=3,
        retry_backoff_seconds=60,
    )

    scenarios = (
        (
            blocked,
            "blocked",
            "storage observability could not record its result",
            "artifact_unreadable",
        ),
        (
            unwritable,
            "unwritable",
            "storage observability could not record its result",
            "artifact_write_failed",
        ),
    )
    for collector, label, expected_issue, expected_reason in scenarios:
        store = MultiAssetScheduleStateStore(tmp_path / f"schedule-{label}.json")
        scheduler = MultiAssetScheduler(
            (RegisteredScheduledJob(definition, run),),
            store,
            storage_observability=collector,
            clock=lambda: now,
        )

        completed = scheduler.tick()

        assert completed[0].status is ScheduledJobAttemptStatus.SUCCEEDED
        assert completed[0].execution is not None
        assert store.load().attempts[0].status is ScheduledJobAttemptStatus.SUCCEEDED
        assert scheduler.status().issues == (
            expected_issue,
            f"storage observability failure reason: {expected_reason}",
        )
        assert not collector.artifact_path.is_file()


def test_measurement_engine_is_bounded_in_memory_and_threads(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    collector = _collector(tmp_path, database_path=database)
    connection = collector._open_read_only_engine()
    try:
        threads = connection.execute("SELECT current_setting('threads')").fetchone()
        memory_limit = connection.execute("SELECT current_setting('memory_limit')").fetchone()
        assert threads == (1,)
        assert memory_limit in (("244.1 MiB",), ("256MB",), ("256.0 MB",))
    finally:
        connection.close()


def test_document_bytes_are_never_measured_inside_attempts_or_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database_path(tmp_path)
    _create_classification_database(database)
    clock = _ScriptedClock(_BASE)
    collector = _collector(tmp_path, database_path=database, clock=clock)
    scans: list[str] = []

    def forbidden_scan(self: StorageObservabilityCollector):
        scans.append("payload")
        raise AssertionError("attempt path must not scan document_json")

    monkeypatch.setattr(StorageObservabilityCollector, "_measure_table_bytes", forbidden_scan)

    id1 = UUID("00000000-0000-4000-8000-000000000001")
    handle1 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id1)
    record1 = collector.complete_attempt(
        handle1,
        _observation(attempt_id=id1, evidence_changed=True, rows_created=1, rows_reused=0),
    )
    assert record1.table_bytes == ()

    id2 = UUID("00000000-0000-4000-8000-000000000002")
    handle2 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id2)
    record2 = collector.complete_attempt(
        handle2,
        _observation(attempt_id=id2, evidence_changed=False, rows_created=0, rows_reused=1),
    )
    assert record2.table_bytes == ()

    id3 = UUID("00000000-0000-4000-8000-000000000003")
    restarted = _collector(tmp_path, database_path=database, clock=clock)
    handle3 = restarted.begin_attempt(job_id=_JOB_ID, attempt_id=id3)
    record3 = restarted.complete_attempt(
        handle3,
        _observation(attempt_id=id3, evidence_changed=False, rows_created=0, rows_reused=1),
    )
    assert record3.table_bytes == ()

    next_day = _BASE + timedelta(days=1)
    clock.advance_to(next_day)
    id4 = UUID("00000000-0000-4000-8000-000000000004")
    next_day_collector = _collector(tmp_path, database_path=database, clock=clock)
    handle4 = next_day_collector.begin_attempt(job_id=_JOB_ID, attempt_id=id4)
    record4 = next_day_collector.complete_attempt(
        handle4,
        _observation(
            attempt_id=id4,
            evidence_changed=False,
            rows_created=0,
            rows_reused=1,
            local_date=next_day.date(),
        ),
    )
    assert record4.table_bytes == ()
    assert scans == []


def test_growth_classification_uses_row_counts_on_every_attempt(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_classification_database(database)
    collector = _collector(tmp_path)

    # Attempt 1: the lightweight phase measures rows but omits logical bytes.
    id1 = UUID("00000000-0000-4000-8000-000000000001")
    handle1 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id1)
    _insert(
        database,
        (
            "INSERT INTO raw_record_index VALUES ('r1', '{\"x\": 1}'), ('r2', '{\"x\": 2}')",
            "INSERT INTO metric_results VALUES ('m1', '{\"y\": 1}')",
            "INSERT INTO assets VALUES ('equity:us:aapl', '{\"z\": 1}')",
        ),
    )
    record1 = collector.complete_attempt(
        handle1,
        _observation(attempt_id=id1, evidence_changed=True, rows_created=7, rows_reused=4),
    )
    assert record1.table_bytes == ()
    assert record1.growth is not None
    assert record1.growth.new_evidence_rows == 2
    assert record1.growth.derived_rows == 1
    assert record1.growth.unclassified_rows == 1
    assert record1.growth.revision_rows == 3
    assert record1.growth.classified_rows == record1.rows_created == 7

    # Attempt 2: second attempt of the same day, table_bytes is empty ()
    id2 = UUID("00000000-0000-4000-8000-000000000002")
    handle2 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id2)
    _insert(
        database,
        (
            "INSERT INTO raw_record_index VALUES ('r3', '{\"x\": 3}')",
            "INSERT INTO diagnostic_results VALUES ('d1', '{\"w\": 1}')",
        ),
    )
    record2 = collector.complete_attempt(
        handle2,
        _observation(attempt_id=id2, evidence_changed=True, rows_created=5, rows_reused=2),
    )
    assert record2.table_bytes == ()
    assert record2.growth is not None
    assert record2.growth.new_evidence_rows == 1
    assert record2.growth.derived_rows == 1
    assert record2.growth.unclassified_rows == 0
    assert record2.growth.revision_rows == 3
    assert record2.growth.classified_rows == record2.rows_created == 5


def test_durations_name_the_job_execution_window_and_the_collector_residual(
    tmp_path: Path,
) -> None:
    _create_database(_database_path(tmp_path))
    record = _record(tmp_path, _ScriptedClock(_BASE))

    durations = record.durations
    assert hasattr(durations, "job_execution_ms")
    assert hasattr(durations, "collector_unattributed_ms")
    assert not hasattr(durations, "network_ms")
    assert not hasattr(durations, "calculation_ms")

    assert "job_execution_ms" in StorageObservabilityDurations.model_fields
    assert "collector_unattributed_ms" in StorageObservabilityDurations.model_fields
    assert "network_ms" not in StorageObservabilityDurations.model_fields
    assert "calculation_ms" not in StorageObservabilityDurations.model_fields

    dumped = durations.model_dump()
    assert "job_execution_ms" in dumped
    assert "collector_unattributed_ms" in dumped
    assert "network_ms" not in dumped
    assert "calculation_ms" not in dumped

    payload = json.loads((tmp_path / "state" / _ARTIFACT_NAME).read_text(encoding="utf-8"))
    artifact_durations = payload["durations"]
    assert "job_execution_ms" in artifact_durations
    assert "collector_unattributed_ms" in artifact_durations
    assert "network_ms" not in artifact_durations
    assert "calculation_ms" not in artifact_durations


def test_v1_and_v2_records_still_parse_while_new_records_are_v3(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    new_record = _record(tmp_path)
    assert new_record.schema_version == "storage-observability-v3"

    v1_line = json.dumps(
        {
            "attempt_id": "00000000-0000-4000-8000-000000000099",
            "attempt_number": 1,
            "attempt_status": "succeeded",
            "collector_overhead_ms": 6000,
            "database_bytes_after": 1000,
            "database_bytes_before": 1000,
            "durations": {
                "calculation_ms": 1000,
                "network_ms": 1000,
                "persistence_ms": 1000,
                "query_ms": 3000,
                "total_ms": 7000,
                "verification_ms": 1000,
            },
            "evidence_changed": True,
            "growth": None,
            "job_id": _JOB_ID,
            "local_date": "2026-09-16",
            "observed_at": "2026-09-16T11:00:03Z",
            "rows_created": 3,
            "rows_reused": 0,
            "schema_version": "storage-observability-v1",
            "table_bytes": [],
            "wal_bytes_after": 0,
            "wal_bytes_before": 0,
        },
        sort_keys=True,
    )
    v2_payload = new_record.to_json_dict()
    v2_payload.update(
        schema_version="storage-observability-v2",
        attempt_id="00000000-0000-4000-8000-000000000098",
        observed_at="2026-09-16T11:30:00Z",
    )
    for field_name in (
        "measurement_state",
        "failure_phase",
        "failure_reason",
        "table_rows_before",
        "table_rows_after",
    ):
        v2_payload.pop(field_name)
    v2_payload["durations"].pop("query_open_ms")
    v2_payload["durations"].pop("query_select_ms")
    v2_line = json.dumps(v2_payload, sort_keys=True)
    v3_line = json.dumps(new_record.to_json_dict(), sort_keys=True)

    state = parse_storage_observability_state(f"{v1_line}\n{v2_line}\n{v3_line}\n")
    assert len(state.records) == 3

    r1 = state.records[0]
    assert isinstance(r1, StorageObservabilityRecordV1)
    assert r1.schema_version == "storage-observability-v1"
    assert r1.durations.network_ms == 1000
    assert r1.durations.calculation_ms == 1000

    r2 = state.records[1]
    assert isinstance(r2, StorageObservabilityRecord)
    assert r2.schema_version == "storage-observability-v2"
    r3 = state.records[2]
    assert isinstance(r3, StorageObservabilityRecordV3)
    assert r3.schema_version == "storage-observability-v3"
    assert r3.durations is not None
    assert new_record.durations is not None
    assert r3.durations.query_select_ms == new_record.durations.query_select_ms


def test_phase_names_keep_their_existing_meaning() -> None:
    durations = StorageObservabilityDurationsV3(
        total_ms=7000,
        job_execution_ms=1000,
        query_ms=3000,
        collector_unattributed_ms=1000,
        persistence_ms=1000,
        verification_ms=1000,
        query_open_ms=500,
        query_select_ms=1500,
    )
    v1_equivalent = StorageObservabilityDurationsV1(
        total_ms=7000,
        network_ms=1000,
        query_ms=3000,
        calculation_ms=1000,
        persistence_ms=1000,
        verification_ms=1000,
    )
    assert durations.job_execution_ms == v1_equivalent.network_ms
    assert durations.collector_unattributed_ms == v1_equivalent.calculation_ms
    assert durations.query_ms == v1_equivalent.query_ms
    assert durations.persistence_ms == v1_equivalent.persistence_ms
    assert durations.verification_ms == v1_equivalent.verification_ms
    assert durations.total_ms == v1_equivalent.total_ms


def test_reconciliation_still_fails_closed_when_phases_do_not_sum() -> None:
    with pytest.raises(ValidationError, match="reconcile"):
        StorageObservabilityDurations(
            total_ms=7000,
            job_execution_ms=1000,
            query_ms=3000,
            collector_unattributed_ms=999,
            persistence_ms=1000,
            verification_ms=1000,
        )

    with pytest.raises(ValidationError, match="reconcile"):
        StorageObservabilityDurations(
            total_ms=7000,
            job_execution_ms=1001,
            query_ms=3000,
            collector_unattributed_ms=1000,
            persistence_ms=1000,
            verification_ms=1000,
        )

    valid_durations = StorageObservabilityDurations(
        total_ms=7000,
        job_execution_ms=1000,
        query_ms=3000,
        collector_unattributed_ms=1000,
        persistence_ms=1000,
        verification_ms=1000,
    )
    with pytest.raises(ValidationError, match="separate"):
        StorageObservabilityRecord(
            observed_at=datetime(2026, 9, 16, 12, 0, 3, tzinfo=UTC),
            attempt_id=UUID("00000000-0000-4000-8000-000000000001"),
            job_id=_JOB_ID,
            attempt_number=1,
            local_date=date(2026, 9, 16),
            attempt_status="succeeded",
            database_bytes_before=0,
            database_bytes_after=0,
            wal_bytes_before=0,
            wal_bytes_after=0,
            collector_overhead_ms=5999,
            durations=valid_durations,
        )


def test_no_network_estimate_is_published(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    record = _record(tmp_path)

    for field_name in StorageObservabilityRecord.model_fields:
        assert "network" not in field_name.lower()
    for field_name in StorageObservabilityDurations.model_fields:
        assert "network" not in field_name.lower()

    dump = record.to_json_dict()
    for key in dump:
        assert "network" not in key.lower()
    for key in dump["durations"]:
        assert "network" not in key.lower()

    file_content = (tmp_path / "state" / _ARTIFACT_NAME).read_text(encoding="utf-8")
    assert "network" not in file_content.lower()


def test_existing_artifact_lines_are_never_rewritten(tmp_path: Path) -> None:
    _create_database(_database_path(tmp_path))
    artifact_path = tmp_path / "state" / _ARTIFACT_NAME
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    v1_line = json.dumps(
        {
            "attempt_id": "00000000-0000-4000-8000-000000000010",
            "attempt_number": 1,
            "attempt_status": "succeeded",
            "collector_overhead_ms": 6000,
            "database_bytes_after": 1000,
            "database_bytes_before": 1000,
            "durations": {
                "calculation_ms": 1000,
                "network_ms": 1000,
                "persistence_ms": 1000,
                "query_ms": 3000,
                "total_ms": 7000,
                "verification_ms": 1000,
            },
            "evidence_changed": True,
            "growth": None,
            "job_id": _JOB_ID,
            "local_date": "2026-09-16",
            "observed_at": "2026-09-16T11:00:00Z",
            "rows_created": 3,
            "rows_reused": 0,
            "schema_version": "storage-observability-v1",
            "table_bytes": [],
            "wal_bytes_after": 0,
            "wal_bytes_before": 0,
        },
        sort_keys=True,
    )
    artifact_path.write_text(f"{v1_line}\n", encoding="utf-8")
    original_bytes = artifact_path.read_bytes()

    collector = _collector(tmp_path)
    id2 = UUID("00000000-0000-4000-8000-000000000020")
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id2)
    collector.complete_attempt(
        handle,
        _observation(attempt_id=id2, local_date=date(2026, 9, 16)),
    )

    new_bytes = artifact_path.read_bytes()
    assert new_bytes.startswith(original_bytes)

    lines = artifact_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == v1_line
    assert json.loads(lines[1])["schema_version"] == "storage-observability-v3"
    assert json.loads(lines[1])["attempt_id"] == str(id2)

    state = collector.state()
    assert len(state.records) == 2
    assert isinstance(state.records[0], StorageObservabilityRecordV1)
    assert isinstance(state.records[1], StorageObservabilityRecordV3)


@pytest.mark.parametrize("fault", ["append", "verify_after_append"])
def test_terminal_append_retry_reuses_one_frozen_candidate(
    tmp_path: Path,
    fault: str,
) -> None:
    class FaultOnceCollector(StorageObservabilityCollector):
        append_calls = 0
        verify_calls = 0

        def _append_line(self, record) -> None:  # type: ignore[no-untyped-def]
            self.append_calls += 1
            if fault == "append" and self.append_calls == 1:
                raise StorageObservabilityError(
                    "simulated append failure", reason_code="artifact_write_failed"
                )
            super()._append_line(record)

        def _verify_append(self, record) -> None:  # type: ignore[no-untyped-def]
            self.verify_calls += 1
            super()._verify_append(record)
            if fault == "verify_after_append" and self.verify_calls == 1:
                raise StorageObservabilityError(
                    "simulated post-append verification failure",
                    reason_code="artifact_invalid",
                )

    collector = FaultOnceCollector(
        state_root=tmp_path / "state",
        database_path=tmp_path / "missing.duckdb",
        clock=lambda: _BASE,
    )
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    observation = _observation()

    with pytest.raises(StorageObservabilityError):
        collector.complete_attempt(handle, observation, job_execution_ms=0)
    candidate = handle.terminal_record
    assert candidate is not None
    assert handle.completed is False
    artifact_lines = (
        collector.artifact_path.read_text(encoding="utf-8").splitlines()
        if collector.artifact_path.exists()
        else []
    )
    assert len(artifact_lines) == (1 if fault == "verify_after_append" else 0)

    record = collector.complete_attempt(
        handle,
        observation,
        execution_completed_at=_BASE + timedelta(seconds=30),
        result_persisted_at=_BASE + timedelta(seconds=31),
        job_execution_ms=29_000,
    )

    assert record == candidate
    assert handle.completed is True
    assert len(collector.state().records) == 1
    assert collector.state().records[0].to_json_dict() == candidate.to_json_dict()
    assert collector.append_calls == (1 if fault == "verify_after_append" else 2)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5"])
def test_measurement_timeout_must_be_a_finite_positive_number(
    tmp_path: Path, timeout: object
) -> None:
    with pytest.raises(ValueError, match="measurement timeout"):
        StorageObservabilityCollector(
            state_root=tmp_path / "state",
            database_path=tmp_path / "missing.duckdb",
            measurement_timeout_seconds=timeout,  # type: ignore[arg-type]
        )


def test_measurement_deadline_interrupts_only_the_read_connection(monkeypatch) -> None:
    calls: list[str] = []

    class FakeConnection:
        def interrupt(self) -> None:
            calls.append("interrupt")

    class ImmediateTimer:
        def __init__(self, interval, function) -> None:
            assert interval == 0.25
            self.function = function

        def start(self) -> None:
            self.function()

        def cancel(self) -> None:
            calls.append("cancel")

    monkeypatch.setattr(storage_observability_module.threading, "Timer", ImmediateTimer)

    with storage_observability_module._measurement_deadline(FakeConnection(), 0.25):
        calls.append("query")

    assert calls == ["interrupt", "query", "cancel"]
