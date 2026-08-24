"""Unit coverage for fail-closed operational readiness."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

import investment_analyst.application.operational_readiness as readiness_module
from investment_analyst.alerts.analytical_engine import AnalyticalScreeningEngine
from investment_analyst.alerts.analytical_models import AnalyticalScreeningRequest
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_MARKET_ACTIVITY_RULE
from investment_analyst.alerts.analytical_state import (
    AnalyticalMonitorReceipt,
    AnalyticalMonitorReceiptStatus,
    AnalyticalScreeningState,
    AnalyticalScreeningStateStore,
)
from investment_analyst.application.manual_operations import (
    ManualOperationKind,
    ManualOperationRequest,
    ManualOperationState,
    ManualOperationStateStore,
    ManualOperationStatus,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduleStateStore,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailureCategory,
    scheduled_job_failure,
)
from investment_analyst.application.operational_alerts import (
    OperationalAlertEngine,
    OperationalAlertMonitor,
    OperationalAlertStateStore,
)
from investment_analyst.application.operational_readiness import (
    OperationalReadinessDecision,
    OperationalReadinessParameters,
    OperationalReadinessReasonCode,
    OperationalReadinessService,
    OperationalReadinessStateError,
)
from investment_analyst.core.models import AssetClass
from investment_analyst.workspace.service import WorkspaceService

_SINCE = datetime(2026, 8, 1, tzinfo=UTC)
_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_SCHEDULE = "multi_asset_schedule_state_v1.json"
_OPERATIONAL = "operational_alert_state_v1.json"
_ANALYTICAL = "analytical_screening_state_v1.json"
_MANUAL = "manual_operation_state_v1.json"


def _definition(
    job_id: str,
    *,
    max_attempts: int = 3,
    backoff: int = 60,
) -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=job_id,
        asset_id="equity:us:aapl",
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=max_attempts,
        retry_backoff_seconds=backoff,
    )


def _attempt(
    definition: ScheduledJobDefinition,
    local_date: date,
    *,
    identifier: int,
    attempt_number: int = 1,
    status: ScheduledJobAttemptStatus = ScheduledJobAttemptStatus.SUCCEEDED,
    category: ScheduledJobFailureCategory = ScheduledJobFailureCategory.TRANSPORT,
    evidence_changed: bool = False,
    started_offset_seconds: int = 60,
    effective_known_at_offset_seconds: int = 0,
) -> ScheduledJobAttempt:
    scheduled_for = definition.scheduled_for(local_date)
    started_at = scheduled_for + timedelta(seconds=started_offset_seconds)
    if status is ScheduledJobAttemptStatus.RUNNING:
        return ScheduledJobAttempt(
            attempt_id=UUID(f"00000000-0000-4000-8000-{identifier:012d}"),
            definition=definition,
            local_date=local_date,
            scheduled_for=scheduled_for,
            attempt_number=attempt_number,
            status=status,
            started_at=started_at,
        )
    completed_at = started_at + timedelta(seconds=30)
    values: dict[str, object] = {
        "attempt_id": UUID(f"00000000-0000-4000-8000-{identifier:012d}"),
        "definition": definition,
        "local_date": local_date,
        "scheduled_for": scheduled_for,
        "attempt_number": attempt_number,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    if status is ScheduledJobAttemptStatus.SUCCEEDED:
        values["execution"] = ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=completed_at + timedelta(seconds=effective_known_at_offset_seconds),
            evidence_changed=evidence_changed,
            source_ids=(_SOURCE_ID,),
            created_count=1 if evidence_changed else 0,
            reused_count=0 if evidence_changed else 1,
            coverage_complete=True,
        )
    else:
        values["failure"] = scheduled_job_failure(category, "bounded test failure")
    return ScheduledJobAttempt.model_validate(values)


def _legacy_failure(
    definition: ScheduledJobDefinition,
    local_date: date,
    *,
    identifier: int,
) -> ScheduledJobAttempt:
    scheduled_for = definition.scheduled_for(local_date)
    return ScheduledJobAttempt.model_validate(
        {
            "attempt_id": UUID(f"00000000-0000-4000-8000-{identifier:012d}"),
            "definition": definition,
            "local_date": local_date,
            "scheduled_for": scheduled_for,
            "attempt_number": 1,
            "status": "failed",
            "started_at": scheduled_for + timedelta(seconds=60),
            "completed_at": scheduled_for + timedelta(seconds=90),
            "failure": {
                "category": "legacy-provider-error",
                "message": "bounded historical failure",
                "retryable": False,
            },
        },
        context={"allow_legacy_failure_categories": True},
    )


def _baseline_attempts(*, screened_first: bool = True) -> tuple[ScheduledJobAttempt, ...]:
    attempts: list[ScheduledJobAttempt] = []
    identifier = 1
    for job_id in ("a-market", "b-market"):
        definition = _definition(job_id)
        for day in (1, 2, 3):
            attempts.append(
                _attempt(
                    definition,
                    date(2026, 8, day),
                    identifier=identifier,
                    evidence_changed=screened_first and identifier == 1,
                )
            )
            identifier += 1
    return tuple(attempts)


def _persist_workspace(
    tmp_path: Path,
    attempts: tuple[ScheduledJobAttempt, ...],
    *,
    active_manual: bool = False,
    screened_processed_offset_seconds: int = 1,
) -> Path:
    root = tmp_path / "workspace"
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    paths = service.initialize(root).paths
    schedule_store = MultiAssetScheduleStateStore(paths.state_root / _SCHEDULE)
    operational_store = OperationalAlertStateStore(paths.state_root / _OPERATIONAL)
    analytical_store = AnalyticalScreeningStateStore(paths.state_root / _ANALYTICAL)
    operational_monitor = OperationalAlertMonitor(
        operational_store,
        clock=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    for attempt in attempts:
        schedule_store.write_attempt(attempt)
        if attempt.status is ScheduledJobAttemptStatus.RUNNING:
            continue
        operational_monitor(attempt)
        assert attempt.completed_at is not None
        if (
            attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
            and attempt.execution is not None
            and attempt.execution.evidence_changed
        ):
            processed_at = attempt.completed_at + timedelta(
                seconds=screened_processed_offset_seconds
            )
            result = AnalyticalScreeningEngine().evaluate(
                AnalyticalScreeningRequest(
                    rule=INITIAL_MARKET_ACTIVITY_RULE,
                    asset_id="equity:us:aapl",
                    asset_class=AssetClass.EQUITY,
                    source_id=_SOURCE_ID,
                    known_at=attempt.execution.effective_known_at,
                    computed_at=processed_at,
                    metrics=(),
                )
            )
            analytical_store.record_attempt(
                AnalyticalMonitorReceipt(
                    attempt_id=attempt.attempt_id,
                    job_id=attempt.definition.job_id,
                    asset_id=attempt.definition.asset_id,
                    status=AnalyticalMonitorReceiptStatus.SCREENED,
                    reason="new_compatible_evidence",
                    processed_at=processed_at,
                    result_ids=(result.result_id,),
                ),
                (result,),
            )
        else:
            analytical_store.record_attempt(
                AnalyticalMonitorReceipt(
                    attempt_id=attempt.attempt_id,
                    job_id=attempt.definition.job_id,
                    asset_id=attempt.definition.asset_id,
                    status=AnalyticalMonitorReceiptStatus.SKIPPED,
                    reason=(
                        "unchanged_evidence"
                        if attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
                        else f"attempt_{attempt.status.value}"
                    ),
                    processed_at=attempt.completed_at,
                ),
                (),
            )
    if active_manual:
        request = ManualOperationRequest(
            operation_kind=ManualOperationKind.MARKET_DAILY,
            payload={
                "asset_id": "crypto:btc-usd",
                "market_start": "2026-08-01",
                "market_end": "2026-08-02",
                "refresh_mode": "auto",
                "requested_known_at": "2026-08-03T00:00:00Z",
            },
        )
        ManualOperationStateStore(paths.state_root / _MANUAL).write(
            ManualOperationState(
                operation_id=UUID("10000000-0000-4000-8000-000000000001"),
                fingerprint=request.fingerprint,
                request=request,
                status=ManualOperationStatus.QUEUED,
                submitted_at=datetime(2026, 8, 3, tzinfo=UTC),
            )
        )
    return root


def _check(root: Path, *, minimum: int = 3):
    return OperationalReadinessService(
        WorkspaceService(environ={}, home=root.parent / "home")
    ).check(workspace=root, since=_SINCE, min_local_dates=minimum)


def _rewrite_json(path: Path, transform) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    transform(document)
    path.write_text(
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_pass_is_strict_deterministic_and_accepts_screened_and_skipped_receipts(
    tmp_path: Path,
) -> None:
    root = _persist_workspace(tmp_path, _baseline_attempts())

    first = _check(root)
    second = _check(root)

    assert first == second
    assert first.decision is OperationalReadinessDecision.PASS
    assert first.reason_codes == ()
    assert first.summary.terminal_attempt_count == 6
    assert first.summary.operational_screening_count == 24
    assert first.summary.analytical_receipt_count == 6
    assert first.summary.qualifying_daily_job_count == 2
    assert first.summary.maximum_local_date_count == 3
    assert first.summary.manual_operation_state_present is False
    assert len(first.semantic_fingerprint) == 64
    assert len(first.summary.evidence_fingerprint) == 64


def test_screened_receipt_may_predate_completion_when_semantic_join_is_exact(
    tmp_path: Path,
) -> None:
    definition = _definition("clock-inversion")
    attempts = tuple(
        _attempt(
            definition,
            date(2026, 8, day),
            identifier=day,
            evidence_changed=day == 1,
            effective_known_at_offset_seconds=-5 if day == 1 else 0,
        )
        for day in (1, 2, 3)
    )
    root = _persist_workspace(
        tmp_path,
        attempts,
        screened_processed_offset_seconds=-1,
    )
    state = AnalyticalScreeningStateStore(root / "state" / _ANALYTICAL).load()
    receipt = next(
        item for item in state.receipts if item.status is AnalyticalMonitorReceiptStatus.SCREENED
    )
    screened_attempt = attempts[0]
    assert screened_attempt.completed_at is not None
    assert screened_attempt.execution is not None
    assert receipt.processed_at < screened_attempt.completed_at
    assert receipt.processed_at >= screened_attempt.execution.effective_known_at

    report = _check(root)

    assert report.decision is OperationalReadinessDecision.PASS
    assert report.reason_codes == ()


def _screened_join_fixture():
    attempt = _attempt(
        _definition("screened-join"),
        date(2026, 8, 1),
        identifier=1,
        evidence_changed=True,
        effective_known_at_offset_seconds=-5,
    )
    assert attempt.completed_at is not None
    assert attempt.execution is not None
    processed_at = attempt.completed_at - timedelta(seconds=1)
    result = AnalyticalScreeningEngine().evaluate(
        AnalyticalScreeningRequest(
            rule=INITIAL_MARKET_ACTIVITY_RULE,
            asset_id=attempt.definition.asset_id,
            asset_class=AssetClass.EQUITY,
            source_id=_SOURCE_ID,
            known_at=attempt.execution.effective_known_at,
            computed_at=processed_at,
            metrics=(),
        )
    )
    receipt = AnalyticalMonitorReceipt(
        attempt_id=attempt.attempt_id,
        job_id=attempt.definition.job_id,
        asset_id=attempt.definition.asset_id,
        status=AnalyticalMonitorReceiptStatus.SCREENED,
        reason="new_compatible_evidence",
        processed_at=processed_at,
        result_ids=(result.result_id,),
    )
    return attempt, receipt, AnalyticalScreeningState(results=(result,), receipts=(receipt,))


@pytest.mark.parametrize("offset_seconds", [-1, 1])
def test_skipped_receipt_requires_exact_completion_timestamp(offset_seconds: int) -> None:
    attempt = _attempt(_definition("skipped-join"), date(2026, 8, 1), identifier=1)
    assert attempt.completed_at is not None
    receipt = AnalyticalMonitorReceipt(
        attempt_id=attempt.attempt_id,
        job_id=attempt.definition.job_id,
        asset_id=attempt.definition.asset_id,
        status=AnalyticalMonitorReceiptStatus.SKIPPED,
        reason="unchanged_evidence",
        processed_at=attempt.completed_at + timedelta(seconds=offset_seconds),
    )
    reasons: set[OperationalReadinessReasonCode] = set()

    OperationalReadinessService._check_analytical_join(
        attempt,
        receipt,
        AnalyticalScreeningState(receipts=(receipt,)),
        reasons,
    )

    assert reasons == {OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("job", OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE),
        ("asset", OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE),
        ("result", OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE),
        ("source", OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE),
        ("known_at", OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE),
        ("computed_at", OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE),
        ("traceability", OperationalReadinessReasonCode.ANALYTICAL_RESULT_JOIN_INCOMPLETE),
    ],
)
def test_screened_receipt_semantic_join_corruption_remains_fail_closed(
    mode: str,
    expected: OperationalReadinessReasonCode,
) -> None:
    attempt, receipt, state = _screened_join_fixture()
    result = state.results[0]
    if mode == "job":
        receipt = receipt.model_copy(update={"job_id": "other-job"})
    elif mode == "asset":
        receipt = receipt.model_copy(update={"asset_id": "equity:us:other"})
    elif mode == "result":
        receipt = receipt.model_copy(
            update={"result_ids": (UUID("20000000-0000-4000-8000-000000000001"),)}
        )
    elif mode == "source":
        result = result.model_copy(update={"source_id": "other-source"})
    elif mode == "known_at":
        result = result.model_copy(update={"known_at": result.known_at + timedelta(seconds=1)})
    elif mode == "computed_at":
        result = result.model_copy(
            update={"computed_at": result.computed_at + timedelta(seconds=1)}
        )
    else:
        result = result.model_copy(update={"traceability_verified": False})
    state = state.model_copy(update={"results": (result,)})
    reasons: set[OperationalReadinessReasonCode] = set()

    OperationalReadinessService._check_analytical_join(attempt, receipt, state, reasons)

    assert reasons == {expected}


def test_parameters_reject_naive_non_utc_bool_and_invalid_minimum() -> None:
    with pytest.raises(ValidationError):
        OperationalReadinessParameters(since=datetime(2026, 8, 1), min_local_dates=3)
    with pytest.raises(ValidationError):
        OperationalReadinessParameters(
            since=datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=1))),
            min_local_dates=3,
        )
    with pytest.raises(ValidationError):
        OperationalReadinessParameters(since=_SINCE, min_local_dates=True)
    with pytest.raises(ValidationError):
        OperationalReadinessParameters(since=_SINCE, min_local_dates=0)


def test_insufficient_dates_running_and_active_manual_are_not_ready(tmp_path: Path) -> None:
    running = _attempt(
        _definition("running-job"),
        date(2026, 8, 3),
        identifier=99,
        status=ScheduledJobAttemptStatus.RUNNING,
    )
    root = _persist_workspace(
        tmp_path,
        _baseline_attempts(screened_first=False) + (running,),
        active_manual=True,
    )

    report = _check(root, minimum=4)

    assert report.decision is OperationalReadinessDecision.NOT_READY
    assert set(report.reason_codes) == {
        OperationalReadinessReasonCode.ACTIVE_MANUAL_OPERATION,
        OperationalReadinessReasonCode.INSUFFICIENT_LOCAL_DATES,
        OperationalReadinessReasonCode.RUNNING_SCHEDULE_ATTEMPT,
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("retry_not_allowed", OperationalReadinessReasonCode.RETRY_NOT_ALLOWED),
        ("budget", OperationalReadinessReasonCode.ATTEMPT_BUDGET_EXCEEDED),
        ("backoff", OperationalReadinessReasonCode.RETRY_BACKOFF_VIOLATION),
    ],
)
def test_retry_policy_failures_are_explicit(
    tmp_path: Path,
    mode: str,
    expected: OperationalReadinessReasonCode,
) -> None:
    definition = _definition("retry-job", max_attempts=1 if mode == "budget" else 3)
    category = (
        ScheduledJobFailureCategory.PROVIDER_CONTRACT
        if mode == "retry_not_allowed"
        else ScheduledJobFailureCategory.TRANSPORT
    )
    first = _attempt(
        definition,
        date(2026, 8, 3),
        identifier=70,
        status=ScheduledJobAttemptStatus.FAILED,
        category=category,
    )
    second = _attempt(
        definition,
        date(2026, 8, 3),
        identifier=71,
        attempt_number=2,
        started_offset_seconds=100 if mode == "backoff" else 180,
    )
    root = _persist_workspace(tmp_path, _baseline_attempts(screened_first=False) + (first, second))

    report = _check(root)

    assert report.decision is OperationalReadinessDecision.NOT_READY
    assert expected in report.reason_codes


def test_legacy_category_after_cut_is_not_ready(tmp_path: Path) -> None:
    legacy = _legacy_failure(_definition("legacy-job"), date(2026, 8, 3), identifier=80)
    root = _persist_workspace(tmp_path, _baseline_attempts(screened_first=False) + (legacy,))

    report = _check(root)

    assert OperationalReadinessReasonCode.UNKNOWN_FAILURE_CATEGORY in report.reason_codes


def test_incomplete_and_orphan_observer_joins_are_not_ready(tmp_path: Path) -> None:
    attempts = _baseline_attempts(screened_first=False)
    root = _persist_workspace(tmp_path, attempts)
    state_root = root / "state"
    _rewrite_json(
        state_root / _OPERATIONAL,
        lambda document: document["screenings"].pop(0),
    )
    _rewrite_json(
        state_root / _ANALYTICAL,
        lambda document: document["receipts"].pop(0),
    )
    orphan = _attempt(
        _definition("orphan-job"),
        date(2026, 8, 3),
        identifier=90,
        status=ScheduledJobAttemptStatus.FAILED,
    )
    results = OperationalAlertEngine().evaluate(
        orphan,
        computed_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    OperationalAlertStateStore(state_root / _OPERATIONAL).record(results, ())
    assert orphan.completed_at is not None
    AnalyticalScreeningStateStore(state_root / _ANALYTICAL).record_attempt(
        AnalyticalMonitorReceipt(
            attempt_id=orphan.attempt_id,
            job_id=orphan.definition.job_id,
            asset_id=orphan.definition.asset_id,
            status=AnalyticalMonitorReceiptStatus.SKIPPED,
            reason="attempt_failed",
            processed_at=orphan.completed_at,
        ),
        (),
    )

    report = _check(root)

    assert (
        OperationalReadinessReasonCode.OPERATIONAL_SCREENING_JOIN_INCOMPLETE in report.reason_codes
    )
    assert OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_JOIN_INCOMPLETE in report.reason_codes
    assert OperationalReadinessReasonCode.OPERATIONAL_SCREENING_ORPHAN in report.reason_codes
    assert OperationalReadinessReasonCode.ANALYTICAL_RECEIPT_ORPHAN in report.reason_codes


@pytest.mark.parametrize("missing", [_SCHEDULE, _OPERATIONAL, _ANALYTICAL])
def test_required_state_missing_is_an_error(tmp_path: Path, missing: str) -> None:
    root = _persist_workspace(tmp_path, _baseline_attempts(screened_first=False))
    (root / "state" / missing).unlink()

    with pytest.raises(OperationalReadinessStateError, match="required_state_missing"):
        _check(root)


def test_malformed_state_is_an_error(tmp_path: Path) -> None:
    root = _persist_workspace(tmp_path, _baseline_attempts(screened_first=False))
    (root / "state" / _SCHEDULE).write_text("{}", encoding="utf-8")

    with pytest.raises(OperationalReadinessStateError, match="state_invalid"):
        _check(root)


def test_snapshot_change_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _persist_workspace(tmp_path, _baseline_attempts(screened_first=False))
    original = readiness_module._capture_signatures
    calls = 0

    def unstable(monitored):
        nonlocal calls
        calls += 1
        signatures = original(monitored)
        if calls == 2:
            name, signature = signatures[0]
            signatures = ((name, replace(signature, modified_ns=1)),) + signatures[1:]
        return signatures

    monkeypatch.setattr(readiness_module, "_capture_signatures", unstable)

    with pytest.raises(OperationalReadinessStateError, match="snapshot_changed"):
        _check(root)
