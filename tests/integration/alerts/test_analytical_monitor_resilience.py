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
    INITIAL_MARKET_ACTIVITY_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import AnalyticalRuleRegistryStore
from investment_analyst.alerts.analytical_state import (
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
_MACD_KEY = "market.technical.macd.histogram"
_MACD_ALGORITHM = "market-macd-v1-decimal34"
_MACD_PARAMS: dict[str, object] = {
    "source_id": _SOURCE_ID,
    "fast_window": 12,
    "slow_window": 26,
    "signal_window": 9,
}


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
