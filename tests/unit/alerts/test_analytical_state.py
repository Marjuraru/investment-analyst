"""Persistence, lifecycle, and scheduler integration for analytical candidates."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.alerts.analytical_engine import (
    AmbiguousAnalyticalMetricError,
    AnalyticalScreeningEngine,
)
from investment_analyst.alerts.analytical_models import (
    AnalyticalRuleState,
    AnalyticalScreeningRequest,
)
from investment_analyst.alerts.analytical_monitor import (
    AnalyticalMetricSnapshotSelector,
    AnalyticalScreeningMonitor,
)
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_MARKET_ACTIVITY_RULE,
    INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateStatus,
    AnalyticalMonitorReceipt,
    AnalyticalMonitorReceiptStatus,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailureCategory,
    scheduled_job_failure,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import AssetClass, DataQuality, MetricResult
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:aapl"
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_FUNDAMENTAL_SOURCE_ID = "sec-edgar:aapl:companyfacts"


def _metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    known_at: datetime,
) -> MetricResult:
    return MetricResult(
        result_id=UUID(f"00000000-0000-4000-8000-{identifier:012d}"),
        asset_id=_ASSET_ID,
        metric_key="market.history.relative_volume",
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters={
            "source_id": _SOURCE_ID,
            "known_at": known_at.isoformat(),
            "window": 20,
        },
        input_observation_ids=[UUID(f"10000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version="market-relative-volume-v1-decimal34",
        quality=DataQuality.PARTIAL,
    )


def _fundamental_metric(
    metric_key: str,
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    available_at: datetime,
) -> MetricResult:
    algorithms = {
        "fundamental.liabilities_to_assets": ("sec-fundamental-liabilities-to-assets-v1-decimal34"),
        "fundamental.net_margin": "sec-fundamental-net-margin-v1-decimal34",
        "fundamental.revenue_yoy_growth": ("sec-fundamental-revenue-yoy-growth-v1-decimal34"),
    }
    return MetricResult(
        result_id=UUID(f"40000000-0000-4000-8000-{identifier:012d}"),
        asset_id=_ASSET_ID,
        metric_key=metric_key,
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=available_at,
        computed_at=available_at,
        parameters={
            "source_id": _FUNDAMENTAL_SOURCE_ID,
            "frequency": "quarterly",
        },
        input_observation_ids=[UUID(f"50000000-0000-4000-8000-{identifier:012d}")],
        algorithm_version=algorithms[metric_key],
        quality=DataQuality.VALID,
    )


def _result(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    known_at: datetime,
    confirmations_required: int = 2,
):
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(
        update={"confirmations_required": confirmations_required}
    )
    metric = _metric(
        value,
        identifier=identifier,
        as_of=as_of,
        known_at=known_at,
    )
    return AnalyticalScreeningEngine().evaluate(
        AnalyticalScreeningRequest(
            rule=rule,
            asset_id=_ASSET_ID,
            asset_class=AssetClass.EQUITY,
            source_id=_SOURCE_ID,
            known_at=known_at,
            computed_at=known_at,
            metrics=(metric,),
        )
    )


def _receipt(result, *, identifier: int) -> AnalyticalMonitorReceipt:
    return AnalyticalMonitorReceipt(
        attempt_id=UUID(f"20000000-0000-4000-8000-{identifier:012d}"),
        job_id="alpaca:equity:us:aapl:market-daily",
        asset_id=_ASSET_ID,
        status=AnalyticalMonitorReceiptStatus.SCREENED,
        reason="new_compatible_evidence",
        processed_at=result.computed_at,
        result_ids=(result.result_id,),
    )


def _record_result(
    store: AnalyticalScreeningStateStore,
    result,
    *,
    identifier: int,
):
    return store.record_attempt(_receipt(result, identifier=identifier), (result,))


def test_store_requires_confirmations_deduplicates_replay_and_survives_restart(
    tmp_path: Path,
) -> None:
    store = AnalyticalScreeningStateStore(tmp_path / "state.json")
    first = _result(
        "1.7",
        identifier=1,
        as_of=datetime(2026, 7, 27, tzinfo=UTC),
        known_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
    )
    second = _result(
        "1.8",
        identifier=2,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )

    assert _record_result(store, first, identifier=1).candidates_created == 0
    outcome = _record_result(store, second, identifier=2)
    assert outcome.candidates_created == 1
    assert outcome.results_created == 1
    assert AnalyticalScreeningStateStore(tmp_path / "state.json").load() == store.load()

    repeated = _record_result(store, second, identifier=2)
    assert repeated.receipt_created is False
    assert len(store.load().results) == 2
    assert len(store.load().candidates) == 1


def test_hysteresis_retains_candidate_in_middle_band_then_resolves_on_exit(
    tmp_path: Path,
) -> None:
    store = AnalyticalScreeningStateStore(tmp_path / "state.json")
    points = (
        ("1.7", 1, datetime(2026, 7, 26, tzinfo=UTC)),
        ("1.8", 2, datetime(2026, 7, 27, tzinfo=UTC)),
        ("1.3", 3, datetime(2026, 7, 28, tzinfo=UTC)),
        ("1.1", 4, datetime(2026, 7, 29, tzinfo=UTC)),
    )
    for value, identifier, as_of in points:
        result = _result(
            value,
            identifier=identifier,
            as_of=as_of,
            known_at=as_of + timedelta(hours=12),
        )
        outcome = _record_result(store, result, identifier=identifier)
        if identifier == 3:
            assert outcome.candidates_resolved == 0
            assert store.load().candidates[0].status is AnalyticalCandidateStatus.NEW
        if identifier == 4:
            assert outcome.candidates_resolved == 1

    state = store.load()
    assert state.candidates[0].status is AnalyticalCandidateStatus.RESOLVED
    assert state.transitions[0].actor == "system_evidence"


def test_cooldown_blocks_reactivation_until_new_confirmations_after_window(
    tmp_path: Path,
) -> None:
    store = AnalyticalScreeningStateStore(tmp_path / "state.json")
    values = ("1.7", "1.8", "1.0", "1.7", "1.8", "1.7", "1.8")
    offsets = (0, 1, 2, 3, 3, 5, 6)
    for identifier, (value, offset) in enumerate(zip(values, offsets, strict=True), start=1):
        as_of = datetime(2026, 7, 20, tzinfo=UTC) + timedelta(
            days=offset,
            hours=identifier,
        )
        result = _result(
            value,
            identifier=identifier,
            as_of=as_of,
            known_at=as_of + timedelta(hours=1),
        )
        _record_result(store, result, identifier=identifier)

    state = store.load()
    assert len(state.candidates) == 2
    assert state.candidates[0].status is AnalyticalCandidateStatus.RESOLVED
    assert state.candidates[1].status is AnalyticalCandidateStatus.NEW


def test_user_transitions_are_idempotent_and_audited(tmp_path: Path) -> None:
    store = AnalyticalScreeningStateStore(tmp_path / "state.json")
    result = _result(
        "1.8",
        identifier=1,
        as_of=datetime(2026, 7, 29, tzinfo=UTC),
        known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        confirmations_required=1,
    )
    _record_result(store, result, identifier=1)
    candidate = store.load().candidates[0]
    recorded_at = datetime(2026, 7, 29, 13, tzinfo=UTC)

    seen, changed = store.transition(
        candidate.candidate_id,
        AnalyticalCandidateStatus.SEEN,
        recorded_at=recorded_at,
    )
    repeated, repeated_changed = store.transition(
        candidate.candidate_id,
        AnalyticalCandidateStatus.SEEN,
        recorded_at=recorded_at + timedelta(minutes=1),
    )

    assert changed is True
    assert repeated_changed is False
    assert seen == repeated
    assert store.status().new_count == 0
    assert store.inbox(limit=1).items[0].result.result_id == result.result_id


def test_store_rejects_receipt_mismatch_and_malformed_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = AnalyticalScreeningStateStore(path)
    result = _result(
        "1.8",
        identifier=1,
        as_of=datetime(2026, 7, 29, tzinfo=UTC),
        known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="do not match"):
        store.record_attempt(
            _receipt(result, identifier=1).model_copy(update={"result_ids": ()}),
            (result,),
        )
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(AaplOperationalStateError, match="malformed"):
        store.load()


def test_selector_uses_exact_cut_rejects_future_and_ambiguous_revisions() -> None:
    selector = AnalyticalMetricSnapshotSelector()
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    current = _metric(
        "1.8",
        identifier=1,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        known_at=known_at,
    )
    other_cut = _metric(
        "2.0",
        identifier=2,
        as_of=datetime(2026, 7, 29, tzinfo=UTC),
        known_at=known_at + timedelta(hours=1),
    )

    selected = selector.select(
        rule=INITIAL_MARKET_ACTIVITY_RULE,
        metrics=(current, other_cut),
        source_id=_SOURCE_ID,
        known_at=known_at,
    )
    assert selected == (current,)

    duplicate = current.model_copy(
        update={"result_id": UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")}
    )
    with pytest.raises(AmbiguousAnalyticalMetricError, match="multiple compatible"):
        selector.select(
            rule=INITIAL_MARKET_ACTIVITY_RULE,
            metrics=(current, duplicate),
            source_id=_SOURCE_ID,
            known_at=known_at,
        )


def test_selector_uses_latest_available_fundamental_revision_without_cut_parameter() -> None:
    selector = AnalyticalMetricSnapshotSelector()
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    as_of = datetime(2026, 6, 30, tzinfo=UTC)
    older = _fundamental_metric(
        "fundamental.liabilities_to_assets",
        "0.55",
        identifier=1,
        as_of=as_of,
        available_at=known_at - timedelta(days=2),
    )
    latest = _fundamental_metric(
        "fundamental.liabilities_to_assets",
        "0.50",
        identifier=2,
        as_of=as_of,
        available_at=known_at - timedelta(days=1),
    )
    margin = _fundamental_metric(
        "fundamental.net_margin",
        "0.20",
        identifier=3,
        as_of=as_of,
        available_at=known_at - timedelta(days=1),
    )
    growth = _fundamental_metric(
        "fundamental.revenue_yoy_growth",
        "0.08",
        identifier=4,
        as_of=as_of,
        available_at=known_at - timedelta(days=1),
    )

    selected = selector.select(
        rule=INITIAL_QUARTERLY_FUNDAMENTAL_RULE,
        metrics=(older, latest, margin, growth),
        source_id=_FUNDAMENTAL_SOURCE_ID,
        known_at=known_at,
    )

    assert {item.result_id for item in selected} == {
        latest.result_id,
        margin.result_id,
        growth.result_id,
    }


def _definition() -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id="alpaca:equity:us:aapl:market-daily",
        asset_id=_ASSET_ID,
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
    )


def _attempt(
    *,
    attempt_id: UUID,
    known_at: datetime,
    created: int = 1,
) -> ScheduledJobAttempt:
    definition = _definition()
    return ScheduledJobAttempt(
        attempt_id=attempt_id,
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
            evidence_changed=created > 0,
            source_ids=(_SOURCE_ID,),
            created_count=created,
            reused_count=0,
        ),
    )


def test_monitor_reads_workspace_once_then_restart_replay_is_noop(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace = service.initialize(tmp_path / "workspace").paths
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    writer = service.open_storage(workspace, WorkspaceAccessMode.READ_WRITE)
    try:
        writer.metric_results.save(
            _metric(
                "1.8",
                identifier=1,
                as_of=datetime(2026, 7, 28, tzinfo=UTC),
                known_at=known_at,
            )
        )
    finally:
        writer.close()
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    state_path = workspace.state_root / "analytical.json"
    store = AnalyticalScreeningStateStore(state_path)
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"confirmations_required": 1})
    attempt = _attempt(
        attempt_id=UUID("30000000-0000-4000-8000-000000000001"),
        known_at=known_at,
    )
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        workspace.root,
        (rule,),
        clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )

    monitor(attempt)
    before = state_path.read_bytes()
    AnalyticalScreeningMonitor(
        AnalyticalScreeningStateStore(state_path),
        runtime,
        workspace.root,
        (rule,),
        clock=lambda: datetime(2026, 7, 29, 13, tzinfo=UTC),
    ).reconcile((attempt,))

    state = store.load()
    assert state_path.read_bytes() == before
    assert len(state.results) == 1
    assert len(state.candidates) == 1
    assert len(state.receipts) == 1


def test_monitor_resolves_versioned_rules_for_each_new_attempt(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace = service.initialize(tmp_path / "workspace").paths
    first_known_at = datetime(2026, 7, 28, 12, tzinfo=UTC)
    second_known_at = first_known_at + timedelta(days=1)
    writer = service.open_storage(workspace, WorkspaceAccessMode.READ_WRITE)
    try:
        writer.metric_results.save(
            _metric(
                "1.8",
                identifier=11,
                as_of=datetime(2026, 7, 27, tzinfo=UTC),
                known_at=first_known_at,
            )
        )
        writer.metric_results.save(
            _metric(
                "1.9",
                identifier=12,
                as_of=datetime(2026, 7, 28, tzinfo=UTC),
                known_at=second_known_at,
            )
        )
    finally:
        writer.close()
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    store = AnalyticalScreeningStateStore(workspace.state_root / "analytical.json")
    configured = [
        INITIAL_MARKET_ACTIVITY_RULE.model_copy(
            update={
                "rule_version": "1.0.local.1",
                "state": AnalyticalRuleState.PAUSED,
                "confirmations_required": 1,
            }
        )
    ]
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        workspace.root,
        lambda: tuple(configured),
        clock=lambda: datetime(2026, 7, 29, 13, tzinfo=UTC),
    )

    monitor(
        _attempt(
            attempt_id=UUID("30000000-0000-4000-8000-000000000011"),
            known_at=first_known_at,
        )
    )
    configured[0] = INITIAL_MARKET_ACTIVITY_RULE.model_copy(
        update={
            "rule_version": "1.0.local.2",
            "confirmations_required": 1,
        }
    )
    monitor(
        _attempt(
            attempt_id=UUID("30000000-0000-4000-8000-000000000012"),
            known_at=second_known_at,
        )
    )

    state = store.load()
    assert tuple(item.rule.rule_version for item in state.results) == (
        "1.0.local.1",
        "1.0.local.2",
    )
    assert tuple(item.activated for item in state.results) == (False, True)
    assert len(state.candidates) == 1
    assert state.candidates[0].rule_version == "1.0.local.2"


def test_monitor_records_failure_and_unchanged_attempts_without_reading_storage(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    store = AnalyticalScreeningStateStore(tmp_path / "state.json")
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        tmp_path / "missing-workspace",
        (INITIAL_MARKET_ACTIVITY_RULE,),
    )
    unchanged = _attempt(
        attempt_id=UUID("30000000-0000-4000-8000-000000000002"),
        known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        created=0,
    )
    definition = _definition()
    failed = ScheduledJobAttempt(
        attempt_id=UUID("30000000-0000-4000-8000-000000000003"),
        definition=definition,
        local_date=date(2026, 7, 29),
        scheduled_for=datetime(2026, 7, 29, 12, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.FAILED,
        started_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        completed_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        failure=scheduled_job_failure(ScheduledJobFailureCategory.TRANSPORT, "safe failure"),
    )

    monitor.reconcile((failed, unchanged))

    state = store.load()
    assert len(state.receipts) == 2
    assert not state.results
    assert {item.reason for item in state.receipts} == {
        "attempt_failed",
        "unchanged_evidence",
    }
