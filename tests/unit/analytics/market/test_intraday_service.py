"""Tests for deterministic fixed-UTC aggregation of minute evidence."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.bar_schemas import (
    COINBASE_INTRADAY_SOURCE_ID,
    COINBASE_SOURCE_ID,
)
from investment_analyst.analytics.market.intraday_models import (
    IntradayAggregationRequest,
    IntradayInterval,
)
from investment_analyst.analytics.market.intraday_service import (
    IntradayAggregationError,
    IntradayAggregationService,
)
from investment_analyst.core.models import DataFrequency, DataQuality


def _series(count: int = 6) -> MarketBarSeries:
    start = datetime(2026, 7, 12, 12, tzinfo=UTC)
    known_at = datetime(2026, 7, 12, 13, tzinfo=UTC)
    query = HistoricalBarQuery(
        asset_id="crypto:btc-usd",
        source_id=COINBASE_INTRADAY_SOURCE_ID,
        start=start,
        end=start + timedelta(minutes=count),
        known_at=known_at,
    )
    bars: list[MarketBar] = []
    for index in range(count):
        open_value = Decimal("100") + Decimal(index)
        bars.append(
            MarketBar(
                asset_id=query.asset_id,
                source_id=query.source_id,
                raw_record_id=uuid4(),
                frequency=DataFrequency.MINUTE_1,
                timestamp=start + timedelta(minutes=index),
                available_at=known_at - timedelta(minutes=1),
                open=open_value,
                high=open_value + Decimal("2"),
                low=open_value - Decimal("1"),
                close=open_value + Decimal("1"),
                volume=Decimal(index + 1),
                quality=DataQuality.DELAYED if index == 3 else DataQuality.VALID,
                observation_ids={
                    "open": uuid4(),
                    "high": uuid4(),
                    "low": uuid4(),
                    "close": uuid4(),
                    "volume": uuid4(),
                },
            )
        )
    return MarketBarSeries(
        query=query,
        bars=tuple(bars),
        coverage=MarketBarCoverage(
            candidate_versions=count,
            selected_versions=count,
            discarded_revisions=0,
            bar_count=count,
            earliest_timestamp=bars[0].timestamp if bars else None,
            latest_timestamp=bars[-1].timestamp if bars else None,
        ),
        traceability_verified=True,
    )


def test_aggregates_complete_and_incomplete_five_minute_buckets() -> None:
    series = _series()
    request = IntradayAggregationRequest(
        query=series.query,
        interval=IntradayInterval.MINUTE_5,
    )

    result = IntradayAggregationService().aggregate(series, request)

    assert result.source_bar_count == 6
    assert result.complete_interval_count == 1
    assert result.incomplete_interval_count == 1
    assert result.traceability_verified
    first, second = result.bars
    assert first.period_start == datetime(2026, 7, 12, 12, tzinfo=UTC)
    assert first.period_end == datetime(2026, 7, 12, 12, 5, tzinfo=UTC)
    assert first.interval_complete
    assert first.source_bar_count == 5
    assert first.open == Decimal("100")
    assert first.high == Decimal("106")
    assert first.low == Decimal("99")
    assert first.close == Decimal("105")
    assert first.volume == Decimal("15")
    assert first.quality is DataQuality.DELAYED
    assert first.raw_record_ids == tuple(bar.raw_record_id for bar in series.bars[:5])
    assert first.open_observation_id == series.bars[0].observation_ids["open"]
    assert first.high_observation_id == series.bars[4].observation_ids["high"]
    assert first.low_observation_id == series.bars[0].observation_ids["low"]
    assert first.close_observation_id == series.bars[4].observation_ids["close"]
    assert first.volume_input_observation_ids == tuple(
        bar.observation_ids["volume"] for bar in series.bars[:5]
    )
    assert not second.interval_complete
    assert second.source_bar_count == 1
    assert second.period_start == datetime(2026, 7, 12, 12, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("interval", "seconds", "source_bars"),
    [
        (IntradayInterval.MINUTE_1, 60, 1),
        (IntradayInterval.MINUTE_5, 300, 5),
        (IntradayInterval.MINUTE_15, 900, 15),
        (IntradayInterval.MINUTE_30, 1_800, 30),
        (IntradayInterval.MINUTE_45, 2_700, 45),
        (IntradayInterval.HOUR_1, 3_600, 60),
        (IntradayInterval.HOUR_2, 7_200, 120),
        (IntradayInterval.HOUR_4, 14_400, 240),
        (IntradayInterval.HOUR_5, 18_000, 300),
    ],
)
def test_supported_intervals_have_fixed_scalable_widths(
    interval: IntradayInterval,
    seconds: int,
    source_bars: int,
) -> None:
    assert interval.seconds == seconds
    assert interval.expected_source_bars == source_bars


def test_daily_source_is_rejected_even_for_an_empty_request() -> None:
    query = _series(1).query.model_copy(update={"source_id": COINBASE_SOURCE_ID})

    with pytest.raises(ValidationError, match="MINUTE_1"):
        IntradayAggregationRequest(query=query, interval=IntradayInterval.MINUTE_5)


def test_service_rejects_non_minute_bar_evidence() -> None:
    series = _series(1)
    invalid = series.model_copy(
        update={"bars": (series.bars[0].model_copy(update={"frequency": DataFrequency.DAY_1}),)}
    )
    request = IntradayAggregationRequest(
        query=series.query,
        interval=IntradayInterval.MINUTE_1,
    )

    with pytest.raises(IntradayAggregationError, match="MINUTE_1"):
        IntradayAggregationService().aggregate(invalid, request)
