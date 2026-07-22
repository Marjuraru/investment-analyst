"""Read-only edge planning for incremental Coinbase BTC-USD refreshes."""

from datetime import UTC, date, datetime, timedelta

from investment_analyst.application.btc_refresh_models import (
    BtcMarketDateInterval,
    BtcMarketRefreshMode,
    BtcMarketRefreshPlan,
    BtcRefreshMode,
)
from investment_analyst.core.models import DataFrequency, NormalizedObservation
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.storage import LocalStorage


class BtcMarketRefreshPlanner:
    """Plan only missing prefix and suffix edges from persisted daily candles."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def plan(
        self,
        *,
        requested_start: date,
        requested_end: date,
        refresh_mode: BtcRefreshMode,
    ) -> BtcMarketRefreshPlan:
        """Return one deterministic plan without fetching or modifying storage."""
        observations = self._persisted_observations()
        persisted = tuple(sorted({item.observed_at.astimezone(UTC) for item in observations}))
        earliest = min(persisted) if persisted else None
        latest = max(persisted) if persisted else None
        latest_available_at = (
            max(item.available_at.astimezone(UTC) for item in observations)
            if observations
            else None
        )
        full = BtcMarketDateInterval(start=requested_start, end=requested_end)

        if refresh_mode is BtcRefreshMode.FULL:
            return self._plan(
                requested_start=requested_start,
                requested_end=requested_end,
                persisted_earliest=earliest,
                persisted_latest=latest,
                persisted_latest_available_at=latest_available_at,
                fetch_intervals=(full,),
                mode=BtcMarketRefreshMode.FULL,
                reason=(
                    "Full Coinbase refresh explicitly requested; persisted deterministic "
                    "identities remain reusable."
                ),
            )

        if not persisted:
            return self._plan(
                requested_start=requested_start,
                requested_end=requested_end,
                persisted_earliest=None,
                persisted_latest=None,
                persisted_latest_available_at=None,
                fetch_intervals=(full,),
                mode=BtcMarketRefreshMode.INITIAL,
                reason="No persisted Coinbase BTC-USD daily candles were found.",
            )

        coverage_start = earliest.date()
        coverage_end = latest.date()
        intervals: list[BtcMarketDateInterval] = []
        if requested_start < coverage_start:
            intervals.append(
                BtcMarketDateInterval(
                    start=requested_start,
                    end=min(requested_end, coverage_start - timedelta(days=1)),
                )
            )
        if requested_end > coverage_end:
            suffix_start = max(requested_start, coverage_end + timedelta(days=1))
            if suffix_start <= requested_end:
                intervals.append(BtcMarketDateInterval(start=suffix_start, end=requested_end))

        if not intervals:
            return self._plan(
                requested_start=requested_start,
                requested_end=requested_end,
                persisted_earliest=earliest,
                persisted_latest=latest,
                persisted_latest_available_at=latest_available_at,
                fetch_intervals=(),
                mode=BtcMarketRefreshMode.ALREADY_CURRENT,
                reason=(
                    "The requested range is inside persisted Coinbase BTC-USD daily-candle edges."
                ),
            )

        if intervals[0].start == requested_start and requested_start < coverage_start:
            mode = BtcMarketRefreshMode.BACKFILL
            reason = "Persisted Coinbase coverage requires an earlier prefix" + (
                " and a later suffix." if len(intervals) == 2 else "."
            )
        else:
            mode = BtcMarketRefreshMode.INCREMENTAL
            reason = "Persisted Coinbase coverage requires only a later suffix."
        return self._plan(
            requested_start=requested_start,
            requested_end=requested_end,
            persisted_earliest=earliest,
            persisted_latest=latest,
            persisted_latest_available_at=latest_available_at,
            fetch_intervals=tuple(intervals),
            mode=mode,
            reason=reason,
        )

    def _persisted_observations(self) -> tuple[NormalizedObservation, ...]:
        observations = self._storage.observations.list(asset_id=ASSET_ID)
        return tuple(
            observation
            for observation in observations
            if observation.asset_id == ASSET_ID
            and observation.source.source_id == SOURCE_ID
            and observation.frequency is DataFrequency.DAY_1
            and observation.observed_at is not None
            and observation.observed_at.tzinfo is not None
            and observation.observed_at.utcoffset() is not None
            and observation.available_at.tzinfo is not None
            and observation.available_at.utcoffset() is not None
        )

    @staticmethod
    def _plan(
        *,
        requested_start: date,
        requested_end: date,
        persisted_earliest: datetime | None,
        persisted_latest: datetime | None,
        persisted_latest_available_at: datetime | None,
        fetch_intervals: tuple[BtcMarketDateInterval, ...],
        mode: BtcMarketRefreshMode,
        reason: str,
    ) -> BtcMarketRefreshPlan:
        return BtcMarketRefreshPlan(
            requested_start=requested_start,
            requested_end=requested_end,
            persisted_earliest=persisted_earliest,
            persisted_latest=persisted_latest,
            persisted_latest_available_at=persisted_latest_available_at,
            fetch_intervals=fetch_intervals,
            mode=mode,
            market_fetch_required=bool(fetch_intervals),
            reason=reason,
            traceability_verified=True,
        )
