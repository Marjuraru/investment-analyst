"""Tests for deterministic composition of independent scheduler observers."""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from investment_analyst.application.multi_asset_scheduler import (
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobFailureCategory,
    scheduled_job_failure,
)
from investment_analyst.application.scheduled_observers import (
    ScheduledJobObserverChain,
    ScheduledObserverDeliveryError,
)


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
        failure=scheduled_job_failure(ScheduledJobFailureCategory.UNEXPECTED, "safe failure"),
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


def test_observer_chain_attempts_all_and_retries_only_pending_observers() -> None:
    definition = ScheduledJobDefinition(
        job_id="test:observer-retry",
        provider="test",
        domain=ScheduledJobDomain.CATALOG,
        data_frequency="daily",
    )
    attempt = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000002"),
        definition=definition,
        local_date=date(2026, 7, 29),
        scheduled_for=definition.scheduled_for(date(2026, 7, 29)),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.FAILED,
        started_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        completed_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        failure=scheduled_job_failure(ScheduledJobFailureCategory.UNEXPECTED, "safe failure"),
    )
    calls: list[str] = []
    failures_remaining = {"first": 1, "middle": 1}

    def observer(name: str):
        def deliver(_: ScheduledJobAttempt) -> None:
            calls.append(name)
            if failures_remaining.get(name, 0):
                failures_remaining[name] -= 1
                raise OSError("simulated-secret")

        return deliver

    chain = ScheduledJobObserverChain((observer("first"), observer("middle"), observer("last")))

    with pytest.raises(ScheduledObserverDeliveryError) as raised:
        chain(attempt)

    assert raised.value.failed_count == 2
    assert "simulated-secret" not in str(raised.value)
    assert calls == ["first", "middle", "last"]

    chain(attempt)

    assert calls == ["first", "middle", "last", "first", "middle"]
    assert chain._pending == {}
