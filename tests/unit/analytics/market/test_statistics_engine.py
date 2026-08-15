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
from investment_analyst.analytics.market.bar_schemas import COINBASE_INTRADAY_SOURCE_ID
from investment_analyst.analytics.market.statistics_definitions import (
    BOLLINGER_BANDWIDTH_KEY,
    BOLLINGER_LOWER_KEY,
    BOLLINGER_PERCENT_B_KEY,
    BOLLINGER_UPPER_KEY,
    EMA_KEY,
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_engine import (
    MarketStatisticsEngine,
    MarketStatisticsTraceabilityError,
)
from investment_analyst.analytics.market.statistics_identity import metric_result_id
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
            low=Decimal(close) - Decimal("0.5"),
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


def test_intraday_source_is_rejected_even_when_series_is_empty() -> None:
    daily = _series(())
    query = daily.query.model_copy(update={"source_id": COINBASE_INTRADAY_SOURCE_ID})
    minute = daily.model_copy(update={"query": query})

    with pytest.raises(MarketStatisticsTraceabilityError, match="DAY_1 source"):
        MarketStatisticsEngine().compute(minute, _request(minute))


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


def test_bollinger_bands_are_population_decimal_only_and_traceable() -> None:
    series = _series(("100", "110", "99"))
    request = MarketStatisticsRequest(
        query=series.query,
        sma_windows=(1,),
        volatility_window=2,
        relative_volume_window=2,
        bollinger_window=3,
        bollinger_multiplier=Decimal("2"),
    )
    result = MarketStatisticsEngine().compute(series, request)

    upper = _items(result, BOLLINGER_UPPER_KEY)[0]
    lower = _items(result, BOLLINGER_LOWER_KEY)[0]
    bandwidth = _items(result, BOLLINGER_BANDWIDTH_KEY)[0]
    percent_b = _items(result, BOLLINGER_PERCENT_B_KEY)[0]

    assert upper.value == Decimal("112.9331096171675598128878773640826")
    assert lower.value == Decimal("93.06689038283244018711212263591737")
    assert bandwidth.value == Decimal("0.1928759148964574720949102400792741")
    assert percent_b.value == Decimal("0.2986531834357927064955159993767037")
    assert upper.input_observation_ids == tuple(bar.observation_ids["close"] for bar in series.bars)
    assert upper.parameters["multiplier"] == "2"
    assert result.warmup_counts[f"{BOLLINGER_UPPER_KEY}:3"] == 2


def test_flat_bollinger_band_omits_percent_b_and_counts_the_zero_denominator() -> None:
    series = _series(("100", "100", "100"))
    result = MarketStatisticsEngine().compute(
        series,
        MarketStatisticsRequest(
            query=series.query,
            sma_windows=(1,),
            volatility_window=2,
            relative_volume_window=2,
            bollinger_window=3,
        ),
    )

    assert _items(result, BOLLINGER_UPPER_KEY)[0].value == Decimal("100")
    assert _items(result, BOLLINGER_LOWER_KEY)[0].value == Decimal("100")
    assert not _items(result, BOLLINGER_PERCENT_B_KEY)
    assert result.zero_denominator_skips[f"{BOLLINGER_PERCENT_B_KEY}:3"] == 1


def test_ema_uses_in_query_sma_seed_and_linear_derived_lineage() -> None:
    series = _series(("1", "2", "3", "4", "5"))
    result = MarketStatisticsEngine().compute(
        series,
        MarketStatisticsRequest(
            query=series.query,
            sma_windows=(1,),
            volatility_window=2,
            relative_volume_window=2,
            bollinger_window=2,
            ema_windows=(3,),
        ),
    )

    ema = _items(result, EMA_KEY)

    assert [item.value for item in ema] == [Decimal("2"), Decimal("3"), Decimal("4")]
    assert ema[0].input_observation_ids == tuple(
        bar.observation_ids["close"] for bar in series.bars[:3]
    )
    assert ema[0].input_metric_result_ids == ()
    assert ema[1].input_observation_ids == (series.bars[3].observation_ids["close"],)
    assert ema[1].input_metric_result_ids == (metric_result_id(ema[0], series.query.known_at),)
    assert ema[2].input_metric_result_ids == (metric_result_id(ema[1], series.query.known_at),)
    assert ema[0].parameters["alpha"] == "0.5"
    assert ema[0].parameters["seed_start"] == series.query.start.isoformat()
    assert result.warmup_counts[f"{EMA_KEY}:3"] == 2


def test_rsi_macd_and_atr_are_decimal_traceable_after_their_warmups() -> None:
    series = _series(tuple(str(100 + index) for index in range(40)))
    result = MarketStatisticsEngine().compute(
        series,
        MarketStatisticsRequest(
            query=series.query,
            sma_windows=(1,),
            volatility_window=2,
            relative_volume_window=2,
            bollinger_window=2,
            ema_windows=(2,),
        ),
    )

    rsi = _items(result, "market.technical.rsi")
    atr = _items(result, "market.technical.atr")
    histogram = _items(result, "market.technical.macd.histogram")

    assert rsi[0].value == Decimal("100")
    assert rsi[0].as_of == series.bars[14].timestamp
    assert atr[0].as_of == series.bars[13].timestamp
    assert histogram
    assert all(item.input_metric_result_ids for item in histogram)
    assert result.warmup_counts["market.technical.rsi:14"] == 14


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
