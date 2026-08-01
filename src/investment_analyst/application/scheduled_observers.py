"""Composable scheduler observers with deterministic execution order."""

from collections.abc import Callable

from investment_analyst.application.multi_asset_scheduler import ScheduledJobAttempt


class ScheduledJobObserverChain:
    """Run independent observers in declared order for one durable attempt."""

    def __init__(
        self,
        observers: tuple[Callable[[ScheduledJobAttempt], None], ...],
    ) -> None:
        if not observers:
            raise ValueError("scheduled observer chain must not be empty")
        self._observers = observers

    def __call__(self, attempt: ScheduledJobAttempt) -> None:
        """Deliver the same attempt to every observer."""
        for observer in self._observers:
            observer(attempt)


__all__ = ["ScheduledJobObserverChain"]
