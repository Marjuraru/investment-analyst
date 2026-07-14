"""Tests for Decimal-only historical market-statistics calculations."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.statistics_definitions import (
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_engine import (
    MarketStatisticsEngine,
    MarketStatisticsTraceabilityError,
)
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.core.models import DataFrequency, DataQuality


def _series(
    closes: tuple[str, ...],
    *,
    volumes: tuple[str, ...] | None = None,
    qualities: tuple[DataQuality, ...] | None = None,
) -> MarketBarSeries:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    known_at = datetime(2026, 3, 1, tzinfo=UTC)
    query = HistoricalBarQuery(
        asset_id="crypto:btc-usd",
        source_id="coinbase-exchange:btc-usd:daily-candles",
        start=start,
        end=start + timedelta(days=max(len(closes), 1) + 1),
        known_at=known_at,
    )
    volume_values = volumes or tuple("100" for _ in closes)
    quality_values = qualities or tuple(DataQuality.VALID for _ in closes)
    bars = tuple(
        MarketBar(
            asset_id=query.asset_id,
            source_id=query.source_id,
            raw_record_id=uuid4(),
            frequency=DataFrequency.DAY_1,
            timestamp=start + timedelta(days=index),
            available_at=start + timedelta(days=index, hours=1),
            open=Decimal(close),
            high=Decimal(close) + Decimal("1"),
            low=Decimal(close) - Decimal("1"),
            close=Decimal(close),
            volume=Decimal(volume_values[index]),
            quality=quality_values[index],
            observation_ids={
                "open": uuid4(),
                "high": uuid4(),
                "low": uuid4(),
                "close": uuid4(),
                "volume": uuid4(),
            },
        )
        for index, close in enumerate(closes)
    )
    return MarketBarSeries(
        query=query,
        bars=bars,
        coverage=MarketBarCoverage(
            candidate_versions=len(bars),
            selected_versions=len(bars),
            discarded_revisions=0,
            bar_count=len(bars),
            earliest_timestamp=bars[0].timestamp if bars else None,
            latest_timestamp=bars[-1].timestamp if bars else None,
        ),
        traceability_verified=True,
    )


def _request(series: MarketBarSeries, *, sma=(1, 3), volatility=2, relative=2):
    return MarketStatisticsRequest(
        query=series.query,
        sma_windows=sma,
        volatility_window=volatility,
        relative_volume_window=relative,
    )


def _items(computation, key: str):
    return [item for item in computation.calculations if item.metric_key == key]


def test_empty_and_single_bar_series_are_valid() -> None:
    empty = _series(())
    one = _series(("100",))

    assert MarketStatisticsEngine().compute(empty, _request(empty)).calculations == ()
    one_result = MarketStatisticsEngine().compute(one, _request(one))
    assert len(_items(one_result, SMA_KEY)) == 1
    assert not _items(one_result, SIMPLE_RETURN_KEY)


def test_returns_sma_volatility_and_relative_volume_are_exact() -> None:
    series = _series(("100", "110", "99"), volumes=("100", "200", "300"))
    result = MarketStatisticsEngine().compute(series, _request(series))

    returns = _items(result, SIMPLE_RETURN_KEY)
    sma_three = [item for item in _items(result, SMA_KEY) if item.parameters["window"] == 3]
    volatility = _items(result, VOLATILITY_KEY)
    relative = _items(result, RELATIVE_VOLUME_KEY)

    assert [item.value for item in returns] == [Decimal("0.1"), Decimal("-0.1")]
    assert sma_three[0].value == Decimal("103")
    assert volatility[0].value == Decimal("0.1414213562373095048801688724209698")
    assert relative[0].value == Decimal("2")
    assert relative[0].input_observation_ids == tuple(
        bar.observation_ids["volume"] for bar in series.bars
    )


def test_gaps_use_previous_available_bar_and_warmup_is_counted() -> None:
    series = _series(("100", "105", "110"))
    shifted = series.model_copy(
        update={
            "bars": (
                series.bars[0],
                series.bars[1].model_copy(
                    update={"timestamp": series.bars[1].timestamp + timedelta(days=3)}
                ),
                series.bars[2].model_copy(
                    update={"timestamp": series.bars[2].timestamp + timedelta(days=3)}
                ),
            )
        }
    )
    shifted = MarketBarSeries(
        query=series.query.model_copy(update={"end": series.query.end + timedelta(days=3)}),
        bars=shifted.bars,
        coverage=series.coverage.model_copy(
            update={"latest_timestamp": shifted.bars[-1].timestamp}
        ),
        traceability_verified=True,
    )
    request = _request(shifted)
    result = MarketStatisticsEngine().compute(shifted, request)

    assert _items(result, SIMPLE_RETURN_KEY)[0].value == Decimal("0.05")
    assert result.warmup_counts[SIMPLE_RETURN_KEY] == 1
    assert result.warmup_counts[f"{SMA_KEY}:3"] == 2


def test_zero_volume_baseline_is_skipped() -> None:
    series = _series(("100", "101", "102"), volumes=("0", "0", "10"))
    result = MarketStatisticsEngine().compute(series, _request(series))

    assert not _items(result, RELATIVE_VOLUME_KEY)
    assert result.zero_denominator_skips[f"{RELATIVE_VOLUME_KEY}:2"] == 1


@pytest.mark.parametrize(
    ("qualities", "expected"),
    [
        ((DataQuality.VALID, DataQuality.DELAYED), DataQuality.DELAYED),
        ((DataQuality.VALID, DataQuality.PARTIAL), DataQuality.PARTIAL),
        ((DataQuality.PARTIAL, DataQuality.SUSPECT), DataQuality.SUSPECT),
    ],
)
def test_quality_precedence(qualities, expected) -> None:
    series = _series(("100", "101"), qualities=qualities)
    result = MarketStatisticsEngine().compute(series, _request(series, sma=(1,)))

    assert _items(result, SIMPLE_RETURN_KEY)[0].quality is expected


def test_context_and_inputs_are_not_modified() -> None:
    series = _series(("100", "101", "102"))
    original = series.model_dump(mode="python")
    precision = getcontext().prec

    MarketStatisticsEngine().compute(series, _request(series))

    assert getcontext().prec == precision
    assert series.model_dump(mode="python") == original


def test_mismatched_query_is_rejected() -> None:
    series = _series(("100", "101"))
    other_query = series.query.model_copy(
        update={"known_at": series.query.known_at + timedelta(days=1)}
    )

    with pytest.raises(MarketStatisticsTraceabilityError, match="does not match"):
        MarketStatisticsEngine().compute(
            series,
            MarketStatisticsRequest(
                query=other_query,
                sma_windows=(1,),
                volatility_window=2,
                relative_volume_window=1,
            ),
        )
