import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from uuid import UUID

from investment_analyst.application.multi_asset_scheduler import (
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
    OperationalAlertEventStatus,
    OperationalAlertMonitor,
    OperationalAlertState,
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
    category: ScheduledJobFailureCategory = ScheduledJobFailureCategory.TRANSPORT,
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
        base["failure"] = scheduled_job_failure(category, "safe failure")
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


def test_reconcile_recovers_after_screenings_persist_before_resolution(tmp_path: Path) -> None:
    class NoReplayEngine(OperationalAlertEngine):
        def evaluate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("recovery must not replay complete screenings")

    store = OperationalAlertStateStore(tmp_path / "alerts.json")
    failure = _attempt(ScheduledJobAttemptStatus.FAILED)
    success = _attempt(
        ScheduledJobAttemptStatus.SUCCEEDED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000107"),
    ).model_copy(
        update={
            "attempt_number": 2,
            "started_at": datetime(2026, 7, 29, 12, 5, tzinfo=UTC),
            "completed_at": datetime(2026, 7, 29, 12, 6, tzinfo=UTC),
        }
    )
    engine = OperationalAlertEngine()
    store.record(
        engine.evaluate(failure, computed_at=failure.completed_at),
        engine.events_for(engine.evaluate(failure, computed_at=failure.completed_at)),
    )
    success_results = engine.evaluate(success, computed_at=success.completed_at)
    store.record(success_results, engine.events_for(success_results))

    OperationalAlertMonitor(
        store,
        engine=NoReplayEngine(),
        clock=lambda: datetime(2026, 7, 29, 12, 7, tzinfo=UTC),
    ).reconcile((failure, success))

    state = store.load()
    assert len(state.screenings) == 8
    assert len(state.transitions) == 1
    assert state.events[0].status is OperationalAlertEventStatus.RESOLVED


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
            category=ScheduledJobFailureCategory.INTERRUPTED,
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


def test_startup_reconciliation_indexes_890_attempts_without_replaying_history(
    tmp_path: Path,
) -> None:
    class CountingStore(OperationalAlertStateStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.loads = 0

        def load(self) -> OperationalAlertState:
            self.loads += 1
            return super().load()

    class NoReplayEngine(OperationalAlertEngine):
        def evaluate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("a fully indexed attempt must not be re-evaluated")

    seed_store = OperationalAlertStateStore(tmp_path / "alerts.json")
    seed_engine = OperationalAlertEngine()
    attempts = tuple(
        _attempt(
            ScheduledJobAttemptStatus.SUCCEEDED,
            attempt_id=UUID(int=0x40000000000000000000000000000000 + index),
        )
        for index in range(1, 891)
    )
    screenings = tuple(
        result
        for attempt in attempts
        for result in seed_engine.evaluate(attempt, computed_at=attempt.completed_at)
    )
    seed_store._write(
        OperationalAlertState(
            screenings=tuple(
                sorted(screenings, key=lambda value: (value.known_at, str(value.result_id)))
            ),
            events=(),
            transitions=(),
        )
    )
    before = (tmp_path / "alerts.json").read_bytes()
    store = CountingStore(tmp_path / "alerts.json")

    started = monotonic()
    OperationalAlertMonitor(store, engine=NoReplayEngine()).reconcile(attempts)
    elapsed = monotonic() - started

    assert store.loads == 1
    assert (tmp_path / "alerts.json").read_bytes() == before
    assert len(screenings) == 3_560
    assert elapsed <= 30


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


def test_journal_append_does_not_reserialize_history_and_reconstructs_the_same_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alerts.json"
    store = OperationalAlertStateStore(path)
    engine = OperationalAlertEngine()

    attempt1 = _attempt(
        ScheduledJobAttemptStatus.FAILED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000001"),
    )
    results1 = engine.evaluate(attempt1, computed_at=attempt1.completed_at)
    events1 = engine.events_for(results1)

    store.record(results1, events1)
    manifest = store._journal._load_manifest()
    assert manifest is not None
    open_segment = store.journal_dir / manifest.open_segment_name
    assert open_segment.exists()
    size_after_first = open_segment.stat().st_size
    assert size_after_first > 0

    attempt2 = _attempt(
        ScheduledJobAttemptStatus.FAILED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000002"),
    )
    results2 = engine.evaluate(attempt2, computed_at=attempt2.completed_at)
    events2 = engine.events_for(results2)
    store.record(results2, events2)

    size_after_second = open_segment.stat().st_size
    delta = size_after_second - size_after_first
    assert delta < size_after_first * 2

    alert1_id = events1[0].alert_id
    recorded_at = datetime(2026, 7, 29, 12, 10, tzinfo=UTC)
    store.transition(alert1_id, OperationalAlertEventStatus.SEEN, recorded_at=recorded_at)

    size_after_transition = open_segment.stat().st_size
    transition_delta = size_after_transition - size_after_second
    assert 100 < transition_delta < 500

    reconstructed = store.load()
    assert len(reconstructed.screenings) == len(results1) + len(results2)
    assert len(reconstructed.events) == 2
    assert len(reconstructed.transitions) == 1
    events_by_id = {e.alert_id: e for e in reconstructed.events}
    assert events_by_id[alert1_id].status is OperationalAlertEventStatus.SEEN

    fresh_store = OperationalAlertStateStore(path)
    reloaded = fresh_store.load()
    assert reloaded.to_json_dict() == reconstructed.to_json_dict()
    assert reloaded == reconstructed


def test_legacy_v1_state_is_folded_once_and_preserved_byte_for_byte(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "operational_alert_state_v1.json"
    engine = OperationalAlertEngine()
    attempt1 = _attempt(
        ScheduledJobAttemptStatus.FAILED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000010"),
    )
    results1 = engine.evaluate(attempt1, computed_at=attempt1.completed_at)
    events1 = engine.events_for(results1)

    legacy_state = OperationalAlertState(
        screenings=tuple(sorted(results1, key=lambda item: (item.known_at, str(item.result_id)))),
        events=tuple(
            sorted(events1, key=lambda item: (item.first_activated_at, str(item.alert_id)))
        ),
        transitions=(),
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

    store = OperationalAlertStateStore(legacy_path)

    loaded_initial = store.load()
    assert len(loaded_initial.screenings) == len(results1)
    assert len(loaded_initial.events) == 1
    assert legacy_path.read_bytes() == original_bytes

    attempt2 = _attempt(
        ScheduledJobAttemptStatus.FAILED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000020"),
    )
    results2 = engine.evaluate(attempt2, computed_at=attempt2.completed_at)
    events2 = engine.events_for(results2)
    store.record(results2, events2)

    assert legacy_path.read_bytes() == original_bytes

    reloaded = store.load()
    assert len(reloaded.screenings) == len(results1) + len(results2)
    assert len(reloaded.events) == 2

    assert store._journal.has_legacy_v1_folded()
    assert store._journal.has_snapshot()

    legacy_path.unlink()
    reloaded_after_delete = store.load()
    assert len(reloaded_after_delete.screenings) == len(results1) + len(results2)
    assert len(reloaded_after_delete.events) == 2


def test_partial_trailing_line_recovers_without_losing_confirmed_transitions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "alerts.json"
    store = OperationalAlertStateStore(path)
    engine = OperationalAlertEngine()
    attempt = _attempt(ScheduledJobAttemptStatus.FAILED)
    results = engine.evaluate(attempt, computed_at=attempt.completed_at)
    store.record(results, engine.events_for(results))

    alert_id = store.inbox().events[0].alert_id
    t1 = datetime(2026, 7, 29, 12, 1, tzinfo=UTC)
    t2 = datetime(2026, 7, 29, 12, 2, tzinfo=UTC)

    store.transition(alert_id, OperationalAlertEventStatus.SEEN, recorded_at=t1)
    store.transition(alert_id, OperationalAlertEventStatus.DISMISSED, recorded_at=t2)

    manifest = store._journal._load_manifest()
    assert manifest is not None
    open_segment = store.journal_dir / manifest.open_segment_name

    with open(open_segment, "ab") as stream:
        stream.write(b'{"collection": "transition", "incomplete": true, "raw_prefix": "abc')

    fresh_store = OperationalAlertStateStore(path)
    recovered_state = fresh_store.load()
    assert len(recovered_state.transitions) == 2
    assert recovered_state.transitions[0].to_status is OperationalAlertEventStatus.SEEN
    assert recovered_state.transitions[1].to_status is OperationalAlertEventStatus.DISMISSED
    assert recovered_state.events[0].status is OperationalAlertEventStatus.DISMISSED

    t3 = datetime(2026, 7, 29, 12, 3, tzinfo=UTC)
    fresh_store.transition(alert_id, OperationalAlertEventStatus.RESOLVED, recorded_at=t3)
    final_state = fresh_store.load()
    assert len(final_state.transitions) == 3
    assert final_state.transitions[2].to_status is OperationalAlertEventStatus.RESOLVED
    assert final_state.events[0].status is OperationalAlertEventStatus.RESOLVED
