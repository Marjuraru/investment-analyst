"""Read-only planning for incremental catalog-backed Alpaca IEX refreshes."""

from datetime import UTC, date, datetime, time, timedelta

from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketDateInterval,
    AaplMarketRefreshMode,
    AaplMarketRefreshPlan,
    AaplRefreshMode,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID
from investment_analyst.providers.market.alpaca_normalizer import SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import (
    ALPACA_FETCH_RECEIPT_SCHEMA,
    ALPACA_INTERVAL_SEMANTICS,
    alpaca_fetch_receipt_from_raw_record,
    alpaca_receipt_covers_calendar_days,
)
from investment_analyst.providers.market.alpaca_stock import ADJUSTMENT, FEED
from investment_analyst.storage import LocalStorage
from investment_analyst.time_intervals import inclusive_utc_date_bounds


class AaplMarketRefreshPlanner:
    """Plan missing prefix and suffix intervals from bars and fetch receipts."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        configuration: AlpacaAssetConfiguration | None = None,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._asset_id = configuration.asset_id if configuration is not None else ASSET_ID
        self._source_id = configuration.source_id if configuration is not None else SOURCE_ID
        self._symbol = configuration.symbol if configuration is not None else "AAPL"

    def plan(
        self,
        *,
        requested_start: date,
        requested_end: date,
        refresh_mode: AaplRefreshMode,
    ) -> AaplMarketRefreshPlan:
        """Return one deterministic read-only refresh plan for the requested date range."""
        persisted = self._persisted_timestamps()
        earliest = min(persisted) if persisted else None
        latest = max(persisted) if persisted else None

        if refresh_mode is AaplRefreshMode.FULL:
            return self._plan(
                requested_start=requested_start,
                requested_end=requested_end,
                persisted_earliest=earliest,
                persisted_latest=latest,
                fetch_intervals=(AaplMarketDateInterval(start=requested_start, end=requested_end),),
                mode=AaplMarketRefreshMode.FULL,
                reason="Full refresh explicitly requested; persisted identities remain reusable.",
            )

        coverage = self._merged_coverage(persisted)
        if not coverage:
            return self._plan(
                requested_start=requested_start,
                requested_end=requested_end,
                persisted_earliest=None,
                persisted_latest=None,
                fetch_intervals=(AaplMarketDateInterval(start=requested_start, end=requested_end),),
                mode=AaplMarketRefreshMode.INITIAL,
                reason=(
                    f"No persisted {self._symbol} IEX bars or completed fetch receipts were found."
                ),
            )

        requested_start_at, requested_end_at = inclusive_utc_date_bounds(
            requested_start,
            requested_end,
        )
        coverage_start = coverage[0][0]
        coverage_end = coverage[-1][1]
        intervals: list[AaplMarketDateInterval] = []

        if requested_start_at < coverage_start:
            prefix_end = min(requested_end_at, coverage_start)
            if requested_start_at < prefix_end:
                intervals.append(
                    AaplMarketDateInterval(
                        start=requested_start_at.date(),
                        end=(prefix_end - timedelta(days=1)).date(),
                    )
                )

        if requested_end_at > coverage_end:
            suffix_start = max(requested_start_at, coverage_end)
            if suffix_start < requested_end_at:
                intervals.append(
                    AaplMarketDateInterval(
                        start=suffix_start.date(),
                        end=(requested_end_at - timedelta(days=1)).date(),
                    )
                )

        if not intervals:
            return self._plan(
                requested_start=requested_start,
                requested_end=requested_end,
                persisted_earliest=earliest,
                persisted_latest=latest,
                fetch_intervals=(),
                mode=AaplMarketRefreshMode.ALREADY_CURRENT,
                reason=(
                    f"The requested range is covered by persisted {self._symbol} IEX bars "
                    "and completed fetch receipts."
                ),
            )

        if intervals[0].start == requested_start and requested_start_at < coverage_start:
            mode = AaplMarketRefreshMode.BACKFILL
            reason = "Persisted coverage requires an earlier prefix" + (
                " and a later suffix." if len(intervals) == 2 else "."
            )
        else:
            mode = AaplMarketRefreshMode.INCREMENTAL
            reason = "Persisted coverage requires only a later suffix."

        return self._plan(
            requested_start=requested_start,
            requested_end=requested_end,
            persisted_earliest=earliest,
            persisted_latest=latest,
            fetch_intervals=tuple(intervals),
            mode=mode,
            reason=reason,
        )

    def latest_available_at(self) -> datetime | None:
        """Return the latest availability cut for this exact asset and source."""
        observation_latest = self._storage.observations.maximum_available_at(
            asset_id=self._asset_id,
            source_id=self._source_id,
        )
        _, receipt_latest = self._storage.raw_records.available_at_bounds(
            asset_id=self._asset_id,
            source_id=self._source_id,
            schema_version=ALPACA_FETCH_RECEIPT_SCHEMA,
        )
        candidates = tuple(
            item.astimezone(UTC)
            for item in (observation_latest, receipt_latest)
            if item is not None
        )
        return max(candidates, default=None)

    def _persisted_timestamps(self) -> tuple[datetime, ...]:
        earliest, latest = self._storage.observations.observed_at_bounds(
            asset_id=self._asset_id,
            source_id=self._source_id,
            frequency=DataFrequency.DAY_1,
        )
        return tuple(item.astimezone(UTC) for item in (earliest, latest) if item is not None)

    def _merged_coverage(
        self,
        persisted: tuple[datetime, ...],
    ) -> tuple[tuple[datetime, datetime], ...]:
        intervals: list[tuple[datetime, datetime]] = []
        if persisted:
            intervals.append(
                (
                    datetime.combine(min(persisted).date(), time.min, tzinfo=UTC),
                    datetime.combine(
                        max(persisted).date() + timedelta(days=1),
                        time.min,
                        tzinfo=UTC,
                    ),
                )
            )
        for record in self._storage.raw_records.list(
            asset_id=self._asset_id,
            source_id=self._source_id,
            schema_version=ALPACA_FETCH_RECEIPT_SCHEMA,
        ):
            receipt = alpaca_fetch_receipt_from_raw_record(record)
            if receipt is None:
                continue
            if (
                receipt.asset_id != self._asset_id
                or receipt.source_id != self._source_id
                or receipt.feed != FEED
                or receipt.adjustment != ADJUSTMENT
                or receipt.interval_semantics != ALPACA_INTERVAL_SEMANTICS
                or not alpaca_receipt_covers_calendar_days(receipt)
            ):
                continue
            intervals.append((receipt.requested_start, receipt.requested_end))
        return self._merge_intervals(intervals)

    @staticmethod
    def _merge_intervals(
        intervals: list[tuple[datetime, datetime]],
    ) -> tuple[tuple[datetime, datetime], ...]:
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
                continue
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        return tuple(merged)

    @staticmethod
    def _plan(
        *,
        requested_start: date,
        requested_end: date,
        persisted_earliest: datetime | None,
        persisted_latest: datetime | None,
        fetch_intervals: tuple[AaplMarketDateInterval, ...],
        mode: AaplMarketRefreshMode,
        reason: str,
    ) -> AaplMarketRefreshPlan:
        return AaplMarketRefreshPlan(
            requested_start=requested_start,
            requested_end=requested_end,
            persisted_earliest=persisted_earliest,
            persisted_latest=persisted_latest,
            fetch_intervals=fetch_intervals,
            mode=mode,
            market_fetch_required=bool(fetch_intervals),
            reason=reason,
            traceability_verified=True,
        )
