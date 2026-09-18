"""Integration coverage for storage observability in the production composition.

The production composition is driven with only three substitutions: the loopback HTTP server is
replaced by a stub that never binds a port, the readiness probe is neutralized, and the watchlist
job factory returns one registered job whose callable never touches a provider. Everything else,
including the real controller, the real monitors, the real scheduler and the real collector, is
the production code under test.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb
import pytest

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
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.application.scheduled_observers import ScheduledJobObserverChain
from investment_analyst.application.storage_observability import (
    StorageObservabilityCollector,
    StorageObservabilityError,
    StorageObservationHandle,
    parse_storage_observability_state,
    storage_observability_artifact_path,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import StoragePaths
from investment_analyst.workspace.service import WorkspaceService

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "serve_investment_analyst.py"
_ARTIFACT_NAME = "storage_observability_v1.jsonl"
_SCHEDULE_STATE_FILE = "multi_asset_schedule_state_v1.json"
_JOB_ID = "composition:market-daily"
_JOB_TIMEZONE = "America/Lima"
_FIXED_CLOCK = datetime(2026, 9, 18, 12, 30, tzinfo=UTC)
_WALL_CLOCK_INSIDE_THE_GAP = datetime(2026, 9, 18, 6, 0, tzinfo=UTC)
_COLLECTOR_WIRED = "wired"
_COLLECTOR_ABSENT = "absent"
_COLLECTOR_FAILING = "failing"
_ALPACA_CREDENTIALS = AlpacaCredentials(api_key="composition", secret_key="composition")
_SEC_IDENTITY = SecEdgarIdentity("investment-analyst composition@example.invalid")
_EXPECTED_OPTIONS = (
    "--workspace",
    "--port",
    "--no-scheduler",
    "--schedule-at",
    "--timezone",
    "--market-start",
    "--market-end-lag-days",
    "--fundamental-frequency",
    "--refresh-mode",
    "--allow-partial",
    "--schedule-asset",
    "--no-schedule-intraday",
    "--no-schedule-smv",
    "--no-schedule-macro",
    "--memory-ceiling-mb",
)
_EXPECTED_DESTINATIONS = (
    "allow_partial",
    "fundamental_frequency",
    "market_end_lag_days",
    "market_start",
    "memory_ceiling_mb",
    "no_schedule_intraday",
    "no_schedule_macro",
    "no_schedule_smv",
    "no_scheduler",
    "port",
    "refresh_mode",
    "schedule_asset",
    "schedule_at",
    "timezone",
    "workspace",
)


@lru_cache(maxsize=1)
def _script_module() -> ModuleType:
    """Load the production composition module once for the whole test module."""
    spec = importlib.util.spec_from_file_location("serve_investment_analyst_composition", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("the production composition script could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FailingCollector(StorageObservabilityCollector):
    """Instrument that cannot open its measurement window, as a faulted one would not."""

    def begin_attempt(self, *, job_id: str, attempt_id: UUID) -> StorageObservationHandle:
        raise StorageObservabilityError("simulated observability failure")


class _StubServer:
    """Loopback server replacement that never binds a socket or serves a request."""

    def __init__(self, address: tuple[str, int], application: object) -> None:
        self.address = address
        self.application = application

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        """Return immediately so the composed startup sequence completes."""
        return None

    def shutdown(self) -> None:
        """Accept the composition's shutdown call without side effects."""
        return None

    def server_close(self) -> None:
        """Accept the composition's close call without side effects."""
        return None


@dataclass(frozen=True)
class _Composed:
    """One production composition executed against an isolated temporary workspace."""

    workspace_root: Path
    storage_root: Path
    state_root: Path
    database_path: Path
    exit_code: int
    collector_calls: tuple[dict[str, object], ...]
    collectors: tuple[StorageObservabilityCollector | None, ...]
    scheduler_args: tuple[tuple[object, ...], ...]
    scheduler_kwargs: tuple[dict[str, object], ...]
    schedulers: tuple[MultiAssetScheduler, ...]
    job_runs: list[str]

    @property
    def artifact_path(self) -> Path:
        """Return the declared bounded artifact location under the composed state root."""
        return self.state_root / _ARTIFACT_NAME

    def scheduler(self) -> MultiAssetScheduler:
        """Return the single composed scheduler of this composition."""
        assert len(self.schedulers) == 1
        return self.schedulers[0]

    def records(self) -> tuple[dict[str, object], ...]:
        """Parse every persisted observability line of this composition."""
        return tuple(
            json.loads(line) for line in self.artifact_path.read_text(encoding="utf-8").splitlines()
        )


def _definition() -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=_JOB_ID,
        asset_id="equity:test:composition",
        provider="test-provider",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=3,
        retry_backoff_seconds=60,
    )


def _compose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    collector_mode: str = _COLLECTOR_WIRED,
    no_scheduler: bool = False,
) -> _Composed:
    """Drive the production composition with an isolated stub job and no provider access."""
    module = _script_module()
    paths = (
        WorkspaceService(environ={}, home=tmp_path / "home")
        .initialize(tmp_path / "workspace")
        .paths
    )
    collector_calls: list[dict[str, object]] = []
    collectors: list[StorageObservabilityCollector | None] = []
    scheduler_args: list[tuple[object, ...]] = []
    scheduler_kwargs: list[dict[str, object]] = []
    schedulers: list[MultiAssetScheduler] = []
    job_runs: list[str] = []

    def _collector(**arguments: object) -> StorageObservabilityCollector | None:
        collector_calls.append(dict(arguments))
        if collector_mode == _COLLECTOR_ABSENT:
            collectors.append(None)
            return None
        instance: StorageObservabilityCollector = (
            _FailingCollector(**arguments)  # type: ignore[arg-type]
            if collector_mode == _COLLECTOR_FAILING
            else StorageObservabilityCollector(**arguments)  # type: ignore[arg-type]
        )
        collectors.append(instance)
        return instance

    class _RecordingScheduler(MultiAssetScheduler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            scheduler_args.append(args)
            scheduler_kwargs.append(dict(kwargs))
            schedulers.append(self)
            super().__init__(*args, clock=lambda: _FIXED_CLOCK, **kwargs)  # type: ignore[arg-type]

        def run_forever(
            self,
            stop_event: object,
            *,
            poll_seconds: float = 30.0,
            error_handler: object = None,
        ) -> None:
            del poll_seconds, error_handler

    def _run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        job_runs.append(invocation.definition.job_id)
        return ScheduledJobExecution(
            job_id=invocation.definition.job_id,
            effective_known_at=invocation.started_at,
            evidence_changed=True,
            source_ids=(f"source:{invocation.definition.job_id}",),
            created_count=1,
            reused_count=0,
        )

    def _jobs(
        controller: object, universe: object, config: object
    ) -> tuple[RegisteredScheduledJob, ...]:
        del controller, universe, config
        return (RegisteredScheduledJob(_definition(), _run),)

    monkeypatch.setattr(module, "AaplLocalHttpServer", _StubServer)
    monkeypatch.setattr(module, "wait_for_overview_ready", lambda port: None)
    monkeypatch.setattr(module, "notify_ready", lambda: None)
    monkeypatch.setattr(module, "build_local_watchlist_jobs", _jobs)
    monkeypatch.setattr(module, "MultiAssetScheduler", _RecordingScheduler)
    monkeypatch.setattr(module, "StorageObservabilityCollector", _collector)

    arguments = module._parser().parse_args(
        ["--workspace", str(paths.root)] + (["--no-scheduler"] if no_scheduler else [])
    )
    exit_code = module._serve_after_lock(
        arguments,
        (_ALPACA_CREDENTIALS, _SEC_IDENTITY, None),
        ApplicationRuntime.create_default(),
        workspace_root=paths.root,
        storage_root=paths.storage_root,
        state_root=paths.state_root,
    )
    return _Composed(
        workspace_root=paths.root,
        storage_root=paths.storage_root,
        state_root=paths.state_root,
        database_path=StoragePaths.from_root(paths.storage_root).database_path,
        exit_code=exit_code,
        collector_calls=tuple(collector_calls),
        collectors=tuple(collectors),
        scheduler_args=tuple(scheduler_args),
        scheduler_kwargs=tuple(scheduler_kwargs),
        schedulers=tuple(schedulers),
        job_runs=job_runs,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_files(root: Path) -> set[str]:
    return {str(item.relative_to(root)) for item in root.rglob("*") if item.is_file()}


def _document_table_names(database_path: Path) -> tuple[str, ...]:
    """List the document tables through an independent read-only engine."""
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT table_name FROM information_schema.columns"
            " WHERE column_name = 'document_json' ORDER BY table_name"
        ).fetchall()
    finally:
        connection.close()
    return tuple(str(row[0]) for row in rows)


def test_composition_injects_the_collector_into_the_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composed = _compose(tmp_path, monkeypatch)

    assert composed.exit_code == 0
    assert len(composed.collector_calls) == 1
    assert composed.collector_calls[0] == {
        "state_root": composed.state_root,
        "database_path": composed.database_path,
    }
    collector = composed.collectors[0]
    assert collector is not None
    assert collector.artifact_path == storage_observability_artifact_path(composed.state_root)
    assert collector.artifact_path == composed.artifact_path
    assert collector.artifact_path.is_relative_to(composed.workspace_root)
    assert composed.scheduler_kwargs[0]["storage_observability"] is collector
    assert composed.job_runs == []
    assert not composed.artifact_path.exists()


def test_running_a_job_writes_one_observability_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composed = _compose(tmp_path, monkeypatch)

    completed = composed.scheduler().tick()

    assert [item.status for item in completed] == [ScheduledJobAttemptStatus.SUCCEEDED]
    assert composed.job_runs == [_JOB_ID]
    database_bytes = composed.database_path.stat().st_size
    assert database_bytes > 0
    records = composed.records()
    assert len(records) == 1
    record = records[0]
    assert record["schema_version"] == "storage-observability-v1"
    assert record["job_id"] == _JOB_ID
    assert record["database_bytes_before"] == database_bytes
    assert record["database_bytes_after"] == database_bytes
    assert composed.artifact_path.read_text(encoding="utf-8").endswith("\n")


def test_written_record_parses_and_correlates_by_attempt_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composed = _compose(tmp_path, monkeypatch)

    completed = composed.scheduler().tick()

    attempt = completed[0]
    state = parse_storage_observability_state(composed.artifact_path.read_text(encoding="utf-8"))
    assert len(state.records) == 1
    record = state.records[0]
    assert record.attempt_id == attempt.attempt_id
    assert record.job_id == attempt.definition.job_id
    assert record.attempt_number == attempt.attempt_number
    assert record.local_date == attempt.local_date
    assert record.attempt_status == attempt.status.value
    assert record.rows_created == attempt.execution.created_count  # type: ignore[union-attr]
    persisted = MultiAssetScheduleStateStore(composed.state_root / _SCHEDULE_STATE_FILE).load()
    assert [item.attempt_id for item in persisted.attempts] == [record.attempt_id]


def test_scheduler_without_collector_remains_valid_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    absent = _compose(tmp_path / "absent", monkeypatch, collector_mode=_COLLECTOR_ABSENT)

    completed = absent.scheduler().tick()

    assert [item.status for item in completed] == [ScheduledJobAttemptStatus.SUCCEEDED]
    assert absent.job_runs == [_JOB_ID]
    assert absent.collectors == (None,)
    assert not absent.artifact_path.exists()
    assert not any("storage observability" in issue for issue in absent.scheduler().status().issues)

    unscheduled = _compose(tmp_path / "unscheduled", monkeypatch, no_scheduler=True)

    assert unscheduled.exit_code == 0
    assert unscheduled.schedulers == ()
    assert unscheduled.collector_calls == ()
    assert not unscheduled.artifact_path.exists()
    assert tuple(unscheduled.workspace_root.rglob(_ARTIFACT_NAME)) == ()


def test_collector_failure_does_not_block_startup_or_alter_the_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _compose(tmp_path / "control", monkeypatch, collector_mode=_COLLECTOR_ABSENT)
    failing = _compose(tmp_path / "failing", monkeypatch, collector_mode=_COLLECTOR_FAILING)

    control_attempt = control.scheduler().tick()[0]
    failing_attempt = failing.scheduler().tick()[0]

    assert failing.exit_code == 0
    assert failing.job_runs == [_JOB_ID]
    assert failing_attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
    assert failing_attempt.failure is None
    assert failing_attempt.definition == control_attempt.definition
    assert failing_attempt.status is control_attempt.status
    assert failing_attempt.execution is not None
    assert control_attempt.execution is not None
    assert failing_attempt.execution.model_dump(
        exclude={"effective_known_at"}
    ) == control_attempt.execution.model_dump(exclude={"effective_known_at"})
    assert not failing.artifact_path.exists()
    assert "storage observability could not open its measurement" in (
        failing.scheduler().status().issues
    )
    assert not any(
        "storage observability" in issue for issue in control.scheduler().status().issues
    )


def test_public_cli_arguments_are_unchanged() -> None:
    parser = _script_module()._parser()

    declared = tuple(
        action.option_strings[0]
        for action in parser._actions
        if action.option_strings and action.dest != "help"
    )

    assert declared == _EXPECTED_OPTIONS
    assert tuple(sorted(vars(parser.parse_args([])))) == _EXPECTED_DESTINATIONS
    assert parser.parse_args([]).port == 8765
    assert parser.parse_args([]).memory_ceiling_mb is None


def test_collector_uses_read_only_engine_and_a_single_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without = _compose(tmp_path / "without", monkeypatch, collector_mode=_COLLECTOR_ABSENT)
    observed = _compose(tmp_path / "observed", monkeypatch)

    without.scheduler().tick()
    database_hash = _sha256(observed.database_path)
    observed.scheduler().tick()

    assert _sha256(observed.database_path) == database_hash
    assert not Path(f"{observed.database_path}.wal").exists()
    assert _relative_files(observed.workspace_root) - _relative_files(without.workspace_root) == {
        f"state/{_ARTIFACT_NAME}"
    }
    record = observed.records()[0]
    assert {item["table_name"] for item in record["table_bytes"]} == set(  # type: ignore[union-attr]
        _document_table_names(observed.database_path)
    )
    assert record["wal_bytes_before"] == record["wal_bytes_after"] == 0


def test_composition_order_and_scheduler_observers_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composed = _compose(tmp_path, monkeypatch)

    jobs, store = composed.scheduler_args[0]
    assert tuple(item.definition.job_id for item in jobs) == (_JOB_ID,)  # type: ignore[union-attr]
    assert isinstance(store, MultiAssetScheduleStateStore)
    assert store.load().attempts == ()
    kwargs = composed.scheduler_kwargs[0]
    assert tuple(sorted(kwargs)) == (
        "memory_ceiling_bytes",
        "observer",
        "storage_observability",
    )
    assert kwargs["memory_ceiling_bytes"] is None
    observer = kwargs["observer"]
    assert isinstance(observer, ScheduledJobObserverChain)
    assert tuple(type(item).__name__ for item in observer._observers) == (
        "OperationalAlertMonitor",
        "AnalyticalScreeningMonitor",
        "CandidateNotificationMonitor",
    )

    composed.scheduler().tick()

    assert (composed.state_root / _SCHEDULE_STATE_FILE).is_file()
    assert [item.definition.job_id for item in store.load().attempts] == [_JOB_ID]


def test_wiring_creates_no_duckdb_object_or_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composed = _compose(tmp_path, monkeypatch)
    tables_before = _document_table_names(composed.database_path)
    database_hash_before = _sha256(composed.database_path)

    composed.scheduler().tick()

    assert tables_before
    assert _document_table_names(composed.database_path) == tables_before
    assert _sha256(composed.database_path) == database_hash_before
    assert not Path(f"{composed.database_path}.wal").exists()
    assert (
        _ROOT / "src" / "investment_analyst" / "storage" / "migrations" / "001_initial.sql"
    ).is_file()
    assert not tuple(composed.workspace_root.rglob("*.sql"))


class _WallClockMeta(type):
    """Keep isinstance checks and delegate every other datetime attribute to the real class."""

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)

    def __getattr__(cls, name: str) -> object:
        return getattr(datetime, name)


class _WallClockDatetime(metaclass=_WallClockMeta):
    """datetime stand-in whose now() is fixed inside the 05:00-12:00 UTC gap."""

    @staticmethod
    def now(tz: object = None) -> datetime:
        del tz
        return _WALL_CLOCK_INSIDE_THE_GAP


def test_composition_is_independent_of_the_wall_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A12: a wall clock inside the 05:00-12:00 UTC gap no longer changes the outcome."""
    inside_gap = _WALL_CLOCK_INSIDE_THE_GAP
    local = inside_gap.astimezone(ZoneInfo(_JOB_TIMEZONE))

    assert 5 <= inside_gap.hour < 12
    assert local.hour < 7, "the registered job is not due under this wall clock"

    monkeypatch.setattr(
        "investment_analyst.application.multi_asset_scheduler.datetime",
        _WallClockDatetime,
    )
    composed = _compose(tmp_path, monkeypatch)
    completed = composed.scheduler().tick()

    assert [item.status for item in completed] == [ScheduledJobAttemptStatus.SUCCEEDED]
    assert composed.job_runs == [_JOB_ID]
    assert composed.records()[0]["job_id"] == _JOB_ID


def test_artifact_path_is_independent_of_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = _compose(tmp_path / "first", monkeypatch)
    first.scheduler().tick()

    monkeypatch.chdir(second_cwd)
    second = _compose(tmp_path / "second", monkeypatch)
    second.scheduler().tick()

    for composed, cwd in ((first, first_cwd), (second, second_cwd)):
        assert composed.artifact_path == composed.state_root / _ARTIFACT_NAME
        assert composed.artifact_path.is_absolute()
        assert composed.artifact_path.is_file()
        assert len(composed.records()) == 1
        assert not (cwd / "state").exists()
        assert tuple(cwd.rglob(_ARTIFACT_NAME)) == ()
