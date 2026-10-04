"""Composable scheduler observers with deterministic execution order."""

from collections.abc import Callable
from uuid import UUID

from investment_analyst.application.multi_asset_scheduler import ScheduledJobAttempt


class ScheduledObserverDeliveryError(RuntimeError):
    """Safe aggregate raised after every independent observer was attempted."""

    def __init__(self, failed_count: int) -> None:
        self.failed_count = failed_count
        super().__init__("scheduled job observer delivery failed")


class ScheduledJobObserverChain:
    """Run independent observers in declared order for one durable attempt."""

    def __init__(
        self,
        observers: tuple[Callable[[ScheduledJobAttempt], None], ...],
    ) -> None:
        if not observers:
            raise ValueError("scheduled observer chain must not be empty")
        self._observers = observers
        self._pending: dict[UUID, set[int]] = {}

    def __call__(self, attempt: ScheduledJobAttempt) -> None:
        """Deliver the same attempt to every observer."""
        attempt_id = attempt.attempt_id
        pending = self._pending.get(attempt_id)
        failed: set[int] = set()
        for index, observer in enumerate(self._observers):
            if pending is not None and index not in pending:
                continue
            try:
                observer(attempt)
            except Exception:  # noqa: BLE001 - preserve isolation between observers
                failed.add(index)
        if failed:
            self._pending[attempt_id] = failed
            raise ScheduledObserverDeliveryError(len(failed))
        self._pending.pop(attempt_id, None)


__all__ = ["ScheduledJobObserverChain", "ScheduledObserverDeliveryError"]
