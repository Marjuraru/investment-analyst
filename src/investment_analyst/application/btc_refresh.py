"""Shared execution clock for Coinbase incremental market refreshes."""

from collections.abc import Callable
from datetime import UTC, datetime


class BtcMarketRefreshError(RuntimeError):
    """Raised when one shared Coinbase refresh clock cannot advance safely."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BtcMarketExecutionClock:
    """Keep one Coinbase daily execution clock in UTC without allowing regressions."""

    def __init__(self, source: Callable[[], datetime] = _utc_now) -> None:
        self._source = source
        self._latest: datetime | None = None

    def __call__(self) -> datetime:
        """Return the latest wall-clock value observed during this execution."""
        return self.observe(self._source())

    def observe(self, value: datetime) -> datetime:
        """Record one UTC instant as a chronological floor for this execution."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise BtcMarketRefreshError("clock must return a timezone-aware datetime")
        normalized = value.astimezone(UTC)
        if self._latest is None or normalized > self._latest:
            self._latest = normalized
        return self._latest
