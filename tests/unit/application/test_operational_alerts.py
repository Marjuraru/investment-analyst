"""Tests for deterministic silent operational screening and local alerts."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailure,
)
from investment_analyst.application.operational_alerts import (
    OperationalAlertEngine,
    OperationalAlertEventStatus,
    OperationalAlertMonitor,
    OperationalAlertStateStore,
    OperationalRuleId,
    ScreeningConditionState,
)

_DEFAULT_ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000101")


def _definition() -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id="alpaca:equity:us:amd:market-daily",
        asset_id="equity:us:amd",
        provider="alpaca",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
    )


def _attempt(
    status: ScheduledJobAttemptStatus,
    *,
    attempt_id: UUID = _DEFAULT_ATTEMPT_ID,
    category: str = "provider_unavailable",
    coverage_complete: bool = True,
) -> ScheduledJobAttempt:
    definition = _definition()
    base = {
        "attempt_id": attempt_id,
        "definition": definition,
        "local_date": date(2026, 7, 29),
        "scheduled_for": datetime(2026, 7, 29, 12, tzinfo=UTC),
        "attempt_number": 1,
        "status": status,
        "started_at": datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        "completed_at": datetime(2026, 7, 29, 12, 2, tzinfo=UTC),
    }
    if status is ScheduledJobAttemptStatus.SUCCEEDED:
        base["execution"] = ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
            evidence_changed=False,
            source_ids=("alpaca:test",),
            created_count=0,
            reused_count=20,
            coverage_complete=coverage_complete,
        )
    else:
        base["failure"] = ScheduledJobFailure(
            category=category,
            message="safe failure",
            retryable=status is ScheduledJobAttemptStatus.FAILED,
        )
    return ScheduledJobAttempt.model_validate(base)


def test_success_is_screened_trivalued_without_creating_alert(tmp_path: Path) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    monitor = OperationalAlertMonitor(
        store,
        clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )

    monitor(_attempt(ScheduledJobAttemptStatus.SUCCEEDED))

    state = store.load()
    assert len(state.screenings) == 4
    assert all(item.condition.state is ScreeningConditionState.NOT_MET for item in state.screenings)
    assert state.events == ()
    assert store.status().screening_results == 4
    assert store.status().new_count == 0


def test_complete_success_resolves_prior_job_alerts_with_audited_system_transition(
    tmp_path: Path,
) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    monitor = OperationalAlertMonitor(
        store,
        clock=lambda: datetime(2026, 7, 29, 12, 7, tzinfo=UTC),
    )
    failure = _attempt(ScheduledJobAttemptStatus.FAILED)
    success = _attempt(
        ScheduledJobAttemptStatus.SUCCEEDED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000105"),
    ).model_copy(
        update={
            "attempt_number": 2,
            "started_at": datetime(2026, 7, 29, 12, 5, tzinfo=UTC),
            "completed_at": datetime(2026, 7, 29, 12, 6, tzinfo=UTC),
        }
    )

    monitor(failure)
    assert (
        store.resolve_recovered_job(
            "sec:equity:us:amd:fundamentals-quarterly",
            recovered_at=datetime(2026, 7, 29, 12, 6, tzinfo=UTC),
            recorded_at=datetime(2026, 7, 29, 12, 7, tzinfo=UTC),
        )
        == 0
    )
    monitor(success)
    monitor.reconcile((failure, success))

    state = store.load()
    assert len(state.screenings) == 8
    assert len(state.events) == 1
    assert state.events[0].status is OperationalAlertEventStatus.RESOLVED
    assert len(state.transitions) == 1
    assert state.transitions[0].actor == "system_recovery"
    assert state.transitions[0].from_status is OperationalAlertEventStatus.NEW
    assert state.transitions[0].to_status is OperationalAlertEventStatus.RESOLVED
    assert store.status().new_count == 0


def test_incomplete_success_does_not_resolve_prior_job_failure(tmp_path: Path) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    monitor = OperationalAlertMonitor(
        store,
        clock=lambda: datetime(2026, 7, 29, 12, 7, tzinfo=UTC),
    )
    failure = _attempt(ScheduledJobAttemptStatus.FAILED)
    incomplete = _attempt(
        ScheduledJobAttemptStatus.SUCCEEDED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000106"),
        coverage_complete=False,
    ).model_copy(
        update={
            "attempt_number": 2,
            "started_at": datetime(2026, 7, 29, 12, 5, tzinfo=UTC),
            "completed_at": datetime(2026, 7, 29, 12, 6, tzinfo=UTC),
        }
    )

    monitor(failure)
    monitor(incomplete)

    state = store.load()
    assert len(state.events) == 2
    assert all(item.status is OperationalAlertEventStatus.NEW for item in state.events)
    assert state.transitions == ()
    assert store.status().new_count == 2


def test_failure_creates_one_idempotent_silent_inbox_event(tmp_path: Path) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    engine = OperationalAlertEngine()
    attempt = _attempt(ScheduledJobAttemptStatus.FAILED)
    first_results = engine.evaluate(
        attempt,
        computed_at=datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )
    second_results = engine.evaluate(
        attempt,
        computed_at=datetime(2026, 7, 29, 12, 4, tzinfo=UTC),
    )

    assert tuple(item.result_id for item in first_results) == tuple(
        item.result_id for item in second_results
    )
    assert store.record(first_results, engine.events_for(first_results)) == (4, 1)
    assert store.record(second_results, engine.events_for(second_results)) == (0, 0)

    inbox = store.inbox()
    assert inbox.total == 1
    assert inbox.events[0].rule_id is OperationalRuleId.JOB_FAILED
    assert inbox.events[0].asset_id == "equity:us:amd"
    assert store.status().silent_mode is True
    assert store.status().new_count == 1


def test_interruption_uses_specific_rule_without_duplicate_failure_alert(
    tmp_path: Path,
) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    monitor = OperationalAlertMonitor(
        store,
        clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )

    monitor(
        _attempt(
            ScheduledJobAttemptStatus.FAILED,
            attempt_id=UUID("00000000-0000-4000-8000-000000000102"),
            category="interrupted_job",
        )
    )

    inbox = store.inbox()
    assert inbox.total == 1
    assert inbox.events[0].rule_id is OperationalRuleId.JOB_INTERRUPTED
    assert "interrumpida" in inbox.events[0].message


def test_inbox_is_bounded_and_newest_first(tmp_path: Path) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    engine = OperationalAlertEngine()
    first = _attempt(ScheduledJobAttemptStatus.FAILED)
    second = _attempt(
        ScheduledJobAttemptStatus.FAILED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000103"),
    )
    second = second.model_copy(
        update={
            "started_at": second.started_at + timedelta(minutes=2),
            "completed_at": second.completed_at + timedelta(minutes=2),
        }
    )
    first_results = engine.evaluate(first, computed_at=first.completed_at)
    second_results = engine.evaluate(second, computed_at=second.completed_at)
    store.record(first_results, engine.events_for(first_results))
    store.record(second_results, engine.events_for(second_results))

    inbox = store.inbox(limit=1)

    assert inbox.total == 2
    assert len(inbox.events) == 1
    assert inbox.events[0].result_id in {item.result_id for item in second_results}


def test_monitor_reconciles_durable_attempts_idempotently(tmp_path: Path) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    monitor = OperationalAlertMonitor(
        store,
        clock=lambda: datetime(2026, 7, 29, 12, 5, tzinfo=UTC),
    )
    attempts = (
        _attempt(ScheduledJobAttemptStatus.SUCCEEDED),
        _attempt(
            ScheduledJobAttemptStatus.FAILED,
            attempt_id=UUID("00000000-0000-4000-8000-000000000104"),
        ),
    )

    monitor.reconcile(attempts)
    first = store.load()
    monitor.reconcile(attempts)

    assert store.load() == first
    assert len(first.screenings) == 8
    assert len(first.events) == 1


def test_incomplete_coverage_creates_specific_alert(tmp_path: Path) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    monitor = OperationalAlertMonitor(
        store,
        clock=lambda: datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
    )

    monitor(_attempt(ScheduledJobAttemptStatus.SUCCEEDED, coverage_complete=False))

    inbox = store.inbox()
    assert inbox.total == 1
    assert inbox.events[0].rule_id is OperationalRuleId.JOB_COVERAGE_INCOMPLETE
    assert "cobertura" in inbox.events[0].message.casefold()


def test_alert_transitions_are_audited_idempotent_and_survive_replay(
    tmp_path: Path,
) -> None:
    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    engine = OperationalAlertEngine()
    attempt = _attempt(ScheduledJobAttemptStatus.FAILED)
    results = engine.evaluate(attempt, computed_at=attempt.completed_at)
    store.record(results, engine.events_for(results))
    alert_id = store.inbox().events[0].alert_id
    recorded_at = datetime(2026, 7, 29, 12, 4, tzinfo=UTC)

    seen, changed = store.transition(
        alert_id,
        OperationalAlertEventStatus.SEEN,
        recorded_at=recorded_at,
    )
    repeated, repeated_changed = store.transition(
        alert_id,
        OperationalAlertEventStatus.SEEN,
        recorded_at=recorded_at + timedelta(minutes=1),
    )
    store.record(results, engine.events_for(results))

    state = store.load()
    assert changed is True
    assert repeated_changed is False
    assert seen == repeated
    assert seen.status is OperationalAlertEventStatus.SEEN
    assert len(state.transitions) == 1
    assert state.transitions[0].from_status is OperationalAlertEventStatus.NEW
    assert state.transitions[0].to_status is OperationalAlertEventStatus.SEEN
    assert store.status().new_count == 0
