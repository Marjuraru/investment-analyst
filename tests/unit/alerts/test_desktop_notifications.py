"""Focused tests for bounded Windows toast delivery evidence."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from investment_analyst.alerts.analytical_models import (
    AnalyticalRuleState,
)
from investment_analyst.alerts.analytical_state import AnalyticalCandidateStatus
from investment_analyst.alerts.candidate_notifications import (
    CandidateNotification,
    CandidateNotificationPayload,
    CandidateNotificationState,
    CandidateNotificationTransition,
    notification_id,
    notification_transition_id,
)
from investment_analyst.alerts.desktop_notification_models import (
    DesktopNotificationDeliveryStatus,
    DesktopNotificationReason,
    desktop_notification_delivery_id,
)
from investment_analyst.alerts.desktop_notifications import (
    DesktopNotificationBudgetError,
    DesktopNotificationChannelError,
    DesktopNotificationError,
    DesktopNotificationMonitor,
    DesktopNotificationRateLimitError,
    DesktopNotificationService,
    DesktopNotificationStore,
    candidate_notification_fingerprint,
)
from investment_analyst.alerts.windows_notifications import (
    ToastMessage,
    ToastSendOutcome,
    ToastSendStatus,
    WindowsCapability,
)

_WORKSPACE = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_RULE_ID = "market.relative_volume"
_FINGERPRINT = "a" * 64


class _Rule:
    def __init__(self, state: AnalyticalRuleState) -> None:
        self.state = state
        self.rule_id = _RULE_ID

    def semantic_fingerprint(self) -> str:
        return _FINGERPRINT


class _FakeAdapter:
    def __init__(
        self,
        *,
        capability: WindowsCapability | None = None,
        statuses: list[tuple[ToastSendStatus, DesktopNotificationReason | None]] | None = None,
    ) -> None:
        self.capability = capability or WindowsCapability(
            available=True,
            reason=None,
            powershell_version="5.1.26100.9711",
            aumid_registered=True,
        )
        self.statuses = list(statuses or [(ToastSendStatus.ACCEPTED, None)])
        self.sent: list[tuple[ToastMessage, ...]] = []
        self.register_calls = 0
        self.probe_calls = 0

    def register(self) -> WindowsCapability:
        self.register_calls += 1
        return self.capability

    def probe(self) -> WindowsCapability:
        self.probe_calls += 1
        return self.capability

    def send(self, messages: tuple[ToastMessage, ...]) -> dict[str, ToastSendOutcome]:
        self.sent.append(tuple(messages))
        status, reason = self.statuses[min(len(self.sent), len(self.statuses)) - 1]
        return {message.tag: ToastSendOutcome(message.tag, status, reason) for message in messages}


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _payload() -> CandidateNotificationPayload:
    return CandidateNotificationPayload(
        rule_name_es="Volumen relativo",
        explanation_es="El volumen relativo superó el umbral.",
        conditions=(
            {
                "condition_id": "market.relative_volume",
                "state": "met",
                "metric_key": "market.history.relative_volume",
                "unit": "ratio",
                "explanation_es": "Condición satisfecha.",
            },
        ),
    )


def _notification(
    created_at: datetime, *, candidate_id: UUID | None = None
) -> CandidateNotification:
    candidate = candidate_id or uuid4()
    return CandidateNotification(
        notification_id=notification_id(candidate),
        candidate_id=candidate,
        activation_result_id=uuid4(),
        asset_id="equity:us:aapl",
        rule_id=_RULE_ID,
        as_of=created_at,
        created_at=created_at,
        payload=_payload(),
    )


def _candidate(notification: CandidateNotification, status=AnalyticalCandidateStatus.NEW):
    return SimpleNamespace(
        candidate_id=notification.candidate_id,
        activation_result_id=notification.activation_result_id,
        rule_id=notification.rule_id,
        rule_fingerprint=_FINGERPRINT,
        status=status,
    )


def _result(notification: CandidateNotification, state=AnalyticalRuleState.ACTIVE):
    return SimpleNamespace(
        result_id=notification.activation_result_id,
        rule=_Rule(state),
    )


def _rule_configuration(state=AnalyticalRuleState.ACTIVE):
    return SimpleNamespace(rule=_Rule(state), fingerprint=_FINGERPRINT)


def _batch(
    notification: CandidateNotification,
    *,
    candidate_status=AnalyticalCandidateStatus.NEW,
    rule_state=AnalyticalRuleState.ACTIVE,
    acknowledged: bool = False,
):
    candidate = _candidate(notification, candidate_status)
    result = _result(notification)
    rules = SimpleNamespace(configurations=(_rule_configuration(rule_state),))
    recorded_at = datetime(2026, 10, 30, 13, tzinfo=UTC)
    transitions = (
        (
            CandidateNotificationTransition(
                transition_id=notification_transition_id(notification.notification_id, recorded_at),
                notification_id=notification.notification_id,
                recorded_at=recorded_at,
            ),
        )
        if acknowledged
        else ()
    )
    return (
        SimpleNamespace(candidates=(candidate,), results=(result,)),
        CandidateNotificationState(items=(notification,), transitions=transitions),
        rules,
    )


def _service(
    tmp_path,
    clock: _Clock,
    adapter: _FakeAdapter,
    *,
    rules=None,
    max_state_bytes: int = 8 * 1024 * 1024,
    max_retained: int = 10_000,
) -> DesktopNotificationService:
    store = DesktopNotificationStore(
        tmp_path / "desktop_notifications",
        workspace_uuid=_WORKSPACE,
        max_state_bytes=max_state_bytes,
        max_retained=max_retained,
        journal_max_segment_bytes=16 * 1024,
        journal_max_segment_records=100,
    )
    return DesktopNotificationService(
        store,
        adapter,
        rules=rules or (lambda: SimpleNamespace(configurations=(_rule_configuration(),))),
        clock=clock,
    )


def _enable(service: DesktopNotificationService) -> None:
    service.configure(enabled=True)


def test_delivery_identity_and_windows_halves_are_deterministic() -> None:
    notification = _notification(datetime(2026, 10, 30, 11, tzinfo=UTC))
    first = desktop_notification_delivery_id(_WORKSPACE, notification.notification_id)
    second = desktop_notification_delivery_id(_WORKSPACE, notification.notification_id)
    assert first == second
    assert first.hex[:16] == second.hex[:16]
    assert first.hex[16:32] == second.hex[16:32]
    assert first.hex[:16] != first.hex[16:32]


def test_disabled_channel_performs_zero_writes_and_sends(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    notification = _notification(clock.value)
    analytical, notifications, rules = _batch(notification)
    service._rules = lambda: rules  # type: ignore[attr-defined]
    assert service.observe_batch(analytical, notifications) == 0
    assert adapter.sent == []
    state_dir = tmp_path / "desktop_notifications"
    assert not state_dir.exists() or not any(state_dir.rglob("*"))


def test_activation_boundary_admits_only_newer_unacknowledged_candidates(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    older = _notification(clock.value - timedelta(seconds=1))
    current = _notification(clock.value + timedelta(seconds=1))
    rules = SimpleNamespace(configurations=(_rule_configuration(),))
    service._rules = lambda: rules  # type: ignore[attr-defined]
    clock.value = clock.value + timedelta(seconds=2)
    service._store.mark_batch(clock.value)  # type: ignore[attr-defined]
    analytical = SimpleNamespace(
        candidates=(_candidate(older), _candidate(current)),
        results=(_result(older), _result(current)),
    )
    notifications = CandidateNotificationState(items=(older, current))
    assert service.observe_batch(analytical, notifications) == 0
    assert adapter.sent == []
    deliveries = service._store.load_deliveries()  # type: ignore[attr-defined]
    assert len(deliveries) == 1
    assert deliveries[0].notification_id == current.notification_id
    assert deliveries[0].status is DesktopNotificationDeliveryStatus.PENDING


def test_silent_rule_and_acknowledged_notification_are_not_admitted(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    rules = SimpleNamespace(configurations=(_rule_configuration(AnalyticalRuleState.SILENT),))
    service._rules = lambda: rules  # type: ignore[attr-defined]
    analytical, notifications, _ = _batch(notification, rule_state=AnalyticalRuleState.SILENT)
    clock.value = clock.value + timedelta(seconds=2)
    service._store.mark_batch(clock.value)  # type: ignore[attr-defined]
    assert service.observe_batch(analytical, notifications) == 0
    assert service._store.load_deliveries() == ()  # type: ignore[attr-defined]

    rules_active = SimpleNamespace(configurations=(_rule_configuration(),))
    service._rules = lambda: rules_active  # type: ignore[attr-defined]
    analytical, notifications, _ = _batch(notification, acknowledged=True)
    assert service.observe_batch(analytical, notifications) == 0
    assert service._store.load_deliveries() == ()  # type: ignore[attr-defined]


def test_validation_cancels_before_send_without_touching_outbox(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    clock.value = clock.value + timedelta(seconds=2)
    service._store.admit(  # type: ignore[attr-defined]
        notification,
        title=notification.payload.rule_name_es,
        body="Cuerpo",
        at=clock.value,
    )
    analytical, notifications, rules = _batch(
        notification, candidate_status=AnalyticalCandidateStatus.SEEN
    )
    service._rules = lambda: rules  # type: ignore[attr-defined]
    clock.value = clock.value + timedelta(seconds=61)
    assert service.observe_batch(analytical, notifications) == 0
    assert adapter.sent == []
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.CANCELLED
    assert delivery.reason is DesktopNotificationReason.CANDIDATE_NOT_NEW


def test_rule_change_cancels_with_incompatible_fingerprint(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    clock.value = clock.value + timedelta(seconds=2)
    service._store.admit(  # type: ignore[attr-defined]
        notification,
        title=notification.payload.rule_name_es,
        body="Cuerpo",
        at=clock.value,
    )
    changed = SimpleNamespace(
        rule=_Rule(AnalyticalRuleState.ACTIVE),
        fingerprint="b" * 64,
    )
    rules = SimpleNamespace(configurations=(changed,))
    analytical, notifications, _ = _batch(notification)
    service._rules = lambda: rules  # type: ignore[attr-defined]
    clock.value = clock.value + timedelta(seconds=61)
    assert service.observe_batch(analytical, notifications) == 0
    assert adapter.sent == []
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.CANCELLED
    assert delivery.reason is DesktopNotificationReason.RULE_FINGERPRINT_CHANGED


def test_transient_errors_follow_declared_retry_delays_and_exhaust(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter(
        statuses=[
            (ToastSendStatus.RETRYABLE, DesktopNotificationReason.SEND_ERROR),
            (ToastSendStatus.RETRYABLE, DesktopNotificationReason.SEND_ERROR),
            (ToastSendStatus.RETRYABLE, DesktopNotificationReason.SEND_ERROR),
        ]
    )
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    analytical, notifications, rules = _batch(notification)
    service._rules = lambda: rules  # type: ignore[attr-defined]
    clock.value = clock.value + timedelta(seconds=61)
    assert service.observe_batch(analytical, notifications) == 1
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.RETRY_WAIT
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at == clock.value + timedelta(seconds=60)

    clock.value = delivery.next_attempt_at
    assert service.observe_batch(analytical, notifications) == 1
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.attempt_count == 2
    assert delivery.next_attempt_at == clock.value + timedelta(seconds=300)

    clock.value = delivery.next_attempt_at
    assert service.observe_batch(analytical, notifications) == 1
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.FAILED
    assert delivery.attempt_count == 3
    assert len(adapter.sent) == 3


def test_unknown_outcome_is_never_resent(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter(statuses=[(ToastSendStatus.UNKNOWN, DesktopNotificationReason.TIMEOUT)])
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    analytical, notifications, rules = _batch(notification)
    service._rules = lambda: rules  # type: ignore[attr-defined]
    clock.value = clock.value + timedelta(seconds=61)
    assert service.observe_batch(analytical, notifications) == 1
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.UNKNOWN
    assert delivery.reason is DesktopNotificationReason.TIMEOUT
    for _ in range(3):
        clock.value = clock.value + timedelta(minutes=10)
        assert service.observe_batch(analytical, notifications) == 0
    assert len(adapter.sent) == 1


def test_restart_recovery_resolves_attempting_without_resend(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    clock.value = clock.value + timedelta(seconds=2)
    delivery, _ = service._store.admit(  # type: ignore[attr-defined]
        notification,
        title=notification.payload.rule_name_es,
        body="Cuerpo",
        at=clock.value,
    )
    service._store.begin_attempt(delivery.delivery_id, clock.value)  # type: ignore[attr-defined]
    restarted = _service(tmp_path, clock, adapter)
    assert restarted.recover() == 1
    analytical, notifications, rules = _batch(notification)
    restarted._rules = lambda: rules  # type: ignore[attr-defined]
    clock.value = clock.value + timedelta(minutes=5)
    assert restarted.observe_batch(analytical, notifications) == 0
    assert adapter.sent == []
    recovered = restarted._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert recovered.status is DesktopNotificationDeliveryStatus.UNKNOWN
    assert recovered.reason is DesktopNotificationReason.INTERRUPTED_AFTER_INVOCATION


def test_disable_cancels_pending_and_reenable_uses_a_new_boundary(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    notification = _notification(clock.value + timedelta(seconds=1))
    clock.value = clock.value + timedelta(seconds=2)
    service._store.admit(  # type: ignore[attr-defined]
        notification,
        title=notification.payload.rule_name_es,
        body="Cuerpo",
        at=clock.value,
    )
    clock.value = clock.value + timedelta(seconds=30)
    service.configure(enabled=False)
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.CANCELLED
    assert delivery.reason is DesktopNotificationReason.CHANNEL_DISABLED

    clock.value = clock.value + timedelta(seconds=30)
    service.configure(enabled=True)
    rules = SimpleNamespace(configurations=(_rule_configuration(),))
    service._rules = lambda: rules  # type: ignore[attr-defined]
    service._store.mark_batch(clock.value)  # type: ignore[attr-defined]
    analytical, notifications, _ = _batch(notification)
    service.observe_batch(analytical, notifications)
    assert len(service._store.load_deliveries()) == 1  # type: ignore[attr-defined]


def test_budget_pauses_admission_and_stays_visible(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter, max_retained=1)
    _enable(service)
    first = _notification(clock.value + timedelta(seconds=1))
    second = _notification(clock.value + timedelta(seconds=2))
    clock.value = clock.value + timedelta(seconds=3)
    service._store.admit(  # type: ignore[attr-defined]
        first,
        title=first.payload.rule_name_es,
        body="Cuerpo",
        at=clock.value,
    )
    with pytest.raises(DesktopNotificationBudgetError):
        service._store.admit(  # type: ignore[attr-defined]
            second,
            title=second.payload.rule_name_es,
            body="Cuerpo",
            at=clock.value,
        )
    status = service.status()
    assert status.budget_exceeded is True
    assert status.total == 1
    assert status.retained_limit == 1


def test_corrupt_journal_fails_closed(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    _enable(service)
    journal_dir = tmp_path / "desktop_notifications" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "segment-000001.jsonl").write_text("not-json\n", encoding="utf-8")
    (journal_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "bounded-operational-journal-v1",
                "journal_id": "desktop-notifications",
                "closed_segments": [],
                "open_segment_name": "segment-000001.jsonl",
            }
        ),
        encoding="utf-8",
    )
    corrupted = DesktopNotificationStore(
        tmp_path / "desktop_notifications",
        workspace_uuid=_WORKSPACE,
    )
    with pytest.raises(DesktopNotificationError):
        corrupted.load_deliveries()


def test_test_action_is_rate_limited_and_never_creates_a_candidate(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    with pytest.raises(DesktopNotificationChannelError):
        service.test()
    _enable(service)
    outcome = service.test()
    assert outcome.accepted is True
    assert adapter.sent[0][0].title == "PRUEBA — sin señal de inversión"
    assert service._store.load_deliveries() == ()  # type: ignore[attr-defined]
    with pytest.raises(DesktopNotificationRateLimitError):
        service.test()


def test_tick_observer_loads_state_only_when_a_batch_is_due(tmp_path) -> None:
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    adapter = _FakeAdapter()
    service = _service(tmp_path, clock, adapter)
    notification = _notification(clock.value + timedelta(seconds=1))
    analytical, notifications, rules = _batch(notification)
    service._rules = lambda: rules  # type: ignore[attr-defined]
    loads: list[str] = []

    def loader():
        loads.append("load")
        return analytical, notifications

    monitor = DesktopNotificationMonitor(service, batch_loader=loader)
    monitor.observe_tick()
    assert loads == []

    _enable(service)
    clock.value = clock.value + timedelta(seconds=2)
    service._store.admit(  # type: ignore[attr-defined]
        notification,
        title=notification.payload.rule_name_es,
        body="Cuerpo",
        at=clock.value,
    )
    monitor.observe_tick()
    assert loads == ["load"]
    assert len(adapter.sent) == 1
    monitor.observe_tick()
    assert loads == ["load"]


def test_two_clocks_produce_distinct_evidence_for_the_same_identity(tmp_path) -> None:
    first_clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    second_clock = _Clock(datetime(2026, 10, 31, 12, tzinfo=UTC))
    notification = _notification(datetime(2026, 10, 30, 11, tzinfo=UTC))
    first = _service(tmp_path / "first", first_clock, _FakeAdapter())
    second = _service(tmp_path / "second", second_clock, _FakeAdapter())
    for service, clock in ((first, first_clock), (second, second_clock)):
        service.configure(enabled=True)
        service._store.admit(  # type: ignore[attr-defined]
            notification,
            title=notification.payload.rule_name_es,
            body="Cuerpo",
            at=clock.value,
        )
    first_delivery = first._store.load_deliveries()[0]  # type: ignore[attr-defined]
    second_delivery = second._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert first_delivery.delivery_id == second_delivery.delivery_id
    assert first_delivery.created_at != second_delivery.created_at


def test_monitor_swallows_channel_failures() -> None:
    class _Broken:
        def observe_batch(self, *_args: object) -> None:
            raise RuntimeError("boom")

    monitor = DesktopNotificationMonitor(_Broken())  # type: ignore[arg-type]
    monitor.observe_batch(SimpleNamespace(), SimpleNamespace())


def test_malicious_payload_is_escaped_in_toast_xml() -> None:
    message = ToastMessage(
        tag="0123456789abcdef",
        group="fedcba9876543210",
        title="<script>alert('x')</script>",
        body="A & B < C",
    )
    xml = message.xml()
    assert "&lt;script&gt;" in xml
    assert "A &amp; B &lt; C" in xml
    assert "<script>" not in xml


def test_fingerprint_is_stable_and_payload_sensitive() -> None:
    notification = _notification(datetime(2026, 10, 30, 11, tzinfo=UTC))
    assert candidate_notification_fingerprint(notification) == (
        candidate_notification_fingerprint(notification)
    )
    changed = notification.model_copy(
        update={"payload": notification.payload.model_copy(update={"rule_name_es": "Otra"})}
    )
    assert candidate_notification_fingerprint(changed) != candidate_notification_fingerprint(
        notification
    )
