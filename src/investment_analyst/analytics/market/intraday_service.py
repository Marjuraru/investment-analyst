"""Deterministic UTC aggregation of verified one-minute market bars."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import UUID

from investment_analyst.analytics.market.bar_models import MarketBar, MarketBarSeries
from investment_analyst.analytics.market.intraday_models import (
    AggregatedIntradayBar,
    IntradayAggregationRequest,
    IntradayAggregationSeries,
)
from investment_analyst.core.models import DataFrequency, DataQuality

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_QUALITY_PRIORITY = {
    DataQuality.VALID: 0,
    DataQuality.DELAYED: 1,
    DataQuality.PARTIAL: 2,
    DataQuality.SUSPECT: 3,
}


class IntradayAggregationError(RuntimeError):
    """Raised when one-minute evidence cannot be aggregated safely."""


class IntradayAggregationService:
    """Aggregate one-minute bars without persisting or inventing missing intervals."""

    def aggregate(
        self,
        series: MarketBarSeries,
        request: IntradayAggregationRequest,
    ) -> IntradayAggregationSeries:
        """Return ordered fixed UTC buckets with exact source evidence."""
        self._validate_input(series, request)
        grouped: dict[datetime, list[MarketBar]] = defaultdict(list)
        for bar in series.bars:
            grouped[self._bucket_start(bar.timestamp, request.interval.seconds)].append(bar)
        bars = tuple(
            self._aggregate_group(tuple(grouped[start]), request) for start in sorted(grouped)
        )
        complete = sum(bar.interval_complete for bar in bars)
        return IntradayAggregationSeries(
            request=request,
            bars=bars,
            source_bar_count=len(series.bars),
            complete_interval_count=complete,
            incomplete_interval_count=len(bars) - complete,
            traceability_verified=True,
        )

    @staticmethod
    def _validate_input(
        series: MarketBarSeries,
        request: IntradayAggregationRequest,
    ) -> None:
        if not series.traceability_verified:
            raise IntradayAggregationError("minute-series traceability is not verified")
        if series.query != request.query:
            raise IntradayAggregationError("minute series query does not match request")
        for bar in series.bars:
            if bar.frequency is not DataFrequency.MINUTE_1:
                raise IntradayAggregationError("intraday aggregation requires MINUTE_1 bars")
            if bar.timestamp.second != 0 or bar.timestamp.microsecond != 0:
                raise IntradayAggregationError("minute bars must align to whole UTC minutes")

    @classmethod
    def _aggregate_group(
        cls,
        bars: tuple[MarketBar, ...],
        request: IntradayAggregationRequest,
    ) -> AggregatedIntradayBar:
        ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
        first = ordered[0]
        last = ordered[-1]
        highest = max(ordered, key=lambda bar: bar.high)
        lowest = min(ordered, key=lambda bar: bar.low)
        period_start = cls._bucket_start(first.timestamp, request.interval.seconds)
        period_end = period_start + timedelta(seconds=request.interval.seconds)
        volume = sum((bar.volume for bar in ordered), Decimal("0"))

        trade_values = tuple(bar.trade_count for bar in ordered)
        trade_count: Decimal | None = None
        trade_ids: tuple[UUID, ...] = ()
        if all(value is not None for value in trade_values):
            trade_count = sum(
                (value for value in trade_values if value is not None),
                Decimal("0"),
            )
            trade_ids = tuple(bar.observation_ids["trade_count"] for bar in ordered)

        vwap_values = tuple(bar.vwap for bar in ordered)
        vwap: Decimal | None = None
        vwap_ids: tuple[UUID, ...] = ()
        if volume > 0 and all(value is not None for value in vwap_values):
            with localcontext(Context(prec=34)):
                vwap = (
                    sum(
                        (
                            value * bar.volume
                            for bar, value in zip(ordered, vwap_values, strict=True)
                            if value is not None
                        ),
                        Decimal("0"),
                    )
                    / volume
                )
            vwap_ids = tuple(bar.observation_ids["vwap"] for bar in ordered)

        expected_times = tuple(
            period_start + timedelta(minutes=index)
            for index in range(request.interval.expected_source_bars)
        )
        complete = tuple(bar.timestamp for bar in ordered) == expected_times
        return AggregatedIntradayBar(
            asset_id=request.query.asset_id,
            source_id=request.query.source_id,
            interval=request.interval,
            period_start=period_start,
            period_end=period_end,
            available_at=max(bar.available_at for bar in ordered),
            source_bar_count=len(ordered),
            expected_source_bar_count=request.interval.expected_source_bars,
            interval_complete=complete,
            open=first.open,
            high=highest.high,
            low=lowest.low,
            close=last.close,
            volume=volume,
            trade_count=trade_count,
            vwap=vwap,
            quality=max(ordered, key=lambda bar: _QUALITY_PRIORITY[bar.quality]).quality,
            raw_record_ids=tuple(bar.raw_record_id for bar in ordered),
            open_observation_id=first.observation_ids["open"],
            high_observation_id=highest.observation_ids["high"],
            low_observation_id=lowest.observation_ids["low"],
            close_observation_id=last.observation_ids["close"],
            volume_input_observation_ids=tuple(bar.observation_ids["volume"] for bar in ordered),
            trade_count_input_observation_ids=trade_ids,
            vwap_input_observation_ids=vwap_ids,
        )

    @staticmethod
    def _bucket_start(value: datetime, interval_seconds: int) -> datetime:
        elapsed = value - _EPOCH
        total_seconds = elapsed.days * 86_400 + elapsed.seconds
        return _EPOCH + timedelta(seconds=(total_seconds // interval_seconds) * interval_seconds)
