"""Recovery flow for the local candidate-notification outbox."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionState,
    AnalyticalScreeningDomain,
)
from investment_analyst.alerts.analytical_state import (
    AnalyticalCandidateEvent,
    analytical_candidate_id,
)
from investment_analyst.alerts.candidate_notifications import (
    CandidateNotificationMonitor,
    CandidateNotificationStore,
)
from investment_analyst.application.operational_state import AaplOperationalStateError


class _FailOnceStore(CandidateNotificationStore):
    def __init__(self, path):
        super().__init__(path)
        self.failed = False

    def enqueue(self, item):
        if not self.failed:
            self.failed = True
            raise AaplOperationalStateError("temporary outbox write failure")
        return super().enqueue(item)


def test_reconcile_recovers_after_outbox_write_failure_without_changing_candidate(tmp_path) -> None:
    result_id = uuid4()
    candidate = AnalyticalCandidateEvent(
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
    )
    state = SimpleNamespace(
        candidates=(candidate,),
        results=(
            SimpleNamespace(
                result_id=result_id,
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
    analytical_store = SimpleNamespace(load=lambda: state)
    path = tmp_path / "candidate_notification_outbox_state_v1.json"

    with pytest.raises(AaplOperationalStateError, match="temporary outbox"):
        CandidateNotificationMonitor(_FailOnceStore(path), analytical_store).reconcile()

    CandidateNotificationMonitor(CandidateNotificationStore(path), analytical_store).reconcile()

    recovered = CandidateNotificationStore(path).load()
    assert recovered.items[0].candidate_id == candidate.candidate_id
    assert len(recovered.items) == 1
    assert recovered.transitions == ()
    assert candidate.status.value == "new"
