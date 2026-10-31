"""Integration tests for analytical monitor and selector resilience."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest

from investment_analyst.alerts.analytical_backtest import (
    AnalyticalBacktestRequest,
    AnalyticalBacktestService,
)
from investment_analyst.alerts.analytical_engine import AmbiguousAnalyticalMetricError
from investment_analyst.alerts.analytical_monitor import (
    AnalyticalMetricSnapshotSelector,
    AnalyticalScreeningMonitor,
)
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_ANALYTICAL_RULES,
    INITIAL_ANNUAL_FUNDAMENTAL_RULE,
    INITIAL_MARKET_ACTIVITY_RULE,
    INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
    MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE,
    RSI_INCREMENTAL_LOW_REVIEW_RULE,
    RSI_LOW_REVIEW_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import AnalyticalRuleRegistryStore
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateStatus,
    AnalyticalMonitorReceiptStatus,
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
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspacePaths
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_FUNDAMENTAL_SOURCE_ID = "sec-edgar:aapl:companyfacts"
_FUNDAMENTAL_ALGORITHMS = {
    "fundamental.liabilities_to_assets": "sec-fundamental-liabilities-to-assets-v1-decimal34",
    "fundamental.net_margin": "sec-fundamental-net-margin-v1-decimal34",
    "fundamental.revenue_yoy_growth": "sec-fundamental-revenue-yoy-growth-v1-decimal34",
}
_MACD_KEY = "market.technical.macd.histogram"
_MACD_ALGORITHM = "market-macd-v1-decimal34"
_MACD_PARAMS: dict[str, object] = {
    "source_id": _SOURCE_ID,
    "fast_window": 12,
    "slow_window": 26,
    "signal_window": 9,
}
_RSI_KEY = "market.technical.rsi"
_RSI_LEGACY_ALGORITHM = "market-rsi-wilder-v1-decimal34"
_RSI_INCREMENTAL_ALGORITHM = "market-rsi-incremental-v2-decimal34"
_MACD_INCREMENTAL_ALGORITHM = "market-macd-incremental-v2-decimal34"


def _get_macd_rule():
    rule_id = "market.technical.macd-positive-histogram-review"
    return next(r for r in INITIAL_ANALYTICAL_RULES if r.rule_id == rule_id)


def _build_v2_metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    available_at: datetime,
    computed_at: datetime,
    metric_key: str = _MACD_KEY,
    algorithm_version: str = _MACD_ALGORITHM,
    unit: str = "USD",
    parameters: dict[str, object] | None = None,
) -> MetricResult:
    input_observation_ids = [UUID(f"81000000-0000-4000-8000-{identifier:012d}")]
    params = parameters if parameters is not None else dict(_MACD_PARAMS)
    result_id = metric_result_id_v2(
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        as_of=as_of,
        available_at=available_at,
        unit=unit,
        quality=DataQuality.VALID,
        parameters=params,
    )
    return MetricResult(
        result_id=result_id,
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at,
        parameters=params,
        input_observation_ids=input_observation_ids,
        algorithm_version=algorithm_version,
        quality=DataQuality.VALID,
    )


def _build_v1_metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    available_at: datetime,
    computed_at: datetime,
    known_at: datetime,
    metric_key: str = _MACD_KEY,
    algorithm_version: str = _MACD_ALGORITHM,
    unit: str = "USD",
    parameters: dict[str, object] | None = None,
) -> MetricResult:
    params = dict(parameters if parameters is not None else _MACD_PARAMS)
    params["known_at"] = known_at.isoformat()
    # UUIDv5 is resolved as CutIdentityVersion.V1
    result_id = uuid5(NAMESPACE_DNS, f"v1-{metric_key}-{as_of.isoformat()}-{identifier}")
    return MetricResult(
        result_id=result_id,
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at,
        parameters=params,
        input_observation_ids=[UUID(f"81000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=algorithm_version,
        quality=DataQuality.VALID,
    )


def _incremental_metrics(
    *,
    start: datetime,
    day: int,
    rsi_value: str,
    histogram_value: str,
) -> tuple[MetricResult, MetricResult]:
    as_of = start + timedelta(days=day)
    available_at = as_of + timedelta(hours=12)
    return (
        _build_v2_metric(
            rsi_value,
            identifier=100 + day,
            as_of=as_of,
            available_at=available_at,
            computed_at=available_at,
            metric_key=_RSI_KEY,
            algorithm_version=_RSI_INCREMENTAL_ALGORITHM,
            unit="index",
            parameters={"source_id": _SOURCE_ID, "window": 14},
        ),
        _build_v2_metric(
            histogram_value,
            identifier=200 + day,
            as_of=as_of,
            available_at=available_at,
            computed_at=available_at,
            metric_key=_MACD_KEY,
            algorithm_version=_MACD_INCREMENTAL_ALGORITHM,
            unit="USD",
            parameters=dict(_MACD_PARAMS),
        ),
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


def _attempt(
    job_id: str,
    known_at: datetime,
    *,
    attempt_id: UUID,
    scheduled_date: date,
) -> ScheduledJobAttempt:
    definition = ScheduledJobDefinition(
        job_id=job_id,
        asset_id=_ASSET_ID,
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
    )
    scheduled_for = datetime(
        scheduled_date.year,
        scheduled_date.month,
        scheduled_date.day,
        12,
        tzinfo=UTC,
    )
    return ScheduledJobAttempt(
        attempt_id=attempt_id,
        definition=definition,
        local_date=scheduled_date,
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=scheduled_for,
        completed_at=scheduled_for + timedelta(minutes=2),
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
    frequency: str,
    as_of: datetime,
    available_at: datetime,
) -> MetricResult:
    return MetricResult(
        result_id=UUID(f"83000000-0000-4000-8000-{identifier:012d}"),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters={
            "source_id": _FUNDAMENTAL_SOURCE_ID,
            "frequency": frequency,
        },
        input_observation_ids=[UUID(f"84000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=_FUNDAMENTAL_ALGORITHMS[metric_key],
        quality=DataQuality.VALID,
    )


def _fundamental_attempt(
    frequency: str,
    known_at: datetime,
    *,
    attempt_id: UUID,
) -> ScheduledJobAttempt:
    scheduled_date = known_at.date()
    definition = ScheduledJobDefinition(
        job_id=f"sec:{_ASSET_ID}:fundamentals-{frequency}",
        asset_id=_ASSET_ID,
        provider="sec-edgar",
        domain=ScheduledJobDomain.FUNDAMENTALS,
        data_frequency=frequency,
        timezone="America/Lima",
        run_at=time(hour=7),
    )
    scheduled_for = datetime(
        scheduled_date.year,
        scheduled_date.month,
        scheduled_date.day,
        12,
        tzinfo=UTC,
    )
    return ScheduledJobAttempt(
        attempt_id=attempt_id,
        definition=definition,
        local_date=scheduled_date,
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=scheduled_for,
        completed_at=scheduled_for + timedelta(minutes=2),
        execution=ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=known_at,
            evidence_changed=True,
            source_ids=(_FUNDAMENTAL_SOURCE_ID,),
            created_count=1,
            reused_count=0,
        ),
    )


def _fundamental_metrics_for_frequency(
    frequency: str,
    *,
    first_identifier: int,
    liabilities_to_assets: str,
) -> tuple[MetricResult, ...]:
    as_of = datetime(2025, 12, 31, tzinfo=UTC)
    available_at = datetime(2026, 2, 15, 12, tzinfo=UTC)
    return (
        _fundamental_metric(
            "fundamental.liabilities_to_assets",
            liabilities_to_assets,
            identifier=first_identifier,
            frequency=frequency,
            as_of=as_of,
            available_at=available_at,
        ),
        _fundamental_metric(
            "fundamental.net_margin",
            "0.2",
            identifier=first_identifier + 1,
            frequency=frequency,
            as_of=as_of,
            available_at=available_at,
        ),
        _fundamental_metric(
            "fundamental.revenue_yoy_growth",
            "0.1",
            identifier=first_identifier + 2,
            frequency=frequency,
            as_of=as_of,
            available_at=available_at,
        ),
    )


def test_window_dependent_v2_revisions_select_the_latest_computation_known_at_the_cut() -> None:
    """A3: when two v2 revisions exist for a window-dependent key, the latest
    computed_at <= known_at is chosen.
    """
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    available_at = datetime(2026, 7, 28, 20, tzinfo=UTC)

    first_revision = _build_v2_metric(
        "1.5",
        identifier=1,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
    )
    second_revision = _build_v2_metric(
        "2.5",
        identifier=2,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
    )

    selector = AnalyticalMetricSnapshotSelector()
    rule = _get_macd_rule()

    selected = selector.select(
        rule=rule,
        metrics=(first_revision, second_revision),
        source_id=_SOURCE_ID,
        known_at=known_at,
    )

    assert len(selected) == 1
    assert selected[0].result_id == second_revision.result_id
    assert selected[0].value == Decimal("2.5")
    assert selected[0].computed_at == second_revision.computed_at


def test_revisions_computed_after_the_cut_are_never_selected() -> None:
    """A4: a revision computed after the cut is never selected; if only such revisions exist,
    ambiguity is preserved.
    """
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    available_at = datetime(2026, 7, 28, 20, tzinfo=UTC)

    future_rev1 = _build_v2_metric(
        "1.0",
        identifier=1,
        as_of=as_of,
        available_at=available_at,
        computed_at=known_at + timedelta(hours=1),
    )
    future_rev2 = _build_v2_metric(
        "2.0",
        identifier=2,
        as_of=as_of,
        available_at=available_at,
        computed_at=known_at + timedelta(hours=2),
    )

    selector = AnalyticalMetricSnapshotSelector()
    rule = _get_macd_rule()

    # When all revisions are computed after known_at, ambiguity is preserved
    with pytest.raises(
        AmbiguousAnalyticalMetricError, match="multiple compatible metric revisions exist"
    ):
        selector.select(
            rule=rule,
            metrics=(future_rev1, future_rev2),
            source_id=_SOURCE_ID,
            known_at=known_at,
        )

    # When one is valid and one is in the future, only the valid one is selected
    valid_rev = _build_v2_metric(
        "0.5",
        identifier=3,
        as_of=as_of,
        available_at=available_at,
        computed_at=known_at - timedelta(hours=1),
    )
    selected = selector.select(
        rule=rule,
        metrics=(future_rev1, valid_rev),
        source_id=_SOURCE_ID,
        known_at=known_at,
    )
    assert len(selected) == 1
    assert selected[0].result_id == valid_rev.result_id
    assert selected[0].computed_at <= known_at


def test_reconcile_records_an_ambiguous_attempt_as_skipped_and_continues(tmp_path: Path) -> None:
    """A5: reconcile records an ambiguous attempt as SKIPPED with reason and
    continues with subsequent attempts.
    """
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    paths = service.initialize(tmp_path / "workspace").paths
    runtime = ApplicationRuntime.create_default(workspace_service=service)

    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    available_at = datetime(2026, 7, 28, 20, tzinfo=UTC)
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)

    # Two duplicate v1 revisions outside the policy -> will raise AmbiguousAnalyticalMetricError
    dup1 = _build_v1_metric(
        "1.0",
        identifier=1,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
        known_at=known_at,
    )
    dup2 = _build_v1_metric(
        "2.0",
        identifier=2,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
        known_at=known_at,
    )

    valid_m = _build_v2_metric(
        "3.0",
        identifier=10,
        as_of=datetime(2026, 7, 29, tzinfo=UTC),
        available_at=datetime(2026, 7, 29, 20, tzinfo=UTC),
        computed_at=datetime(2026, 7, 29, 20, tzinfo=UTC),
    )

    with service.open_storage(paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.metric_results.save(dup1)
        storage.metric_results.save(dup2)
        storage.metric_results.save(valid_m)

    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    rule = _get_macd_rule()
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (rule,),
        clock=lambda: datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
    )

    attempt_ambiguous = _attempt(
        "alpaca:equity:us:aapl:market-daily",
        known_at,
        attempt_id=UUID("90000000-0000-4000-8000-000000000001"),
        scheduled_date=date(2026, 7, 29),
    )
    attempt_valid = _attempt(
        "alpaca:equity:us:aapl:market-daily",
        known_at + timedelta(days=1),
        attempt_id=UUID("90000000-0000-4000-8000-000000000002"),
        scheduled_date=date(2026, 7, 30),
    )

    # reconcile must NOT raise even though attempt_ambiguous encounters
    # AmbiguousAnalyticalMetricError
    monitor.reconcile((attempt_ambiguous, attempt_valid))

    state = store.load()
    receipts_by_attempt = {r.attempt_id: r for r in state.receipts}
    assert attempt_ambiguous.attempt_id in receipts_by_attempt
    assert attempt_valid.attempt_id in receipts_by_attempt

    ambiguous_receipt = receipts_by_attempt[attempt_ambiguous.attempt_id]
    assert ambiguous_receipt.status is AnalyticalMonitorReceiptStatus.SKIPPED
    assert "screening_error:" in ambiguous_receipt.reason
    assert "multiple compatible metric revisions exist" in ambiguous_receipt.reason

    # The monitor registered the attempt so replay won't re-run it
    assert store.contains_attempt(attempt_ambiguous.attempt_id)


def test_backtest_applies_the_same_window_revision_selection(tmp_path: Path) -> None:
    """A6: AnalyticalBacktestService uses the same selector and resolves window revisions."""
    # Day 1 with two revisions: first_rev computed at day1_time, second_rev computed at day2_time
    day1_as_of = datetime(2026, 7, 27, tzinfo=UTC)
    day1_available = datetime(2026, 7, 27, 20, tzinfo=UTC)

    first_rev = _build_v2_metric(
        "1.5",
        identifier=1,
        as_of=day1_as_of,
        available_at=day1_available,
        computed_at=day1_available,
    )
    second_rev = _build_v2_metric(
        "2.5",
        identifier=2,
        as_of=day1_as_of,
        available_at=day1_available,
        computed_at=datetime(2026, 7, 28, 10, tzinfo=UTC),
    )
    # Day 2 metric creates cut at 2026-07-28 20:00:00Z
    day2_as_of = datetime(2026, 7, 28, tzinfo=UTC)
    day2_available = datetime(2026, 7, 28, 20, tzinfo=UTC)
    day2_metric = _build_v2_metric(
        "3.5",
        identifier=3,
        as_of=day2_as_of,
        available_at=day2_available,
        computed_at=day2_available,
    )

    service, paths, runtime = _seed(tmp_path, (first_rev, second_rev, day2_metric))
    registry = AnalyticalRuleRegistryStore(
        paths.state_root / "rules.json",
        INITIAL_ANALYTICAL_RULES,
    )
    backtest = AnalyticalBacktestService(runtime, paths.root, registry)

    result = backtest.run(
        AnalyticalBacktestRequest(
            rule_id="market.technical.macd-positive-histogram-review",
            asset_id=_ASSET_ID,
            max_cuts=20,
        )
    )

    assert result.total_available_cuts == 2
    # At cut 1 (2026-07-27 20:00:00Z): second_rev was computed in future, so first_rev was chosen
    evaluation1 = result.evaluations[0]
    assert evaluation1.result.conditions[0].metric_result_id == first_rev.result_id
    # At cut 2 (2026-07-28 20:00:00Z): day2_metric was chosen for the latest period
    evaluation2 = result.evaluations[1]
    assert evaluation2.result.conditions[0].metric_result_id == day2_metric.result_id


def test_revisions_outside_the_policy_remain_ambiguous() -> None:
    """X1: two compatible revisions of a key outside the window policy remain ambiguous."""
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    available_at = datetime(2026, 7, 28, 20, tzinfo=UTC)

    # market.history.relative_volume is NOT in WINDOW_DEPENDENT_METRIC_KEYS
    rev1 = _build_v2_metric(
        "1.2",
        identifier=1,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
        metric_key="market.history.relative_volume",
        algorithm_version="market-relative-volume-v1-decimal34",
        unit="ratio",
        parameters={"source_id": _SOURCE_ID, "window": 20},
    )
    rev2 = _build_v2_metric(
        "1.4",
        identifier=2,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
        metric_key="market.history.relative_volume",
        algorithm_version="market-relative-volume-v1-decimal34",
        unit="ratio",
        parameters={"source_id": _SOURCE_ID, "window": 20},
    )

    selector = AnalyticalMetricSnapshotSelector()
    rule = INITIAL_MARKET_ACTIVITY_RULE

    with pytest.raises(
        AmbiguousAnalyticalMetricError, match="multiple compatible metric revisions exist"
    ):
        selector.select(
            rule=rule,
            metrics=(rev1, rev2),
            source_id=_SOURCE_ID,
            known_at=known_at,
        )


def test_duplicate_v1_revisions_remain_ambiguous() -> None:
    """X2: two duplicate v1 revisions of a window-dependent key remain ambiguous."""
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    available_at = datetime(2026, 7, 28, 20, tzinfo=UTC)

    rev1 = _build_v1_metric(
        "1.0",
        identifier=1,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 28, 21, tzinfo=UTC),
        known_at=known_at,
    )
    rev2 = _build_v1_metric(
        "2.0",
        identifier=2,
        as_of=as_of,
        available_at=available_at,
        computed_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
        known_at=known_at,
    )

    selector = AnalyticalMetricSnapshotSelector()
    rule = _get_macd_rule()

    with pytest.raises(
        AmbiguousAnalyticalMetricError, match="multiple compatible metric revisions exist"
    ):
        selector.select(
            rule=rule,
            metrics=(rev1, rev2),
            source_id=_SOURCE_ID,
            known_at=known_at,
        )


def test_fundamental_dispatch_evaluates_only_the_attempt_frequency(tmp_path: Path) -> None:
    known_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    metrics = (
        *_fundamental_metrics_for_frequency(
            "annual",
            first_identifier=1,
            liabilities_to_assets="0.5",
        ),
        *_fundamental_metrics_for_frequency(
            "quarterly",
            first_identifier=10,
            liabilities_to_assets="0.7",
        ),
    )
    service, paths, runtime = _seed(tmp_path, metrics)
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (INITIAL_ANNUAL_FUNDAMENTAL_RULE, INITIAL_QUARTERLY_FUNDAMENTAL_RULE),
        clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    annual_attempt = _fundamental_attempt(
        "annual",
        known_at,
        attempt_id=UUID("90000000-0000-4000-8000-000000000021"),
    )
    quarterly_attempt = _fundamental_attempt(
        "quarterly",
        known_at,
        attempt_id=UUID("90000000-0000-4000-8000-000000000022"),
    )

    monitor.reconcile((annual_attempt, quarterly_attempt))

    state = store.load()
    by_attempt = {
        receipt.attempt_id: tuple(
            result for result in state.results if result.result_id in receipt.result_ids
        )
        for receipt in state.receipts
    }
    annual_result = by_attempt[annual_attempt.attempt_id]
    quarterly_result = by_attempt[quarterly_attempt.attempt_id]

    assert len(annual_result) == 1
    assert annual_result[0].rule.rule_id == INITIAL_ANNUAL_FUNDAMENTAL_RULE.rule_id
    assert annual_result[0].matched is True
    assert {item.metric_result_id for item in annual_result[0].conditions} == {
        metrics[0].result_id,
        metrics[1].result_id,
        metrics[2].result_id,
    }
    assert len(quarterly_result) == 1
    assert quarterly_result[0].rule.rule_id == INITIAL_QUARTERLY_FUNDAMENTAL_RULE.rule_id
    assert quarterly_result[0].matched is False
    assert {item.metric_result_id for item in quarterly_result[0].conditions} == {
        metrics[3].result_id,
        metrics[4].result_id,
        metrics[5].result_id,
    }


def test_fundamental_dispatch_never_falls_back_to_another_frequency(tmp_path: Path) -> None:
    known_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    service, paths, runtime = _seed(
        tmp_path,
        _fundamental_metrics_for_frequency(
            "quarterly",
            first_identifier=1,
            liabilities_to_assets="0.5",
        ),
    )
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (INITIAL_QUARTERLY_FUNDAMENTAL_RULE,),
        clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    attempt = _fundamental_attempt(
        "annual",
        known_at,
        attempt_id=UUID("90000000-0000-4000-8000-000000000023"),
    )

    monitor(attempt)

    state = store.load()
    assert state.results == ()
    assert len(state.receipts) == 1
    assert state.receipts[0].status is AnalyticalMonitorReceiptStatus.SKIPPED
    assert state.receipts[0].reason == "no_compatible_rules"


@pytest.mark.parametrize("mode", ("missing", "contradictory"))
def test_fundamental_dispatch_fails_closed_on_incoherent_rule_frequency(
    tmp_path: Path,
    mode: str,
) -> None:
    conditions = list(INITIAL_ANNUAL_FUNDAMENTAL_RULE.conditions)
    replacement = {} if mode == "missing" else {"frequency": "quarterly"}
    conditions[0] = conditions[0].model_copy(update={"parameter_filters": replacement})
    rule = INITIAL_ANNUAL_FUNDAMENTAL_RULE.model_copy(update={"conditions": tuple(conditions)})
    known_at = datetime(2026, 9, 1, 12, tzinfo=UTC)
    service, paths, runtime = _seed(
        tmp_path,
        _fundamental_metrics_for_frequency(
            "annual",
            first_identifier=1,
            liabilities_to_assets="0.5",
        ),
    )
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (rule,),
        clock=lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )
    attempt = _fundamental_attempt(
        "annual",
        known_at,
        attempt_id=UUID("90000000-0000-4000-8000-000000000024"),
    )

    monitor(attempt)

    state = store.load()
    assert state.results == ()
    assert len(state.receipts) == 1
    assert state.receipts[0].status is AnalyticalMonitorReceiptStatus.SKIPPED
    assert state.receipts[0].reason == "no_compatible_rules"


def test_empty_market_check_skips_screening_but_new_bar_runs(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    paths = service.initialize(tmp_path / "workspace").paths
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    rule = _get_macd_rule()
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (rule,),
        clock=lambda: datetime(2026, 7, 30, 12, 5, tzinfo=UTC),
    )
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)

    def _market_attempt(identifier: str, analytical_changed: bool | None) -> ScheduledJobAttempt:
        base = _attempt(
            "alpaca:equity:us:aapl:market-daily",
            known_at,
            attempt_id=UUID(identifier),
            scheduled_date=date(2026, 7, 29),
        )
        return base.model_copy(
            update={
                "execution": base.execution.model_copy(
                    update={"analytical_inputs_changed": analytical_changed}
                )
            }
        )

    quiet = _market_attempt("90000000-0000-4000-8000-000000000011", False)
    monitor(quiet)
    quiet_receipt = next(
        item for item in store.load().receipts if item.attempt_id == quiet.attempt_id
    )
    assert quiet_receipt.status is AnalyticalMonitorReceiptStatus.SKIPPED
    assert quiet_receipt.reason == "unchanged_analytical_inputs"

    legacy = _market_attempt("90000000-0000-4000-8000-000000000012", None)
    base = _attempt(
        "alpaca:equity:us:aapl:market-daily",
        known_at,
        attempt_id=UUID("90000000-0000-4000-8000-000000000012"),
        scheduled_date=date(2026, 7, 29),
    )
    assert legacy == base


def test_incremental_technical_rules_flow_through_monitor_lifecycle_and_replay(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 7, 20, tzinfo=UTC)
    values = (
        ("25", "0.5"),
        ("24", "0.7"),
        ("40", "-0.2"),
        ("23", "0.6"),
        ("22", "0.9"),
    )
    metrics = tuple(
        metric
        for day, (rsi_value, histogram_value) in enumerate(values)
        for metric in _incremental_metrics(
            start=start,
            day=day,
            rsi_value=rsi_value,
            histogram_value=histogram_value,
        )
    )
    service, paths, runtime = _seed(tmp_path, metrics)
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    clocks = iter(
        (
            start + timedelta(hours=12, minutes=5),
            start + timedelta(days=1, hours=12, minutes=5),
            start + timedelta(days=2, hours=12),
            start + timedelta(days=3, hours=12),
            start + timedelta(days=4, hours=12),
        )
    )
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (RSI_INCREMENTAL_LOW_REVIEW_RULE, MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE),
        clock=lambda: next(clocks),
    )
    attempts = tuple(
        _attempt(
            "alpaca:equity:us:aapl:market-daily",
            start + timedelta(days=day, hours=12),
            attempt_id=UUID(f"90000000-0000-4000-8000-{day + 100:012d}"),
            scheduled_date=(start + timedelta(days=day)).date(),
        )
        for day in range(len(values))
    )

    for attempt in attempts:
        monitor(attempt)
    state = store.load()

    assert len(state.results) == 10
    assert len(state.candidates) == 4
    assert (
        sum(candidate.status is AnalyticalCandidateStatus.NEW for candidate in state.candidates)
        == 2
    )
    assert (
        sum(
            candidate.status is AnalyticalCandidateStatus.RESOLVED for candidate in state.candidates
        )
        == 2
    )
    assert len(state.transitions) == 2
    assert len(state.receipts) == 5

    monitor(attempts[-1])
    assert store.load() == state

    registry = AnalyticalRuleRegistryStore(
        paths.state_root / "rules.json",
        INITIAL_ANALYTICAL_RULES,
    )
    backtest = AnalyticalBacktestService(runtime, paths.root, registry)
    metric_ids = {
        rule_id: {metric.result_id for metric in metrics if metric.metric_key == metric_key}
        for rule_id, metric_key in (
            (RSI_INCREMENTAL_LOW_REVIEW_RULE.rule_id, _RSI_KEY),
            (MACD_INCREMENTAL_POSITIVE_HISTOGRAM_REVIEW_RULE.rule_id, _MACD_KEY),
        )
    }
    for rule_id, expected_ids in metric_ids.items():
        replay = backtest.run(
            AnalyticalBacktestRequest(
                rule_id=rule_id,
                asset_id=_ASSET_ID,
                max_cuts=20,
            )
        )

        assert replay.total_available_cuts == 5
        assert len(replay.evaluations) == 5
        assert replay.evaluations[-1].result.conditions[0].metric_result_id in expected_ids


def test_monitor_keeps_legacy_and_incremental_versions_separate(tmp_path: Path) -> None:
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 7, 28, tzinfo=UTC)
    available_at = datetime(2026, 7, 28, 20, tzinfo=UTC)
    legacy_metric = _build_v1_metric(
        "25",
        identifier=1,
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        known_at=known_at,
        metric_key=_RSI_KEY,
        algorithm_version=_RSI_LEGACY_ALGORITHM,
        unit="index",
        parameters={"source_id": _SOURCE_ID, "window": 14},
    )
    incremental_metric = _build_v2_metric(
        "24",
        identifier=2,
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        metric_key=_RSI_KEY,
        algorithm_version=_RSI_INCREMENTAL_ALGORITHM,
        unit="index",
        parameters={"source_id": _SOURCE_ID, "window": 14},
    )
    service, paths, runtime = _seed(tmp_path, (legacy_metric, incremental_metric))
    store = AnalyticalScreeningStateStore(paths.state_root / "analytical.json")
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        paths.root,
        (RSI_LOW_REVIEW_RULE, RSI_INCREMENTAL_LOW_REVIEW_RULE),
        clock=lambda: datetime(2026, 7, 30, 12, tzinfo=UTC),
    )

    monitor(
        _attempt(
            "alpaca:equity:us:aapl:market-daily",
            known_at,
            attempt_id=UUID("90000000-0000-4000-8000-000000000031"),
            scheduled_date=date(2026, 7, 29),
        )
    )
    state = store.load()
    by_rule = {result.rule.rule_id: result for result in state.results}

    assert len(state.results) == 2
    assert len(state.candidates) == 0
    assert (
        by_rule[RSI_LOW_REVIEW_RULE.rule_id].conditions[0].metric_result_id
        == legacy_metric.result_id
    )
    assert (
        by_rule[RSI_INCREMENTAL_LOW_REVIEW_RULE.rule_id].conditions[0].metric_result_id
        == incremental_metric.result_id
    )
