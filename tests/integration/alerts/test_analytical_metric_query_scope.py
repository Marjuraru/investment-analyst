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
from investment_analyst.alerts.analytical_models import AnalyticalScreeningRule
from investment_analyst.alerts.analytical_monitor import AnalyticalScreeningMonitor
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    INITIAL_ANNUAL_FUNDAMENTAL_RULE,
    INITIAL_MARKET_ACTIVITY_RULE,
    MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE,
    RSI_INCREMENTAL_LOW_REVIEW_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import AnalyticalRuleRegistryStore
from investment_analyst.alerts.analytical_state import (
    AnalyticalScreeningState,
    AnalyticalScreeningStateStore,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_v2
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
_FUNDAMENTAL_SOURCE_ID = "sec-edgar:aapl:companyfacts"
_FUNDAMENTAL_METRICS = {
    "fundamental.liabilities_to_assets": "sec-fundamental-liabilities-to-assets-v1-decimal34",
    "fundamental.net_margin": "sec-fundamental-net-margin-v1-decimal34",
    "fundamental.revenue_yoy_growth": "sec-fundamental-revenue-yoy-growth-v1-decimal34",
}
_RSI_KEY = "market.technical.rsi"
_MACD_HISTOGRAM_KEY = "market.technical.macd.histogram"
_RSI_LEGACY_ALGORITHM = "market-rsi-wilder-v1-decimal34"
_RSI_INCREMENTAL_ALGORITHM = "market-rsi-incremental-v2-decimal34"
_MACD_LEGACY_ALGORITHM = "market-macd-v1-decimal34"
_MACD_INCREMENTAL_ALGORITHM = "market-macd-incremental-v2-decimal34"


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


def _v2_metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    known_at: datetime,
    metric_key: str = _METRIC_KEY,
) -> MetricResult:
    """Build a v2 market row: semantic identity without the legacy known_at parameter."""
    is_relevant = metric_key == _METRIC_KEY
    input_observation_ids = [UUID(f"71000000-0000-4000-8000-{identifier:012d}")]
    parameters: dict[str, object] = (
        {"source_id": _SOURCE_ID, "window": 20}
        if is_relevant
        else {"source_id": _SOURCE_ID, "kind": "noise"}
    )
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=_ASSET_ID,
            metric_key=metric_key,
            input_observation_ids=input_observation_ids,
            algorithm_version=_METRIC_ALGORITHM if is_relevant else "unused-v1",
            as_of=as_of,
            available_at=known_at,
            unit="ratio",
            quality=DataQuality.PARTIAL,
            parameters=parameters,
        ),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters=parameters,
        input_observation_ids=input_observation_ids,
        algorithm_version=_METRIC_ALGORITHM if is_relevant else "unused-v1",
        quality=DataQuality.PARTIAL,
    )


def _technical_scope_metric(
    value: str,
    *,
    identifier: int,
    metric_key: str,
    algorithm_version: str,
    unit: str,
    parameters: dict[str, object],
    version_two: bool,
    as_of: datetime,
    known_at: datetime,
) -> MetricResult:
    input_observation_ids = [UUID(f"73000000-0000-4000-8000-{identifier:012d}")]
    if version_two:
        scoped_parameters = {"source_id": _SOURCE_ID, **parameters}
        result_id = metric_result_id_v2(
            asset_id=_ASSET_ID,
            metric_key=metric_key,
            input_observation_ids=input_observation_ids,
            algorithm_version=algorithm_version,
            as_of=as_of,
            available_at=known_at,
            unit=unit,
            quality=DataQuality.PARTIAL,
            parameters=scoped_parameters,
        )
    else:
        scoped_parameters = {
            "source_id": _SOURCE_ID,
            "known_at": known_at.isoformat(),
            **parameters,
        }
        result_id = UUID(f"70000000-0000-4000-8000-{identifier:012d}")
    return MetricResult(
        result_id=result_id,
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters=scoped_parameters,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
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


def _fundamental_metric(
    metric_key: str,
    value: str,
    *,
    identifier: int,
    frequency: str = "annual",
    source_id: str = _FUNDAMENTAL_SOURCE_ID,
    asset_id: str = _ASSET_ID,
) -> MetricResult:
    as_of = datetime(2025, 12, 31, tzinfo=UTC)
    available_at = datetime(2026, 2, 15, 12, tzinfo=UTC)
    return MetricResult(
        result_id=UUID(f"85000000-0000-4000-8000-{identifier:012d}"),
        asset_id=asset_id,
        metric_key=metric_key,
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters={"source_id": source_id, "frequency": frequency},
        input_observation_ids=[UUID(f"86000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=_FUNDAMENTAL_METRICS.get(metric_key, "unused-fundamental-v1"),
        quality=DataQuality.VALID,
    )


def _fundamental_corpus(*, foreign_assets: int) -> tuple[MetricResult, ...]:
    in_scope = (
        _fundamental_metric("fundamental.liabilities_to_assets", "0.5", identifier=1),
        _fundamental_metric("fundamental.net_margin", "0.2", identifier=2),
        _fundamental_metric("fundamental.revenue_yoy_growth", "0.1", identifier=3),
    )
    foreign_asset_rows = tuple(
        metric
        for index in range(foreign_assets)
        for metric in (
            _fundamental_metric(
                "fundamental.liabilities_to_assets",
                "0.9",
                identifier=10 + index * 3,
                asset_id=f"equity:us:foreign{index}",
            ),
            _fundamental_metric(
                "fundamental.net_margin",
                "0.9",
                identifier=11 + index * 3,
                asset_id=f"equity:us:foreign{index}",
            ),
            _fundamental_metric(
                "fundamental.revenue_yoy_growth",
                "0.9",
                identifier=12 + index * 3,
                asset_id=f"equity:us:foreign{index}",
            ),
        )
    )
    foreign_source = tuple(
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.9",
            identifier=100 + index,
            source_id=f"sec-edgar:foreign{index}:companyfacts",
        )
        for index in range(3)
    )
    foreign_frequency = tuple(
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.9",
            identifier=200 + index,
            frequency="quarterly",
        )
        for index in range(3)
    )
    unrelated_key = (_fundamental_metric("fundamental.asset_turnover", "9", identifier=300),)
    later_cut = (
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            "0.9",
            identifier=400,
        ).model_copy(
            update={
                "available_at": datetime(2026, 9, 2, tzinfo=UTC),
                "computed_at": datetime(2026, 9, 2, tzinfo=UTC),
            }
        ),
    )
    return (
        *in_scope,
        *foreign_asset_rows,
        *foreign_source,
        *foreign_frequency,
        *unrelated_key,
        *later_cut,
    )


def _fundamental_attempt(known_at: datetime, *, attempt_id: UUID) -> ScheduledJobAttempt:
    definition = ScheduledJobDefinition(
        job_id="sec:equity:us:aapl:fundamentals-annual",
        asset_id=_ASSET_ID,
        provider="sec-edgar",
        domain=ScheduledJobDomain.FUNDAMENTALS,
        data_frequency="annual",
        timezone="America/Lima",
        run_at=time(hour=7),
    )
    return ScheduledJobAttempt(
        attempt_id=attempt_id,
        definition=definition,
        local_date=date(2026, 9, 1),
        scheduled_for=datetime(2026, 9, 1, 12, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        completed_at=datetime(2026, 9, 1, 12, 2, tzinfo=UTC),
        execution=ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=known_at,
            evidence_changed=True,
            source_ids=(_FUNDAMENTAL_SOURCE_ID,),
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


def _run_monitor_catalog(
    root: Path,
    metrics: tuple[MetricResult, ...],
    monkeypatch: pytest.MonkeyPatch,
    *,
    rules: tuple[AnalyticalScreeningRule, ...],
) -> tuple[AnalyticalScreeningState, list[tuple[str, ...] | None], list[int]]:
    service, paths, runtime = _seed(root, metrics)
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    observed_keys: list[tuple[str, ...] | None] = []
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
        observed_keys.append(metric_keys)
        results = original_list(
            repository,
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=metric_keys,
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
            rules,
            clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
        )(_attempt(datetime(2026, 7, 29, 12, tzinfo=UTC)))

    return store.load(), observed_keys, observed_rows


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


def _run_fundamental_scope(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    foreign_assets: int,
) -> tuple[AnalyticalScreeningState, int, list[dict[str, object]], list[tuple[UUID, ...]]]:
    known_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    service, paths, runtime = _seed(root, _fundamental_corpus(foreign_assets=foreign_assets))
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    list_calls = 0
    list_id_calls: list[dict[str, object]] = []
    hydration_runs: list[tuple[UUID, ...]] = []
    original_list = DuckDBMetricResultRepository.list
    original_list_ids = DuckDBMetricResultRepository.list_ids
    original_get_many = DuckDBMetricResultRepository.get_many

    def tracked_list(
        repository: DuckDBMetricResultRepository, **kwargs: object
    ) -> list[MetricResult]:
        nonlocal list_calls
        list_calls += 1
        return original_list(repository, **kwargs)

    def tracked_list_ids(
        repository: DuckDBMetricResultRepository,
        **kwargs: object,
    ) -> list[UUID]:
        list_id_calls.append(dict(kwargs))
        return original_list_ids(repository, **kwargs)

    def tracked_get_many(
        repository: DuckDBMetricResultRepository,
        result_ids: tuple[UUID, ...] | list[UUID],
    ) -> dict[UUID, MetricResult]:
        hydration_runs[-1] = (*hydration_runs[-1], *result_ids)
        return original_get_many(repository, result_ids)

    with monkeypatch.context() as patch:
        patch.setattr(DuckDBMetricResultRepository, "list", tracked_list)
        patch.setattr(DuckDBMetricResultRepository, "list_ids", tracked_list_ids)
        patch.setattr(DuckDBMetricResultRepository, "get_many", tracked_get_many)
        monitor = AnalyticalScreeningMonitor(
            store,
            runtime,
            paths.root,
            (INITIAL_ANNUAL_FUNDAMENTAL_RULE,),
            clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
        )
        for attempt_id in (
            UUID("87000000-0000-4000-8000-000000000001"),
            UUID("87000000-0000-4000-8000-000000000002"),
        ):
            hydration_runs.append(())
            monitor(_fundamental_attempt(known_at, attempt_id=attempt_id))

    return store.load(), list_calls, list_id_calls, hydration_runs


def test_fundamental_query_hydrates_only_scope_across_1x_and_10x_foreign_corpora(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    known_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    expected_ids = {
        UUID("85000000-0000-4000-8000-000000000001"),
        UUID("85000000-0000-4000-8000-000000000002"),
        UUID("85000000-0000-4000-8000-000000000003"),
    }

    baseline, baseline_list_calls, baseline_list_ids, baseline_runs = _run_fundamental_scope(
        tmp_path / "corpus-1x",
        monkeypatch,
        foreign_assets=1,
    )
    scaled, scaled_list_calls, scaled_list_ids, scaled_runs = _run_fundamental_scope(
        tmp_path / "corpus-10x",
        monkeypatch,
        foreign_assets=10,
    )

    expected_call = {
        "asset_id": _ASSET_ID,
        "metric_keys": tuple(sorted(_FUNDAMENTAL_METRICS)),
        "available_to": known_at,
        "parameter_equals": {
            "source_id": _FUNDAMENTAL_SOURCE_ID,
            "frequency": "annual",
        },
    }
    assert baseline_list_calls == scaled_list_calls == 0
    assert baseline_list_ids == [expected_call, expected_call]
    assert scaled_list_ids == [expected_call, expected_call]
    assert tuple(baseline_runs[0]) == tuple(sorted(expected_ids, key=str))
    assert baseline_runs[0] == baseline_runs[1]
    assert scaled_runs[0] == scaled_runs[1] == baseline_runs[0]
    assert all(len(batch) <= 256 for batch in (*baseline_runs, *scaled_runs))
    assert len(baseline.results) == len(scaled.results) == 1
    baseline_result = baseline.results[0]
    scaled_result = scaled.results[0]
    assert baseline_result.result_id == scaled_result.result_id
    assert baseline_result.as_of == scaled_result.as_of == datetime(2025, 12, 31, tzinfo=UTC)
    assert baseline_result.matched is scaled_result.matched is True
    assert baseline_result.conditions[0].observed_value == Decimal("0.5")
    assert {item.metric_result_id for item in baseline_result.conditions} == expected_ids
    assert {receipt.result_ids for receipt in baseline.receipts} == {(baseline_result.result_id,)}


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


def test_bounded_reads_keep_v2_rows_reachable_for_monitor_and_backtest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A8/A9: the bounded projection keeps version-aware v2 rows reachable end to end."""
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    metrics = (
        _v2_metric(
            "1.8",
            identifier=1,
            as_of=datetime(2026, 7, 28, tzinfo=UTC),
            known_at=known_at,
        ),
        _v2_metric(
            "9.0",
            identifier=2,
            as_of=datetime(2026, 7, 27, tzinfo=UTC),
            known_at=known_at,
            metric_key="market.unused",
        ),
    )

    bounded, bounded_rows = _run_monitor(
        tmp_path / "monitor-bounded",
        metrics,
        monkeypatch,
        unbounded=False,
    )
    baseline, baseline_rows = _run_monitor(
        tmp_path / "monitor-unbounded",
        metrics,
        monkeypatch,
        unbounded=True,
    )

    assert bounded_rows == 1 < baseline_rows
    assert bounded == baseline
    assert len(bounded.results) == 1
    assert bounded.results[0].conditions[0].metric_result_id == metrics[0].result_id

    replayed, backtest_rows = _run_backtest(
        tmp_path / "backtest-bounded",
        metrics,
        monkeypatch,
        unbounded=False,
    )

    assert backtest_rows == 1
    assert replayed.total_available_cuts == 1
    assert replayed.evaluations[0].result.conditions[0].metric_result_id == metrics[0].result_id


def test_seven_rule_catalog_keeps_the_same_market_query_and_adds_two_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    metrics = (
        _metric("2.0", identifier=1, as_of=as_of, known_at=known_at),
        _technical_scope_metric(
            "25",
            identifier=2,
            metric_key=_RSI_KEY,
            algorithm_version=_RSI_LEGACY_ALGORITHM,
            unit="index",
            parameters={"window": 14},
            version_two=False,
            as_of=as_of,
            known_at=known_at,
        ),
        _technical_scope_metric(
            "24",
            identifier=3,
            metric_key=_RSI_KEY,
            algorithm_version=_RSI_INCREMENTAL_ALGORITHM,
            unit="index",
            parameters={"window": 14},
            version_two=True,
            as_of=as_of,
            known_at=known_at,
        ),
        _technical_scope_metric(
            "0.5",
            identifier=4,
            metric_key=_MACD_HISTOGRAM_KEY,
            algorithm_version=_MACD_LEGACY_ALGORITHM,
            unit="USD",
            parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
            version_two=False,
            as_of=as_of,
            known_at=known_at,
        ),
        _technical_scope_metric(
            "0.7",
            identifier=5,
            metric_key=_MACD_HISTOGRAM_KEY,
            algorithm_version=_MACD_INCREMENTAL_ALGORITHM,
            unit="USD",
            parameters={"fast_window": 12, "slow_window": 26, "signal_window": 9},
            version_two=True,
            as_of=as_of,
            known_at=known_at,
        ),
    )
    incremental_ids = {
        RSI_INCREMENTAL_LOW_REVIEW_RULE.rule_id,
        MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE.rule_id,
    }
    legacy_rules = tuple(
        rule for rule in INITIAL_ANALYTICAL_RULES if rule.rule_id not in incremental_ids
    )

    legacy_state, legacy_keys, legacy_rows = _run_monitor_catalog(
        tmp_path / "catalog-legacy",
        metrics,
        monkeypatch,
        rules=legacy_rules,
    )
    full_state, full_keys, full_rows = _run_monitor_catalog(
        tmp_path / "catalog-full",
        metrics,
        monkeypatch,
        rules=INITIAL_ANALYTICAL_RULES,
    )

    expected_keys = tuple(sorted({_METRIC_KEY, _RSI_KEY, _MACD_HISTOGRAM_KEY}))
    assert legacy_keys == full_keys == [expected_keys]
    assert legacy_rows == full_rows == [len(metrics)]
    legacy_by_rule = {result.rule.rule_id: result for result in legacy_state.results}
    full_by_rule = {result.rule.rule_id: result for result in full_state.results}
    assert set(legacy_by_rule) == {
        "market.activity.relative-volume-review",
        "market.technical.macd-positive-histogram-review",
        "market.technical.rsi-low-review",
    }
    assert set(full_by_rule) == set(legacy_by_rule) | incremental_ids
    assert {rule_id: full_by_rule[rule_id].result_id for rule_id in legacy_by_rule} == {
        rule_id: result.result_id for rule_id, result in legacy_by_rule.items()
    }
    assert all(result.asset_id == _ASSET_ID for result in full_state.results)
    assert {
        condition.metric_result_id
        for result in full_state.results
        for condition in result.conditions
    } <= {metric.result_id for metric in metrics}
