"""Persistence, lifecycle, and scheduler integration for analytical candidates."""

import json
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
    ScheduledJobFailureCategory,
    scheduled_job_failure,
)
from investment_analyst.application.operational_state import AaplOperationalStateError
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.core.models import AssetClass, DataQuality, MetricResult
from investment_analyst.storage.repositories import DuckDBMetricResultRepository
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


def _v2_metric(
    value: str,
    *,
    identifier: int,
    as_of: datetime,
    known_at: datetime,
) -> MetricResult:
    """Build a v2 market row: semantic identity without the legacy known_at parameter."""
    input_observation_ids = [UUID(f"10000000-0000-4000-8000-{identifier:012d}")]
    parameters = {"source_id": _SOURCE_ID, "window": 20}
    return MetricResult(
        result_id=metric_result_id_v2(
            asset_id=_ASSET_ID,
            metric_key="market.history.relative_volume",
            input_observation_ids=input_observation_ids,
            algorithm_version="market-relative-volume-v1-decimal34",
            as_of=as_of,
            available_at=known_at,
            unit="ratio",
            quality=DataQuality.PARTIAL,
            parameters=parameters,
        ),
        asset_id=_ASSET_ID,
        metric_key="market.history.relative_volume",
        value=Decimal(value),
        unit="ratio",
        as_of=as_of,
        available_at=known_at,
        computed_at=known_at,
        parameters=parameters,
        input_observation_ids=input_observation_ids,
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


def test_monitor_selects_a_v2_market_metric_instead_of_dropping_it_silently(
    tmp_path: Path,
) -> None:
    """A8: the legacy rule drops a v2 market metric silently; the new rule selects it."""
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    metric = _v2_metric(
        "1.8",
        identifier=41,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        known_at=known_at,
    )
    assert metric.result_id.version == 8
    assert metric.parameters.get("known_at") != known_at.isoformat()
    assert AnalyticalMetricSnapshotSelector().select(
        rule=INITIAL_MARKET_ACTIVITY_RULE,
        metrics=(metric,),
        source_id=_SOURCE_ID,
        known_at=known_at,
    ) == (metric,)

    service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace = service.initialize(tmp_path / "workspace").paths
    writer = service.open_storage(workspace, WorkspaceAccessMode.READ_WRITE)
    try:
        writer.metric_results.save(metric)
    finally:
        writer.close()
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    store = AnalyticalScreeningStateStore(workspace.state_root / "analytical.json")
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"confirmations_required": 1})
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        workspace.root,
        (rule,),
        clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )

    monitor(
        _attempt(
            attempt_id=UUID("30000000-0000-4000-8000-000000000041"),
            known_at=known_at,
        )
    )

    state = store.load()
    assert [item.status for item in state.receipts] == [AnalyticalMonitorReceiptStatus.SCREENED]
    assert len(state.results) == 1
    assert state.results[0].conditions[0].metric_result_id == metric.result_id
    assert state.results[0].as_of == metric.as_of


def test_selector_still_drops_malformed_legacy_market_rows_without_raising() -> None:
    """A3: the legacy read path keeps dropping malformed v1 cut parameters without raising."""
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    malformed = _metric(
        "1.8",
        identifier=42,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        known_at=known_at,
    ).model_copy(
        update={
            "parameters": {
                "source_id": _SOURCE_ID,
                "known_at": "not-a-timestamp",
                "window": 20,
            }
        }
    )

    assert (
        AnalyticalMetricSnapshotSelector().select(
            rule=INITIAL_MARKET_ACTIVITY_RULE,
            metrics=(malformed,),
            source_id=_SOURCE_ID,
            known_at=known_at,
        )
        == ()
    )


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


def test_monitor_bounds_query_to_compatible_rule_condition_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace = service.initialize(tmp_path / "workspace").paths
    known_at = datetime(2026, 7, 29, 12, tzinfo=UTC)
    writer = service.open_storage(workspace, WorkspaceAccessMode.READ_WRITE)
    try:
        writer.metric_results.save(
            _metric(
                "1.8",
                identifier=21,
                as_of=datetime(2026, 7, 28, tzinfo=UTC),
                known_at=known_at,
            )
        )
    finally:
        writer.close()
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    store = AnalyticalScreeningStateStore(workspace.state_root / "analytical.json")
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"confirmations_required": 1})
    attempt = _attempt(
        attempt_id=UUID("30000000-0000-4000-8000-000000000021"),
        known_at=known_at,
    )
    seen_metric_keys: list[tuple[str, ...] | None] = []
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
        seen_metric_keys.append(metric_keys)
        return original_list(
            repository,
            asset_id=asset_id,
            metric_key=metric_key,
            metric_keys=metric_keys,
            as_of_from=as_of_from,
            as_of_to=as_of_to,
        )

    monkeypatch.setattr(DuckDBMetricResultRepository, "list", tracked_list)
    monitor = AnalyticalScreeningMonitor(
        store,
        runtime,
        workspace.root,
        (rule, INITIAL_QUARTERLY_FUNDAMENTAL_RULE),
        clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )

    monitor(attempt)

    assert seen_metric_keys == [("market.history.relative_volume",)]


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


def test_journal_append_does_not_reserialize_history_and_reconstructs_the_same_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "screening_state.json"
    store = AnalyticalScreeningStateStore(path)
    first = _result(
        "1.7",
        identifier=1,
        as_of=datetime(2026, 7, 27, tzinfo=UTC),
        known_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
    )
    _record_result(store, first, identifier=1)

    manifest = store._journal._load_manifest()
    assert manifest is not None
    open_segment = store.journal_dir / manifest.open_segment_name
    assert open_segment.exists()
    size_after_first = open_segment.stat().st_size
    assert size_after_first > 0

    second = _result(
        "1.8",
        identifier=2,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    outcome = _record_result(store, second, identifier=2)
    assert outcome.candidates_created == 1

    size_after_second = open_segment.stat().st_size
    delta = size_after_second - size_after_first
    assert delta < size_after_first * 2

    candidate = store.load().candidates[0]
    transition_time = datetime(2026, 7, 29, 14, tzinfo=UTC)
    store.transition(
        candidate.candidate_id,
        AnalyticalCandidateStatus.SEEN,
        recorded_at=transition_time,
    )

    size_after_transition = open_segment.stat().st_size
    transition_delta = size_after_transition - size_after_second
    assert 100 < transition_delta < 600

    reconstructed = store.load()
    assert len(reconstructed.results) == 2
    assert len(reconstructed.candidates) == 1
    assert len(reconstructed.transitions) == 1
    assert reconstructed.candidates[0].status is AnalyticalCandidateStatus.SEEN

    fresh_store = AnalyticalScreeningStateStore(path)
    reloaded = fresh_store.load()
    assert reloaded.to_json_dict() == reconstructed.to_json_dict()
    assert reloaded == reconstructed


def test_contains_attempt_answers_without_reparsing_the_full_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    _record_result(store, first, identifier=1)
    _record_result(store, second, identifier=2)

    attempt1_id = UUID("20000000-0000-4000-8000-000000000001")
    attempt2_id = UUID("20000000-0000-4000-8000-000000000002")
    unknown_id = UUID("20000000-0000-4000-8000-999999999999")

    def fail_read_entries(*args, **kwargs):
        pytest.fail("read_entries should not be called by contains_attempt on a warm cache")

    loads_calls = 0
    orig_loads = json.loads

    def counting_loads(*args, **kwargs):
        nonlocal loads_calls
        loads_calls += 1
        return orig_loads(*args, **kwargs)

    monkeypatch.setattr(store._journal, "read_entries", fail_read_entries)
    monkeypatch.setattr(json, "loads", counting_loads)

    for _ in range(10):
        assert store.contains_attempt(attempt1_id) is True
        assert store.contains_attempt(attempt2_id) is True
        assert store.contains_attempt(unknown_id) is False

    assert loads_calls == 0


def test_legacy_v1_state_is_folded_once_and_preserved_byte_for_byte(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "screening_state_v1.json"
    first = _result(
        "1.7",
        identifier=1,
        as_of=datetime(2026, 7, 27, tzinfo=UTC),
        known_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
    )
    first_receipt = _receipt(first, identifier=1)
    legacy_state = AnalyticalScreeningState(
        results=(first,),
        candidates=(),
        transitions=(),
        receipts=(first_receipt,),
    )
    legacy_payload = (
        json.dumps(
            legacy_state.to_json_dict(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    legacy_path.write_bytes(legacy_payload)
    original_bytes = legacy_path.read_bytes()

    store = AnalyticalScreeningStateStore(legacy_path)

    loaded_initial = store.load()
    assert len(loaded_initial.results) == 1
    assert len(loaded_initial.receipts) == 1
    assert legacy_path.read_bytes() == original_bytes

    second = _result(
        "1.8",
        identifier=2,
        as_of=datetime(2026, 7, 28, tzinfo=UTC),
        known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
    )
    outcome = _record_result(store, second, identifier=2)
    assert outcome.candidates_created == 1

    assert legacy_path.read_bytes() == original_bytes

    reloaded = store.load()
    assert len(reloaded.results) == 2
    assert len(reloaded.receipts) == 2
    assert len(reloaded.candidates) == 1

    assert store._journal.has_legacy_v1_folded()
    assert store._journal.has_snapshot()

    legacy_path.unlink()
    reloaded_after_delete = store.load()
    assert len(reloaded_after_delete.results) == 2
    assert len(reloaded_after_delete.receipts) == 2
    assert len(reloaded_after_delete.candidates) == 1
