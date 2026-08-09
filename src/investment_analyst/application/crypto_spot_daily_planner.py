"""Read-only edge planner scoped to one configured Coinbase daily series."""
# ruff: noqa: E501

from datetime import UTC, date, datetime, timedelta

from investment_analyst.application.btc_refresh_models import (
    BtcMarketDateInterval,
    BtcMarketRefreshMode,
    BtcMarketRefreshPlan,
    BtcRefreshMode,
)
from investment_analyst.core.models import DataFrequency, NormalizedObservation
from investment_analyst.storage import LocalStorage


class CryptoSpotDailyRefreshPlanner:
    """Plan only missing range edges without inferring gaps inside a daily series."""

    def __init__(self, storage: LocalStorage, *, asset_id: str, source_id: str) -> None:
        storage.require_open()
        self._storage = storage
        self._asset_id = asset_id
        self._source_id = source_id

    def plan(
        self, *, requested_start: date, requested_end: date, refresh_mode: BtcRefreshMode
    ) -> BtcMarketRefreshPlan:
        observations = self._persisted_observations()
        persisted = tuple(sorted({item.observed_at.astimezone(UTC) for item in observations}))
        earliest = min(persisted) if persisted else None
        latest = max(persisted) if persisted else None
        available = max((item.available_at.astimezone(UTC) for item in observations), default=None)
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
        if not persisted:
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

    def _persisted_observations(self) -> tuple[NormalizedObservation, ...]:
        return tuple(
            item
            for item in self._storage.observations.list(asset_id=self._asset_id)
            if item.asset_id == self._asset_id
            and item.source.source_id == self._source_id
            and item.frequency is DataFrequency.DAY_1
            and item.observed_at is not None
            and item.observed_at.tzinfo is not None
            and item.observed_at.utcoffset() is not None
            and item.available_at.tzinfo is not None
            and item.available_at.utcoffset() is not None
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
