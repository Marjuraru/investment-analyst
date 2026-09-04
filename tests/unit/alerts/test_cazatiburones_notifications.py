"""Unit tests for projection, deduplication, and append-only outbox behavior."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.alerts.cazatiburones_notification_models import (
    notification_id,
)
from investment_analyst.alerts.cazatiburones_notifications import (
    CazatiburonesNotificationReconciliationError,
    CazatiburonesNotificationStateError,
    CazatiburonesNotificationStore,
    project_cazatiburones_notifications,
)
from investment_analyst.analytics.cazatiburones.activity_event_models import (
    ActivityCandidate,
    ActivityEvent,
    ActivityEventSnapshot,
)
from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalCandidate,
    InstitutionalEvent,
    InstitutionalEventSnapshot,
)

_ASSET_ID = "equity:us:aapl"
_T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


def _activity_event(*, label: str, available_at: datetime) -> ActivityEvent:
    return ActivityEvent(
        event_id=uuid4(),
        asset_id=_ASSET_ID,
        rule_id="insider-holding-increased",
        metric_result_id=uuid4(),
        metric_key="cazatiburones.insider.holding_delta_ratio",
        unit="ratio",
        value=Decimal("1.250000000000000001"),
        available_at=available_at,
        input_observation_ids=(uuid4(), uuid4()),
        parameters={
            "algorithm_version": "cazatiburones-activity-metrics-v1",
            "participant_cik": label,
        },
    )


def _activity_snapshot(
    *,
    snapshot_id_value,
    event: ActivityEvent,
    candidate: ActivityCandidate,
    known_at: datetime,
    recorded_at: datetime,
) -> ActivityEventSnapshot:
    return ActivityEventSnapshot(
        snapshot_id=snapshot_id_value,
        asset_id=_ASSET_ID,
        known_at=known_at,
        recorded_at=recorded_at,
        policy_version="cazatiburones-persisted-activity-events-v1",
        evaluations=(),
        events=(event,),
        candidates=(candidate,),
    )


def _institutional_snapshot() -> InstitutionalEventSnapshot:
    event = InstitutionalEvent(
        event_id=uuid4(),
        asset_id=_ASSET_ID,
        manager_cik="0001350694",
        report_period="2025-12-31",
        prior_report_period="2025-09-30",
        cusip="037833100",
        title_of_class="COM",
        put_call=None,
        rule_id="institutional-delta-reported-shares-increased",
        metric_result_id=uuid4(),
        metric_key="cazatiburones.institutional.delta_reported_shares",
        algorithm_version="cazatiburones-institutional-metrics-v1",
        unit="shares",
        value=Decimal("2500.000000000000000001"),
        available_at=_T0 + timedelta(hours=1),
        input_observation_ids=(uuid4(), uuid4()),
        parameters={
            "manager_cik": "0001350694",
            "report_period": "2025-12-31",
            "prior_report_period": "2025-09-30",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
        },
    )
    candidate = InstitutionalCandidate(
        candidate_id=uuid4(),
        event_id=event.event_id,
        status="eligible",
    )
    return InstitutionalEventSnapshot(
        snapshot_id=uuid4(),
        asset_id=_ASSET_ID,
        manager_cik="0001350694",
        known_at=_T0 + timedelta(days=1),
        recorded_at=_T0 + timedelta(minutes=3),
        policy_version="cazatiburones-persisted-institutional-events-v1",
        evaluations=(),
        events=(event,),
        candidates=(candidate,),
    )


def test_only_eligible_candidates_are_projected_and_families_stay_separate() -> None:
    eligible_event = _activity_event(label="0000000001", available_at=_T0)
    suppressed_event = _activity_event(label="0000000002", available_at=_T0 + timedelta(hours=1))
    eligible_candidate = ActivityCandidate(
        candidate_id=uuid4(), event_id=eligible_event.event_id, status="eligible"
    )
    suppressed_candidate = ActivityCandidate(
        candidate_id=uuid4(),
        event_id=suppressed_event.event_id,
        status="suppressed",
        cooldown_until=_T0 + timedelta(days=1),
        suppressed_by_event_id=eligible_event.event_id,
    )
    activity = ActivityEventSnapshot(
        snapshot_id=uuid4(),
        asset_id=_ASSET_ID,
        known_at=_T0 + timedelta(days=1),
        recorded_at=_T0 + timedelta(minutes=1),
        policy_version="cazatiburones-persisted-activity-events-v1",
        evaluations=(),
        events=(eligible_event, suppressed_event),
        candidates=(eligible_candidate, suppressed_candidate),
    )
    institutional = _institutional_snapshot()

    items = project_cazatiburones_notifications((activity,), (institutional,))

    assert len(items) == 2
    assert {item.family for item in items} == {"activity", "institutional"}
    assert suppressed_candidate.candidate_id not in {item.candidate_id for item in items}


def test_deduplication_by_family_and_candidate_id_across_snapshots() -> None:
    event = _activity_event(label="0000000001", available_at=_T0)
    candidate = ActivityCandidate(candidate_id=uuid4(), event_id=event.event_id, status="eligible")
    first_snapshot = _activity_snapshot(
        snapshot_id_value=uuid4(),
        event=event,
        candidate=candidate,
        known_at=_T0 + timedelta(hours=2),
        recorded_at=_T0 + timedelta(minutes=1),
    )
    second_snapshot = _activity_snapshot(
        snapshot_id_value=uuid4(),
        event=event,
        candidate=candidate,
        known_at=_T0 + timedelta(days=1),
        recorded_at=_T0 + timedelta(minutes=2),
    )

    items = project_cazatiburones_notifications(
        (second_snapshot, first_snapshot),
        (),
    )

    assert len(items) == 1
    assert items[0].snapshot_id == first_snapshot.snapshot_id
    assert items[0].notification_id == notification_id("activity", candidate.candidate_id)


def test_distinct_snapshot_recording_clocks_produce_identical_content_and_order() -> None:
    event = _activity_event(label="0000000001", available_at=_T0)
    candidate = ActivityCandidate(candidate_id=uuid4(), event_id=event.event_id, status="eligible")
    first = _activity_snapshot(
        snapshot_id_value=uuid4(),
        event=event,
        candidate=candidate,
        known_at=_T0 + timedelta(hours=2),
        recorded_at=_T0 + timedelta(minutes=1),
    )
    second = first.model_copy(update={"recorded_at": _T0 + timedelta(days=10)})

    assert project_cazatiburones_notifications((first,), ()) == project_cazatiburones_notifications(
        (second,), ()
    )


def test_created_at_is_event_available_at_and_traceability_preserves_decimal() -> None:
    event = _activity_event(label="0000000001", available_at=_T0)
    candidate = ActivityCandidate(candidate_id=uuid4(), event_id=event.event_id, status="eligible")
    item = project_cazatiburones_notifications(
        (
            _activity_snapshot(
                snapshot_id_value=uuid4(),
                event=event,
                candidate=candidate,
                known_at=_T0 + timedelta(days=1),
                recorded_at=_T0 + timedelta(minutes=1),
            ),
        ),
        (),
    )[0]

    assert item.created_at == event.available_at
    assert item.event_id == event.event_id
    assert item.metric_result_id == event.metric_result_id
    assert item.input_observation_ids == event.input_observation_ids
    assert item.value == Decimal("1.250000000000000001")
    assert isinstance(item.value, Decimal)
    forbidden = {"score", "ranking", "verdict", "recommendation", "signal", "percentile"}
    assert not forbidden.intersection(item.model_dump())


def test_append_only_acknowledgement_is_idempotent(tmp_path: Path) -> None:
    event = _activity_event(label="0000000001", available_at=_T0)
    candidate = ActivityCandidate(candidate_id=uuid4(), event_id=event.event_id, status="eligible")
    item = project_cazatiburones_notifications(
        (
            _activity_snapshot(
                snapshot_id_value=uuid4(),
                event=event,
                candidate=candidate,
                known_at=_T0 + timedelta(days=1),
                recorded_at=_T0 + timedelta(minutes=1),
            ),
        ),
        (),
    )[0]
    store = CazatiburonesNotificationStore(tmp_path / "outbox.json")

    assert store.enqueue(item)[1] is True
    assert store.enqueue(item)[1] is False
    first = store.acknowledge(item.notification_id, recorded_at=_T0 + timedelta(days=2))
    second = store.acknowledge(item.notification_id, recorded_at=_T0 + timedelta(days=3))

    assert first[1] is True
    assert second[1] is False
    state = store.load()
    assert state.items == (item,)
    assert len(state.acknowledgements) == 1


def test_divergent_content_under_same_identity_fails_closed(tmp_path: Path) -> None:
    event = _activity_event(label="0000000001", available_at=_T0)
    candidate = ActivityCandidate(candidate_id=uuid4(), event_id=event.event_id, status="eligible")
    original = project_cazatiburones_notifications(
        (
            _activity_snapshot(
                snapshot_id_value=uuid4(),
                event=event,
                candidate=candidate,
                known_at=_T0 + timedelta(days=1),
                recorded_at=_T0 + timedelta(minutes=1),
            ),
        ),
        (),
    )[0]
    divergent = original.model_copy(update={"value": Decimal("2")})
    store = CazatiburonesNotificationStore(tmp_path / "outbox.json")
    store.enqueue(original)

    with pytest.raises(CazatiburonesNotificationStateError, match="changed semantics"):
        store.enqueue(divergent)

    assert store.load().items == (original,)


def test_malformed_outbox_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "malformed.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(CazatiburonesNotificationStateError, match="malformed"):
        CazatiburonesNotificationStore(path).load()


def test_missing_event_and_pit_violation_fail_closed() -> None:
    event = _activity_event(label="0000000001", available_at=_T0)
    missing = ActivityCandidate(candidate_id=uuid4(), event_id=uuid4(), status="eligible")
    snapshot = ActivityEventSnapshot.model_construct(
        snapshot_id=uuid4(),
        asset_id=_ASSET_ID,
        known_at=_T0 + timedelta(days=1),
        recorded_at=_T0,
        policy_version="cazatiburones-persisted-activity-events-v1",
        evaluations=(),
        events=(event,),
        candidates=(missing,),
    )
    with pytest.raises(CazatiburonesNotificationReconciliationError, match="missing event"):
        project_cazatiburones_notifications((snapshot,), ())
