"""Integration coverage for bounded analytical metric-result queries."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.alerts.analytical_backtest import (
    AnalyticalBacktestRequest,
    AnalyticalBacktestResult,
    AnalyticalBacktestService,
)
from investment_analyst.alerts.analytical_monitor import AnalyticalScreeningMonitor
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    INITIAL_MARKET_ACTIVITY_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import AnalyticalRuleRegistryStore
from investment_analyst.alerts.analytical_state import (
    AnalyticalScreeningState,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.storage.repositories import DuckDBMetricResultRepository
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspacePaths
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_METRIC_KEY = "market.history.relative_volume"
_METRIC_ALGORITHM = "market-relative-volume-v1-decimal34"


def _metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    known_at: datetime,
    metric_key: str = _METRIC_KEY,
) -> MetricResult:
    is_relevant = metric_key == _METRIC_KEY
    return MetricResult(
        result_id=UUID(f"70000000-0000-4000-8000-{identifier:012d}"),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters=(
            {
                "source_id": _SOURCE_ID,
                "known_at": known_at.isoformat(),
                "window": 20,
            }
            if is_relevant
            else {"source_id": _SOURCE_ID, "kind": "noise"}
        ),
        input_observation_ids=[UUID(f"71000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=_METRIC_ALGORITHM if is_relevant else "unused-v1",
        quality=DataQuality.PARTIAL,
    )


def _seed(
    root: Path,
    metrics: tuple[MetricResult, ...],
) -> tuple[WorkspaceService, WorkspacePaths, ApplicationRuntime]:
    service = WorkspaceService(environ={}, home=root / "home")
    paths = service.initialize(root / "workspace").paths
    storage = service.open_storage(paths, WorkspaceAccessMode.READ_WRITE)
    try:
        for metric in metrics:
            storage.metric_results.save(metric)
    finally:
        storage.close()
    return service, paths, ApplicationRuntime.create_default(workspace_service=service)


def _attempt(known_at: datetime) -> ScheduledJobAttempt:
    definition = ScheduledJobDefinition(
        job_id="alpaca:equity:us:aapl:market-daily",
        asset_id=_ASSET_ID,
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
    )
    return ScheduledJobAttempt(
        attempt_id=UUID("72000000-0000-4000-8000-000000000001"),
        definition=definition,
        local_date=date(2026, 7, 29),
        scheduled_for=datetime(2026, 7, 29, 12, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        completed_at=datetime(2026, 7, 29, 12, 2, tzinfo=UTC),
        execution=ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=known_at,
            evidence_changed=True,
            source_ids=(_SOURCE_ID,),
            created_count=1,
            reused_count=0,
        ),
    )


def _run_monitor(
    root: Path,
    metrics: tuple[MetricResult, ...],
    monkeypatch: pytest.MonkeyPatch,
    *,
    unbounded: bool,
) -> tuple[AnalyticalScreeningState, int]:
    service, paths, runtime = _seed(root, metrics)
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"confirmations_required": 1})
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    observed_rows: list[int] = []
    original_list = DuckDBMetricResultRepository.list

    def tracked_list(
        repository: DuckDBMetricResultRepository,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]:
        selected_keys = None if unbounded else metric_keys
        results = original_list(
            repository,
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=selected_keys,
            as_of_from=as_of_from,
            as_of_to=as_of_to,
        )
        observed_rows.append(len(results))
        return results

    with monkeypatch.context() as patch:
        patch.setattr(DuckDBMetricResultRepository, "list", tracked_list)
        AnalyticalScreeningMonitor(
            store,
            runtime,
            paths.root,
            (rule,),
            clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
        )(_attempt(datetime(2026, 7, 29, 12, tzinfo=UTC)))

    assert len(observed_rows) == 1
    return store.load(), observed_rows[0]


def _run_backtest(
    root: Path,
    metrics: tuple[MetricResult, ...],
    monkeypatch: pytest.MonkeyPatch,
    *,
    unbounded: bool,
) -> tuple[AnalyticalBacktestResult, int]:
    service, paths, runtime = _seed(root, metrics)
    registry = AnalyticalRuleRegistryStore(
        paths.state_root / "rules.json",
        INITIAL_ANALYTICAL_RULES,
    )
    backtest = AnalyticalBacktestService(runtime, paths.root, registry)
    observed_rows: list[int] = []
    original_list = DuckDBMetricResultRepository.list

    def tracked_list(
        repository: DuckDBMetricResultRepository,
        *,
        asset_id: str | None = None,
        metric_key: str | None = None,
        metric_keys: tuple[str, ...] | None = None,
        as_of_from: datetime | None = None,
        as_of_to: datetime | None = None,
    ) -> list[MetricResult]:
        selected_keys = None if unbounded else metric_keys
        results = original_list(
            repository,
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=selected_keys,
            as_of_from=as_of_from,
            as_of_to=as_of_to,
        )
        observed_rows.append(len(results))
        return results

    with monkeypatch.context() as patch:
        patch.setattr(DuckDBMetricResultRepository, "list", tracked_list)
        result = backtest.run(
            AnalyticalBacktestRequest(
                rule_id=INITIAL_MARKET_ACTIVITY_RULE.rule_id,
                asset_id=_ASSET_ID,
                max_cuts=20,
            )
        )

    assert len(observed_rows) == 1
    return result, observed_rows[0]


def test_bounded_monitor_query_reduces_rows_without_changing_screening_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    metrics = (
        _metric(
            "1.8",
            identifier=1,
            as_of=datetime(2026, 7, 28, tzinfo=UTC),
            known_at=known_at,
        ),
        _metric(
            "9.0",
            identifier=2,
            as_of=datetime(2026, 7, 27, tzinfo=UTC),
            known_at=known_at,
            metric_key="market.unused",
        ),
        _metric(
            "8.0",
            identifier=3,
            as_of=datetime(2026, 7, 26, tzinfo=UTC),
            known_at=known_at,
            metric_key="fundamental.unused",
        ),
        _metric(
            "2.0",
            identifier=4,
            as_of=datetime(2026, 7, 29, tzinfo=UTC),
            known_at=known_at + timedelta(days=1),
        ),
    )

    baseline, baseline_rows = _run_monitor(
        tmp_path / "monitor-baseline",
        metrics,
        monkeypatch,
        unbounded=True,
    )
    bounded, bounded_rows = _run_monitor(
        tmp_path / "monitor-bounded",
        metrics,
        monkeypatch,
        unbounded=False,
    )

    assert bounded_rows < baseline_rows
    assert bounded_rows == 2
    assert bounded == baseline
    assert bounded.results[0].as_of == datetime(2026, 7, 28, tzinfo=UTC)


def test_bounded_backtest_query_reduces_rows_without_changing_cuts_or_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    metrics = tuple(
        metric
        for index, value in enumerate(("1.6", "1.7", "1.3", "1.1"), start=1)
        for metric in (
            _metric(
                value,
                identifier=index,
                as_of=start + timedelta(days=index - 1),
                known_at=start + timedelta(days=index - 1, hours=12),
            ),
            _metric(
                "8.0",
                identifier=index + 10,
                as_of=start + timedelta(days=index - 1),
                known_at=start + timedelta(days=index - 1, hours=12),
                metric_key=f"unused.metric.{index}",
            ),
        )
    )

    baseline, baseline_rows = _run_backtest(
        tmp_path / "backtest-baseline",
        metrics,
        monkeypatch,
        unbounded=True,
    )
    bounded, bounded_rows = _run_backtest(
        tmp_path / "backtest-bounded",
        metrics,
        monkeypatch,
        unbounded=False,
    )

    assert bounded_rows < baseline_rows
    assert bounded_rows == 4
    assert bounded == baseline
