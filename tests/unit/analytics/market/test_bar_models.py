"""Tests for provider-independent market-bar models."""

import json
from datetime import UTC, datetime
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
from investment_analyst.core.models import DataFrequency, DataQuality


def _query(**overrides: object) -> HistoricalBarQuery:
    values: dict[str, object] = {
        "asset_id": "crypto:btc-usd",
        "source_id": "coinbase-exchange:btc-usd:daily-candles",
        "start": datetime(2026, 7, 1, tzinfo=UTC),
        "end": datetime(2026, 7, 4, tzinfo=UTC),
        "known_at": datetime(2026, 7, 5, tzinfo=UTC),
    }
    values.update(overrides)
    return HistoricalBarQuery.model_validate(values)


def _bar(**overrides: object) -> MarketBar:
    fields = {name: uuid4() for name in ("open", "high", "low", "close", "volume")}
    values: dict[str, object] = {
        "asset_id": "crypto:btc-usd",
        "source_id": "coinbase-exchange:btc-usd:daily-candles",
        "raw_record_id": uuid4(),
        "frequency": DataFrequency.DAY_1,
        "timestamp": datetime(2026, 7, 2, tzinfo=UTC),
        "available_at": datetime(2026, 7, 2, 1, tzinfo=UTC),
        "open": Decimal("100"),
        "high": Decimal("110"),
        "low": Decimal("90"),
        "close": Decimal("105"),
        "volume": Decimal("12.5"),
        "quality": DataQuality.VALID,
        "observation_ids": fields,
    }
    values.update(overrides)
    return MarketBar.model_validate(values)


def _coverage(bars: tuple[MarketBar, ...]) -> MarketBarCoverage:
    return MarketBarCoverage(
        candidate_versions=len(bars),
        selected_versions=len(bars),
        discarded_revisions=0,
        bar_count=len(bars),
        earliest_timestamp=min(bar.timestamp for bar in bars) if bars else None,
        latest_timestamp=max(bar.timestamp for bar in bars) if bars else None,
    )


def test_valid_btc_and_aapl_bars_preserve_decimal() -> None:
    btc = _bar()
    aapl_ids = {
        name: uuid4() for name in ("open", "high", "low", "close", "volume", "trade_count", "vwap")
    }
    aapl = _bar(
        asset_id="equity:us:aapl",
        source_id="alpaca-market-data:iex:aapl:daily-bars:adjustment-all",
        trade_count=Decimal("200"),
        vwap=Decimal("102"),
        quality=DataQuality.PARTIAL,
        observation_ids=aapl_ids,
    )

    assert isinstance(btc.close, Decimal)
    assert isinstance(aapl.trade_count, Decimal)
    assert aapl.vwap == Decimal("102")


def test_float_financial_value_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must not use float"):
        _bar(close=105.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"low": Decimal("111")},
        {"volume": Decimal("-1")},
        {
            "trade_count": Decimal("2.5"),
            "observation_ids": {
                **{name: uuid4() for name in ("open", "high", "low", "close", "volume")},
                "trade_count": uuid4(),
            },
        },
    ],
)
def test_invalid_numeric_bar_constraints(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _bar(**overrides)


def test_observation_ids_must_be_exact() -> None:
    with pytest.raises(ValidationError, match="must match exactly"):
        _bar(observation_ids={"open": uuid4()})


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _bar(timestamp=datetime(2026, 7, 2))


def test_series_validates_order_duplicates_range_and_known_at() -> None:
    first = _bar(timestamp=datetime(2026, 7, 1, tzinfo=UTC))
    second = _bar(timestamp=datetime(2026, 7, 2, tzinfo=UTC))

    with pytest.raises(ValidationError, match="ordered"):
        MarketBarSeries(
            query=_query(),
            bars=(second, first),
            coverage=_coverage((second, first)),
            traceability_verified=True,
        )
    with pytest.raises(ValidationError, match="duplicate"):
        MarketBarSeries(
            query=_query(),
            bars=(first, _bar(timestamp=first.timestamp)),
            coverage=MarketBarCoverage(
                candidate_versions=2,
                selected_versions=2,
                discarded_revisions=0,
                bar_count=2,
                earliest_timestamp=first.timestamp,
                latest_timestamp=first.timestamp,
            ),
            traceability_verified=True,
        )
    with pytest.raises(ValidationError, match="outside"):
        MarketBarSeries(
            query=_query(),
            bars=(_bar(timestamp=datetime(2026, 7, 4, tzinfo=UTC)),),
            coverage=_coverage((_bar(timestamp=datetime(2026, 7, 4, tzinfo=UTC)),)),
            traceability_verified=True,
        )
    future = _bar(available_at=datetime(2026, 7, 6, tzinfo=UTC))
    with pytest.raises(ValidationError, match="known_at"):
        MarketBarSeries(
            query=_query(),
            bars=(future,),
            coverage=_coverage((future,)),
            traceability_verified=True,
        )


def test_series_serialization_is_json_compatible() -> None:
    bar = _bar()
    series = MarketBarSeries(
        query=_query(),
        bars=(bar,),
        coverage=_coverage((bar,)),
        traceability_verified=True,
    )

    payload = series.to_json_dict()
    encoded = json.dumps(payload)

    assert '"close": "105"' in encoded
    assert str(bar.raw_record_id) in encoded
