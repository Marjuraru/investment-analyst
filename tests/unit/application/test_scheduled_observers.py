"""Tests for deterministic composition of independent scheduler observers."""

from datetime import UTC, date, datetime
from uuid import UUID

from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobFailure,
)
from investment_analyst.application.scheduled_observers import ScheduledJobObserverChain


def test_observer_chain_delivers_same_attempt_in_declared_order() -> None:
    definition = ScheduledJobDefinition(
        job_id="test:catalog",
        provider="test",
        domain=ScheduledJobDomain.CATALOG,
        data_frequency="daily",
    )
    attempt = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000001"),
        definition=definition,
        local_date=date(2026, 7, 29),
        scheduled_for=definition.scheduled_for(date(2026, 7, 29)),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.FAILED,
        started_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        completed_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        failure=ScheduledJobFailure(
            category="test",
            message="safe failure",
            retryable=False,
        ),
    )
    calls: list[tuple[str, UUID]] = []

    ScheduledJobObserverChain(
        (
            lambda observed: calls.append(("first", observed.attempt_id)),
            lambda observed: calls.append(("second", observed.attempt_id)),
        )
    )(attempt)

    assert calls == [
        ("first", attempt.attempt_id),
        ("second", attempt.attempt_id),
    ]
