"""Regression tests for durable queued manual operations."""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.application.manual_operations import (
    ManualOperationKind,
    ManualOperationQueue,
    ManualOperationRequest,
    ManualOperationResult,
    ManualOperationState,
    ManualOperationStateStore,
    ManualOperationStatus,
)


def _request(*, market_end: str = "2026-07-02") -> ManualOperationRequest:
    return ManualOperationRequest(
        operation_kind=ManualOperationKind.MARKET_DAILY,
        payload={
            "asset_id": "crypto:btc-usd",
            "market_start": "2026-07-01",
            "market_end": market_end,
            "refresh_mode": "auto",
            "requested_known_at": "2026-07-03T00:00:00Z",
        },
    )


def _result() -> ManualOperationResult:
    return ManualOperationResult(
        result_schema_version="btc-market-refresh-v1",
        effective_known_at=datetime(2026, 7, 3, tzinfo=UTC),
        created_count=2,
        reused_count=0,
    )


def test_equivalent_active_requests_are_deduplicated_and_then_can_repeat(tmp_path: Path) -> None:
    calls: list[ManualOperationRequest] = []
    moments = iter(
        datetime(2026, 7, 3, hour=1, tzinfo=UTC) + timedelta(seconds=index) for index in range(8)
    )
    ids = iter(
        (
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
        )
    )

    def dispatch(request: ManualOperationRequest) -> ManualOperationResult:
        calls.append(request)
        return _result()

    queue = ManualOperationQueue(
        ManualOperationStateStore(tmp_path / "state.json"),
        dispatch,
        clock=lambda: next(moments),
        operation_id_factory=lambda: next(ids),
    )

    first = queue.enqueue(_request())
    duplicate = queue.enqueue(_request())
    completed = queue.run_next()
    repeated = queue.enqueue(_request())
    second = queue.run_next()

    assert duplicate.operation_id == first.operation_id
    assert completed is not None and completed.status is ManualOperationStatus.SUCCEEDED
    assert repeated.operation_id != first.operation_id
    assert second is not None and second.status is ManualOperationStatus.SUCCEEDED
    assert calls == [_request(), _request()]


def test_running_operation_is_requeued_and_recovered_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = ManualOperationStateStore(path)
    request = _request()
    submitted = datetime(2026, 7, 3, hour=1, tzinfo=UTC)
    running = ManualOperationState(
        operation_id=UUID("00000000-0000-0000-0000-000000000003"),
        fingerprint=request.fingerprint,
        request=request,
        status=ManualOperationStatus.RUNNING,
        submitted_at=submitted,
        started_at=submitted + timedelta(seconds=1),
    )
    store.write(running)

    queue = ManualOperationQueue(store, lambda request: _result())
    recovered = store.load().operations[0]
    completed = queue.run_next()

    assert recovered.status is ManualOperationStatus.QUEUED
    assert recovered.recovery_count == 1
    assert completed is not None and completed.operation_id == running.operation_id
    assert completed.status is ManualOperationStatus.SUCCEEDED


def test_queue_uses_one_writer_mutex_while_snapshot_remains_available(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def dispatch(request: ManualOperationRequest) -> ManualOperationResult:
        del request
        entered.set()
        assert release.wait(timeout=2)
        return _result()

    queue = ManualOperationQueue(ManualOperationStateStore(tmp_path / "state.json"), dispatch)
    queued = queue.enqueue(_request())
    worker = threading.Thread(target=queue.run_next)
    worker.start()
    assert entered.wait(timeout=2)

    snapshot = queue.snapshot()
    status = queue.get(queued.operation_id)
    release.set()
    worker.join(timeout=2)

    assert snapshot.running_count == 1
    assert status is not None and status.status is ManualOperationStatus.RUNNING
    assert not worker.is_alive()


@pytest.mark.parametrize(
    ("operation_kind", "payload"),
    [
        (
            ManualOperationKind.COMPLETE_REFRESH,
            {
                "asset_id": "equity:us:aapl",
                "market_start": "2026-07-01",
                "market_end": "2026-07-02",
                "unexpected": True,
            },
        ),
        (
            ManualOperationKind.MARKET_DAILY,
            {"asset_id": "crypto:btc-usd", "hours": 24},
        ),
        (
            ManualOperationKind.MARKET_INTRADAY,
            {
                "asset_id": "crypto:btc-usd",
                "hours": 12,
                "requested_end": "2026-07-03T00:00:00Z",
            },
        ),
        (
            ManualOperationKind.FUNDAMENTALS,
            {"asset_id": "equity:us:amd", "frequency": "monthly"},
        ),
    ],
)
def test_payload_is_strictly_validated_for_operation_before_persistence(
    tmp_path: Path,
    operation_kind: ManualOperationKind,
    payload: dict[str, object],
) -> None:
    state_path = tmp_path / "manual-operations.json"
    ManualOperationQueue(ManualOperationStateStore(state_path), lambda request: _result())

    with pytest.raises(ValidationError):
        ManualOperationRequest(operation_kind=operation_kind, payload=payload)

    assert not state_path.exists()


def test_worker_survives_decreasing_clock_and_completes_later_work(tmp_path: Path) -> None:
    origin = datetime(2026, 7, 3, hour=1, tzinfo=UTC)
    moments = iter(origin - timedelta(seconds=index) for index in range(6))
    calls: list[ManualOperationRequest] = []
    queue = ManualOperationQueue(
        ManualOperationStateStore(tmp_path / "state.json"),
        lambda request: calls.append(request) or _result(),
        clock=lambda: next(moments),
        operation_id_factory=iter(
            (
                UUID("00000000-0000-0000-0000-000000000010"),
                UUID("00000000-0000-0000-0000-000000000011"),
            )
        ).__next__,
    )
    first = queue.enqueue(_request())
    second = queue.enqueue(_request(market_end="2026-07-03"))

    queue.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(
                (state := queue.get(operation_id)) is not None
                and state.status is ManualOperationStatus.SUCCEEDED
                for operation_id in (first.operation_id, second.operation_id)
            ):
                break
            time.sleep(0.01)
    finally:
        queue.stop()

    completed = tuple(queue.get(item.operation_id) for item in (first, second))
    assert all(
        item is not None and item.status is ManualOperationStatus.SUCCEEDED for item in completed
    )
    assert len(calls) == 2
    for item in completed:
        assert item is not None
        assert item.started_at is not None and item.started_at >= item.submitted_at
        assert item.completed_at is not None and item.completed_at >= item.started_at


@pytest.mark.parametrize("key", ["api_key", "secret_key", "Authorization", "access_token"])
def test_manual_state_rejects_secret_bearing_payload_keys(key: str) -> None:
    with pytest.raises(ValidationError, match="must not contain credentials"):
        ManualOperationRequest(
            operation_kind=ManualOperationKind.MARKET_DAILY,
            payload={key: "must-never-be-persisted"},
        )
