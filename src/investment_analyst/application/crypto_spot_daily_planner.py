"""Read-only edge planner scoped to one configured Coinbase daily series."""
# ruff: noqa: E501

from datetime import UTC, date, datetime, timedelta

from investment_analyst.application.btc_refresh_models import (
    BtcMarketDateInterval,
    BtcMarketRefreshMode,
    BtcMarketRefreshPlan,
    BtcRefreshMode,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.storage import LocalStorage


class CryptoSpotDailyRefreshPlanner:
    """Plan only missing range edges without inferring gaps inside a daily series."""

    def __init__(self, storage: LocalStorage, *, asset_id: str, source_id: str) -> None:
        storage.require_open()
        self._storage = storage
        self._asset_id = asset_id
        self._source_id = source_id

    @property
    def storage_handle(self) -> LocalStorage:
        """Expose the injected storage for the shared daily-cut projection."""
        return self._storage

    def plan(
        self, *, requested_start: date, requested_end: date, refresh_mode: BtcRefreshMode
    ) -> BtcMarketRefreshPlan:
        earliest, latest, available = self._persisted_edges()
        full = BtcMarketDateInterval(start=requested_start, end=requested_end)
        if refresh_mode is BtcRefreshMode.FULL:
            return self._result(
                requested_start,
                requested_end,
                earliest,
                latest,
                available,
                (full,),
                BtcMarketRefreshMode.FULL,
                "Full Coinbase refresh explicitly requested; persisted deterministic identities remain reusable.",
            )
        if earliest is None or latest is None:
            return self._result(
                requested_start,
                requested_end,
                None,
                None,
                None,
                (full,),
                BtcMarketRefreshMode.INITIAL,
                "No persisted Coinbase daily candles were found for this configured asset.",
            )
        intervals: list[BtcMarketDateInterval] = []
        if requested_start < earliest.date():
            intervals.append(
                BtcMarketDateInterval(
                    start=requested_start,
                    end=min(requested_end, earliest.date() - timedelta(days=1)),
                )
            )
        if requested_end > latest.date():
            start = max(requested_start, latest.date() + timedelta(days=1))
            if start <= requested_end:
                intervals.append(BtcMarketDateInterval(start=start, end=requested_end))
        if not intervals:
            return self._result(
                requested_start,
                requested_end,
                earliest,
                latest,
                available,
                (),
                BtcMarketRefreshMode.ALREADY_CURRENT,
                "The requested range is inside persisted Coinbase daily-candle edges.",
            )
        if intervals[0].start == requested_start and requested_start < earliest.date():
            mode = BtcMarketRefreshMode.BACKFILL
            reason = "Persisted Coinbase coverage requires an earlier prefix" + (
                " and a later suffix." if len(intervals) == 2 else "."
            )
        else:
            mode, reason = (
                BtcMarketRefreshMode.INCREMENTAL,
                "Persisted Coinbase coverage requires only a later suffix.",
            )
        return self._result(
            requested_start,
            requested_end,
            earliest,
            latest,
            available,
            tuple(intervals),
            mode,
            reason,
        )

    def _persisted_edges(self) -> tuple[datetime | None, datetime | None, datetime | None]:
        """Project persisted series edges with bounded SQL aggregates, never hydrating history."""
        earliest, latest = self._storage.observations.observed_at_bounds(
            asset_id=self._asset_id,
            source_id=self._source_id,
            frequency=DataFrequency.DAY_1,
        )
        if earliest is None or latest is None:
            return None, None, None
        available = self._storage.observations.maximum_available_at(
            asset_id=self._asset_id,
            source_id=self._source_id,
            frequency=DataFrequency.DAY_1,
        )
        return (
            earliest.astimezone(UTC),
            latest.astimezone(UTC),
            available.astimezone(UTC) if available is not None else None,
        )

    @staticmethod
    def _result(
        start: date,
        end: date,
        earliest: datetime | None,
        latest: datetime | None,
        available: datetime | None,
        intervals: tuple[BtcMarketDateInterval, ...],
        mode: BtcMarketRefreshMode,
        reason: str,
    ) -> BtcMarketRefreshPlan:
        return BtcMarketRefreshPlan(
            requested_start=start,
            requested_end=end,
            persisted_earliest=earliest,
            persisted_latest=latest,
            persisted_latest_available_at=available,
            fetch_intervals=intervals,
            mode=mode,
            market_fetch_required=bool(intervals),
            reason=reason,
            traceability_verified=True,
        )
