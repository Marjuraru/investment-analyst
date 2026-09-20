"""Tests for additive per-attempt storage observability."""

import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

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
    StorageObservabilityGrowthClassification,
    StorageObservabilityRecord,
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


def _record(root: Path, clock: _ScriptedClock | None = None) -> StorageObservabilityRecord:
    collector = _collector(root, clock=clock)
    handle = collector.begin_attempt(job_id=_JOB_ID, attempt_id=_ATTEMPT_ID)
    return collector.complete_attempt(handle, _observation())


def test_storage_observability_contract_is_frozen_and_versioned(tmp_path: Path) -> None:
    record = _record(tmp_path)

    assert record.schema_version == "storage-observability-v1"
    assert StorageObservabilityRecord.model_config["frozen"] is True
    assert StorageObservabilityRecord.model_config["extra"] == "forbid"
    assert StorageObservabilityDurations.model_config["frozen"] is True
    assert StorageObservabilityState.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        record.attempt_status = "changed"
    with pytest.raises(ValidationError):
        StorageObservabilityRecord(**{**record.model_dump(), "unexpected_field": 1})
    with pytest.raises(ValidationError):
        StorageObservabilityRecord(
            **{**record.model_dump(), "schema_version": "storage-observability-v2"}
        )

    for model in (
        ScheduledJobObservation,
        StorageObservabilityDurations,
        StorageObservabilityGrowthClassification,
        StorageObservabilityRecord,
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
    assert durations.model_dump() == {
        "total_ms": 7000,
        "network_ms": 1000,
        "query_ms": 3000,
        "calculation_ms": 1000,
        "persistence_ms": 1000,
        "verification_ms": 1000,
    }
    assert (
        durations.network_ms
        + durations.query_ms
        + durations.calculation_ms
        + durations.persistence_ms
        + durations.verification_ms
        == durations.total_ms
    )
    with pytest.raises(ValidationError, match="reconcile"):
        StorageObservabilityDurations(
            total_ms=10,
            network_ms=1,
            query_ms=1,
            calculation_ms=1,
            persistence_ms=1,
            verification_ms=1,
        )
    with pytest.raises(ValidationError, match="reconcile"):
        StorageObservabilityDurations(
            total_ms=9,
            network_ms=1,
            query_ms=1,
            calculation_ms=1,
            persistence_ms=1,
            verification_ms=1,
        )


def test_logical_bytes_by_table_measured_read_only(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    original = database.read_bytes()

    record = _record(tmp_path)

    measured = {item.table_name: item for item in record.table_bytes}
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
    assert {item.table_name: item.row_count for item in record.table_bytes} == {
        "assets": 1,
        "diagnostic_results": 0,
        "metric_results": 1,
        "normalized_observations": 0,
        "raw_record_index": 2,
    }


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
    assert record.collector_overhead_ms == 6000
    assert durations.network_ms == 1000
    assert durations.total_ms == 7000
    assert record.collector_overhead_ms + durations.network_ms == durations.total_ms
    assert record.collector_overhead_ms == (
        durations.query_ms
        + durations.calculation_ms
        + durations.persistence_ms
        + durations.verification_ms
    )
    assert "collector_overhead_ms" in record.to_json_dict()
    with pytest.raises(ValidationError, match="separate"):
        StorageObservabilityRecord(**{**record.model_dump(), "collector_overhead_ms": 5000})


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
        StorageObservabilityRecord(
            **{
                **record.model_dump(),
                "evidence_changed": None,
                "rows_created": None,
                "rows_reused": None,
            }
        )


def test_daily_snapshot_is_compact_and_bounded(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    clock = _ScriptedClock(_BASE)
    collector = _collector(tmp_path, database_path=database, clock=clock)
    artifact = collector.artifact_path

    def collect(day_offset: int, attempt: int) -> None:
        moment = _BASE + timedelta(days=day_offset)
        clock.advance_to(moment)
        handle = collector.begin_attempt(
            job_id=_JOB_ID,
            attempt_id=UUID(int=day_offset * 10 + attempt),
        )
        collector.complete_attempt(
            handle,
            _observation(local_date=moment.date(), attempt_id=UUID(int=day_offset * 10 + attempt)),
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
    assert summary.total_ms == 14000

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

    assert "available_at" not in StorageObservabilityRecord.model_fields
    assert "known_at" not in StorageObservabilityRecord.model_fields
    assert "available_at" not in payload
    assert payload["schema_version"] == "storage-observability-v1"
    assert record.schema_version == "storage-observability-v1"
    assert tuple(StorageObservabilityRecord.model_fields) == (
        "schema_version",
        "observed_at",
        "attempt_id",
        "job_id",
        "attempt_number",
        "local_date",
        "attempt_status",
        "evidence_changed",
        "rows_created",
        "rows_reused",
        "database_bytes_before",
        "database_bytes_after",
        "wal_bytes_before",
        "wal_bytes_after",
        "table_bytes",
        "growth",
        "collector_overhead_ms",
        "durations",
    )
    assert "storage" not in (tmp_path / "state").parts
    assert "storage_observability" in StorageObservabilityRecord.__module__


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
    assert {type(item.row_count) for item in record.table_bytes} == {int}
    with pytest.raises(ValidationError):
        StorageObservabilityTableBytes(table_name="metric_results", row_count=1.5, document_bytes=1)
    with pytest.raises(ValidationError):
        StorageObservabilityTableBytes.model_validate(
            {"table_name": "metric_results", "row_count": 1, "document_bytes": 1.5}
        )


def test_collector_uses_a_single_writer_and_read_only_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database_path(tmp_path)
    _create_database(database)
    original = database.read_bytes()
    opened: list[bool] = []
    real_connect = duckdb.connect

    def tracking_connect(database_path: str, *args: object, **kwargs: object):
        opened.append(bool(kwargs.get("read_only", False)))
        return real_connect(database_path, *args, **kwargs)

    monkeypatch.setattr(duckdb, "connect", tracking_connect)

    record = _record(tmp_path)

    assert opened == [True, True]
    assert database.read_bytes() == original
    assert record.table_bytes
    assert sorted(item.name for item in (tmp_path / "state").iterdir()) == [_ARTIFACT_NAME]


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
        (blocked, "blocked", "storage observability could not open its measurement"),
        (unwritable, "unwritable", "storage observability could not record its result"),
    )
    for collector, label, expected_issue in scenarios:
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
        assert scheduler.status().issues == (expected_issue,)
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


def test_document_bytes_are_measured_at_most_once_per_utc_day(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_classification_database(database)
    clock = _ScriptedClock(_BASE)
    collector = _collector(tmp_path, database_path=database, clock=clock)

    # Attempt 1 on day 1: document bytes are measured
    id1 = UUID("00000000-0000-4000-8000-000000000001")
    handle1 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id1)
    record1 = collector.complete_attempt(
        handle1,
        _observation(attempt_id=id1, evidence_changed=True, rows_created=1, rows_reused=0),
    )
    assert len(record1.table_bytes) > 0

    # Attempt 2 on day 1 (same UTC day): document bytes are skipped, table_bytes is empty
    id2 = UUID("00000000-0000-4000-8000-000000000002")
    handle2 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id2)
    record2 = collector.complete_attempt(
        handle2,
        _observation(attempt_id=id2, evidence_changed=False, rows_created=0, rows_reused=1),
    )
    assert record2.table_bytes == ()

    # Attempt 3 on day 1 (same UTC day): document bytes are skipped again
    id3 = UUID("00000000-0000-4000-8000-000000000003")
    handle3 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id3)
    record3 = collector.complete_attempt(
        handle3,
        _observation(attempt_id=id3, evidence_changed=False, rows_created=0, rows_reused=1),
    )
    assert record3.table_bytes == ()

    # Attempt 4 on day 2 (next UTC day): document bytes are measured again
    next_day = _BASE + timedelta(days=1)
    clock.advance_to(next_day)
    id4 = UUID("00000000-0000-4000-8000-000000000004")
    handle4 = collector.begin_attempt(job_id=_JOB_ID, attempt_id=id4)
    record4 = collector.complete_attempt(
        handle4,
        _observation(
            attempt_id=id4,
            evidence_changed=False,
            rows_created=0,
            rows_reused=1,
            local_date=next_day.date(),
        ),
    )
    assert len(record4.table_bytes) > 0


def test_growth_classification_uses_row_counts_on_every_attempt(tmp_path: Path) -> None:
    database = _database_path(tmp_path)
    _create_classification_database(database)
    collector = _collector(tmp_path)

    # Attempt 1: first attempt of day, table_bytes is measured
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
    assert record1.table_bytes != ()
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
