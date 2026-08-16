"""Tests for deterministic local notification outbox state."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionState,
    AnalyticalScreeningDomain,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateEvent,
    AnalyticalCandidateStatus,
    analytical_candidate_id,
)
from investment_analyst.alerts.candidate_notifications import (
    CandidateNotification,
    CandidateNotificationMonitor,
    CandidateNotificationPayload,
    CandidateNotificationStore,
    notification_id,
)


def _payload() -> CandidateNotificationPayload:
    return CandidateNotificationPayload(
        rule_name_es="Regla de prueba",
        explanation_es="Evidencia compacta de prueba.",
        conditions=(
            {
                "condition_id": "market.test",
                "state": "met",
                "metric_key": "market.test",
                "unit": "ratio",
                "explanation_es": "Condición satisfecha.",
            },
        ),
    )


def test_enqueue_and_acknowledge_are_idempotent(tmp_path) -> None:
    candidate = uuid4()
    item = CandidateNotification(
        notification_id=notification_id(candidate),
        candidate_id=candidate,
        activation_result_id=uuid4(),
        asset_id="crypto:btc-usd",
        rule_id="market.test",
        as_of=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        payload=_payload(),
    )
    store = CandidateNotificationStore(tmp_path / "state.json")
    assert store.enqueue(item)[1] is True
    assert store.enqueue(item)[1] is False
    assert store.acknowledge(item.notification_id, recorded_at=datetime(2026, 1, 3, tzinfo=UTC))[1]
    assert not store.acknowledge(
        item.notification_id, recorded_at=datetime(2026, 1, 4, tzinfo=UTC)
    )[1]


def test_reconcile_enqueues_only_new_candidates_once(tmp_path) -> None:
    def candidate(result_id, status=AnalyticalCandidateStatus.NEW):
        return AnalyticalCandidateEvent(
            candidate_id=analytical_candidate_id(result_id),
            activation_result_id=result_id,
            rule_id="market.test",
            rule_version="v1",
            rule_fingerprint="f" * 64,
            asset_id="crypto:btc-usd",
            domain=AnalyticalScreeningDomain.MARKET,
            source_id="test-source",
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
            activated_at=datetime(2026, 1, 2, tzinfo=UTC),
            cooldown_until=datetime(2026, 1, 3, tzinfo=UTC),
            confirmations=1,
            status=status,
        )

    new_candidate = candidate(uuid4())
    nonnew = candidate(uuid4(), AnalyticalCandidateStatus.SEEN)
    store = CandidateNotificationStore(tmp_path / "notifications.json")
    monitor = CandidateNotificationMonitor(
        store,
        SimpleNamespace(
            load=lambda: SimpleNamespace(
                candidates=(new_candidate, nonnew),
                results=(
                    SimpleNamespace(
                        result_id=new_candidate.activation_result_id,
                        rule=SimpleNamespace(name_es="Regla de prueba"),
                        explanation_es="Evidencia compacta de prueba.",
                        conditions=(
                            SimpleNamespace(
                                condition_id="market.test",
                                state=AnalyticalConditionState.MET,
                                metric_key="market.test",
                                unit="ratio",
                                metric_result_id=None,
                                as_of=None,
                                explanation_es="Condición satisfecha.",
                            ),
                        ),
                    ),
                ),
            )
        ),
    )

    monitor.reconcile()
    monitor.reconcile()

    state = store.load()
    assert len(state.items) == 1
    assert state.items[0].candidate_id == new_candidate.candidate_id
    assert state.items[0].notification_id == notification_id(new_candidate.candidate_id)
