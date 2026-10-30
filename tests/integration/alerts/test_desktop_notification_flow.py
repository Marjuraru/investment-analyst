"""End-to-end local flow from persisted candidate to bounded toast delivery."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from investment_analyst.alerts.analytical_engine import AnalyticalScreeningEngine
from investment_analyst.alerts.analytical_models import (
    AnalyticalRuleState,
    AnalyticalScreeningRequest,
)
from investment_analyst.alerts.analytical_rule_catalog import (
    INITIAL_MARKET_ACTIVITY_RULE,
)
from investment_analyst.alerts.analytical_rule_registry import (
    AnalyticalRuleConfiguration,
    AnalyticalRuleRegistrySnapshot,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateEvent,
    AnalyticalScreeningState,
    analytical_candidate_id,
)
from investment_analyst.alerts.candidate_notifications import (
    CandidateNotificationMonitor,
    CandidateNotificationStore,
)
from investment_analyst.alerts.desktop_notification_models import (
    DesktopNotificationDeliveryStatus,
    DesktopNotificationReason,
)
from investment_analyst.alerts.desktop_notifications import (
    DesktopNotificationMonitor,
    DesktopNotificationService,
    DesktopNotificationStore,
)
from investment_analyst.alerts.windows_notifications import (
    ToastMessage,
    ToastSendOutcome,
    ToastSendStatus,
    WindowsCapability,
)
from investment_analyst.core.models import AssetClass, DataQuality, MetricResult

_WORKSPACE = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_MARKET_SOURCE = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_T0 = datetime(2026, 10, 30, 12, tzinfo=UTC)


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class _Adapter:
    def __init__(self, status: ToastSendStatus = ToastSendStatus.ACCEPTED) -> None:
        self.status = status
        self.sent: list[tuple[ToastMessage, ...]] = []
        self.capability = WindowsCapability(
            available=True,
            reason=None,
            powershell_version="5.1.26100.9711",
            aumid_registered=True,
        )

    def register(self) -> WindowsCapability:
        return self.capability

    def probe(self) -> WindowsCapability:
        return self.capability

    def send(self, messages: tuple[ToastMessage, ...]) -> dict[str, ToastSendOutcome]:
        self.sent.append(tuple(messages))
        reason = (
            None
            if self.status is ToastSendStatus.ACCEPTED
            else DesktopNotificationReason.SEND_ERROR
        )
        return {
            message.tag: ToastSendOutcome(message.tag, self.status, reason) for message in messages
        }


class _AnalyticalStore:
    def __init__(self, state: AnalyticalScreeningState) -> None:
        self._state = state

    def load(self) -> AnalyticalScreeningState:
        return self._state


def _state(known_at: datetime) -> AnalyticalScreeningState:
    rule = INITIAL_MARKET_ACTIVITY_RULE.model_copy(update={"state": AnalyticalRuleState.ACTIVE})
    metric = MetricResult(
        result_id=UUID("00000000-0000-4000-8000-000000000001"),
        asset_id="equity:us:aapl",
        metric_key="market.history.relative_volume",
        value=Decimal("1.75"),
        unit="ratio",
        as_of=known_at - timedelta(days=1),
        available_at=known_at - timedelta(hours=1),
        computed_at=known_at,
        parameters={"source_id": _MARKET_SOURCE, "window": 20},
        input_observation_ids=[UUID("10000000-0000-4000-8000-000000000001")],
        algorithm_version="market-relative-volume-v1-decimal34",
        quality=DataQuality.PARTIAL,
    )
    result = AnalyticalScreeningEngine().evaluate(
        AnalyticalScreeningRequest(
            rule=rule,
            asset_id="equity:us:aapl",
            asset_class=AssetClass.EQUITY,
            source_id=_MARKET_SOURCE,
            known_at=known_at,
            computed_at=known_at,
            metrics=(metric,),
        )
    )
    candidate = AnalyticalCandidateEvent(
        candidate_id=analytical_candidate_id(result.result_id),
        activation_result_id=result.result_id,
        rule_id=result.rule.rule_id,
        rule_version=result.rule.rule_version,
        rule_fingerprint=result.rule.semantic_fingerprint(),
        asset_id=result.asset_id,
        domain=result.rule.domain,
        source_id=result.source_id,
        as_of=result.as_of,
        activated_at=known_at + timedelta(seconds=1),
        cooldown_until=known_at + timedelta(days=1),
        confirmations=1,
    )
    return AnalyticalScreeningState(results=(result,), candidates=(candidate,))


def _registry(state: AnalyticalScreeningState) -> AnalyticalRuleRegistrySnapshot:
    result = state.results[0]
    configuration = AnalyticalRuleConfiguration(
        rule=result.rule,
        default_rule=INITIAL_MARKET_ACTIVITY_RULE,
        fingerprint=result.rule.semantic_fingerprint(),
        customized=True,
        revision_count=1,
    )
    return AnalyticalRuleRegistrySnapshot(configurations=(configuration,), total_revisions=1)


def _service(tmp_path, clock: _Clock, adapter: _Adapter, registry) -> DesktopNotificationService:
    return DesktopNotificationService(
        DesktopNotificationStore(
            tmp_path / "state" / "desktop_notifications",
            workspace_uuid=_WORKSPACE,
        ),
        adapter,
        rules=lambda: registry,
        clock=clock,
    )


def _monitor(service: DesktopNotificationService, store: CandidateNotificationStore, state):
    return CandidateNotificationMonitor(
        store,
        _AnalyticalStore(state),
        batch_observer=DesktopNotificationMonitor(service).observe_batch,
    )


def test_reconcile_delivers_once_and_restart_never_replays(tmp_path) -> None:
    clock = _Clock(_T0)
    adapter = _Adapter()
    state = _state(_T0 + timedelta(seconds=5))
    registry = _registry(state)
    service = _service(tmp_path, clock, adapter, registry)
    service.configure(enabled=True)
    outbox = CandidateNotificationStore(tmp_path / "state" / "outbox.json")

    clock.value = _T0 + timedelta(seconds=10)
    _monitor(service, outbox, state).reconcile()
    assert len(adapter.sent) == 1
    assert len(adapter.sent[0]) == 1
    assert adapter.sent[0][0].title == state.results[0].rule.name_es
    deliveries = service._store.load_deliveries()  # type: ignore[attr-defined]
    assert len(deliveries) == 1
    assert deliveries[0].status is DesktopNotificationDeliveryStatus.ACCEPTED
    assert deliveries[0].notification_id == outbox.load().items[0].notification_id

    clock.value = _T0 + timedelta(minutes=5)
    _monitor(service, outbox, state).reconcile()
    assert len(adapter.sent) == 1

    restarted = _service(tmp_path, clock, adapter, registry)
    assert restarted.recover() == 0
    clock.value = _T0 + timedelta(minutes=10)
    _monitor(restarted, outbox, state).reconcile()
    assert len(adapter.sent) == 1
    assert (
        restarted._store.load_deliveries()[0].delivery_id  # type: ignore[attr-defined]
        == deliveries[0].delivery_id
    )


def test_disable_cancels_pending_and_reenable_keeps_the_old_boundary(tmp_path) -> None:
    clock = _Clock(_T0)
    adapter = _Adapter(status=ToastSendStatus.RETRYABLE)
    state = _state(_T0 + timedelta(seconds=5))
    registry = _registry(state)
    service = _service(tmp_path, clock, adapter, registry)
    service.configure(enabled=True)
    outbox = CandidateNotificationStore(tmp_path / "state" / "outbox.json")

    clock.value = _T0 + timedelta(seconds=10)
    _monitor(service, outbox, state).reconcile()
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.RETRY_WAIT
    assert len(adapter.sent) == 1

    clock.value = _T0 + timedelta(seconds=20)
    service.configure(enabled=False)
    delivery = service._store.load_deliveries()[0]  # type: ignore[attr-defined]
    assert delivery.status is DesktopNotificationDeliveryStatus.CANCELLED
    assert delivery.reason is DesktopNotificationReason.CHANNEL_DISABLED

    clock.value = _T0 + timedelta(seconds=30)
    service.configure(enabled=True)
    clock.value = _T0 + timedelta(minutes=10)
    _monitor(service, outbox, state).reconcile()
    assert len(adapter.sent) == 1
    assert len(service._store.load_deliveries()) == 1  # type: ignore[attr-defined]


def test_disabled_channel_leaves_no_channel_artifacts(tmp_path) -> None:
    clock = _Clock(_T0)
    adapter = _Adapter()
    state = _state(_T0 + timedelta(seconds=5))
    service = _service(tmp_path, clock, adapter, _registry(state))
    outbox = CandidateNotificationStore(tmp_path / "state" / "outbox.json")
    clock.value = _T0 + timedelta(seconds=10)
    _monitor(service, outbox, state).reconcile()
    assert len(outbox.load().items) == 1
    assert adapter.sent == []
    channel_dir = tmp_path / "state" / "desktop_notifications"
    assert not channel_dir.exists() or not any(channel_dir.rglob("*"))


def test_status_read_does_not_probe_or_write(tmp_path) -> None:
    clock = _Clock(_T0)
    adapter = _Adapter()
    state = _state(_T0 + timedelta(seconds=5))
    service = _service(tmp_path, clock, adapter, _registry(state))
    service.configure(enabled=True)
    before = service._store.state_bytes()  # type: ignore[attr-defined]
    status = service.status()
    after = service._store.state_bytes()  # type: ignore[attr-defined]
    assert before == after
    assert status.enabled is True
    assert status.capability.available is True
    assert adapter.sent == []
