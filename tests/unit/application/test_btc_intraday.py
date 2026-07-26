"""Tests for bounded BTC-USD intraday application contracts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.intraday_models import (
    AggregatedIntradayBar,
    IntradayInterval,
)
from investment_analyst.application.btc_intraday import (
    BtcIntradayRefreshError,
    refresh_btc_intraday,
)
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayChart,
    BtcIntradayChartRequest,
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.core.models import DataQuality
from investment_analyst.providers.crypto.coinbase_pipeline import (
    CoinbaseImportSummary,
    CoinbaseIntradayPipeline,
)


class _FakePipeline:
    def __init__(self, summary: CoinbaseImportSummary) -> None:
        self.summary = summary
        self.ranges: list[tuple[datetime, datetime]] = []

    def run(self, start: datetime, end: datetime) -> CoinbaseImportSummary:
        self.ranges.append((start, end))
        return self.summary


def _import_summary(
    *,
    requested_end: datetime,
    raw_records_created: int,
    raw_records_reused: int,
) -> CoinbaseImportSummary:
    requested_start = requested_end - timedelta(hours=24)
    candle = requested_start
    missing = tuple(requested_start + timedelta(minutes=index) for index in range(1, 1_440))
    return CoinbaseImportSummary(
        asset_id="crypto:btc-usd",
        source_id="coinbase-exchange:btc-usd:minute-1-candles",
        requested_start=requested_start,
        requested_end=requested_end,
        retrieved_at=requested_end + timedelta(seconds=15),
        request_count=5,
        candles_received=1,
        raw_records_created=raw_records_created,
        raw_records_reused=raw_records_reused,
        observations_created=raw_records_created * 5,
        observations_reused=raw_records_reused * 5,
        missing_intervals=missing,
        earliest_candle=candle,
        latest_candle=candle,
        traceability_verified=True,
    )


def test_chart_request_uses_latest_24_complete_minutes() -> None:
    request = BtcIntradayChartRequest(
        known_at=datetime(2026, 7, 25, 15, 42, 59, 123456, tzinfo=UTC),
        interval=IntradayInterval.MINUTE_15,
    )

    assert request.query_end == datetime(2026, 7, 25, 15, 42, tzinfo=UTC)
    assert request.query_start == datetime(2026, 7, 24, 15, 42, tzinfo=UTC)
    assert request.lookback_hours == 24


def test_chart_accepts_a_fixed_bucket_that_overlaps_the_window_edge() -> None:
    start = datetime(2026, 7, 24, 15, 42, tzinfo=UTC)
    raw_ids = tuple(uuid4() for _ in range(3))
    volume_ids = tuple(uuid4() for _ in range(3))
    bar = AggregatedIntradayBar(
        asset_id="crypto:btc-usd",
        source_id="coinbase-exchange:btc-usd:minute-1-candles",
        interval=IntradayInterval.MINUTE_5,
        period_start=start - timedelta(minutes=2),
        period_end=start + timedelta(minutes=3),
        available_at=start + timedelta(minutes=5),
        source_bar_count=3,
        expected_source_bar_count=5,
        interval_complete=False,
        open=Decimal("117000"),
        high=Decimal("117100"),
        low=Decimal("116900"),
        close=Decimal("117050"),
        volume=Decimal("1.25"),
        quality=DataQuality.PARTIAL,
        raw_record_ids=raw_ids,
        open_observation_id=uuid4(),
        high_observation_id=uuid4(),
        low_observation_id=uuid4(),
        close_observation_id=uuid4(),
        volume_input_observation_ids=volume_ids,
    )

    chart = BtcIntradayChart(
        known_at=start + timedelta(days=1, minutes=5),
        start=start,
        end=start + timedelta(days=1),
        interval=IntradayInterval.MINUTE_5,
        bars=(bar,),
        source_bar_count=3,
        complete_interval_count=0,
        incomplete_interval_count=1,
    )

    assert chart.bars == (bar,)
    assert chart.incomplete_interval_count == 1


def test_refresh_summary_exposes_idempotent_reuse_counts() -> None:
    requested_end = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
    pipeline = _FakePipeline(
        _import_summary(
            requested_end=requested_end,
            raw_records_created=0,
            raw_records_reused=1,
        )
    )
    request = BtcIntradayRefreshRequest(requested_end=requested_end)

    first = refresh_btc_intraday(
        cast(CoinbaseIntradayPipeline, pipeline),
        request,
        now=requested_end,
    )
    second = refresh_btc_intraday(
        cast(CoinbaseIntradayPipeline, pipeline),
        request,
        now=requested_end,
    )

    assert first == second
    assert first.schema_version == "btc-intraday-refresh-v1"
    assert first.raw_records_created == 0
    assert first.raw_records_reused == 1
    assert first.observations_created == 0
    assert first.observations_reused == 5
    assert first.traceability_verified
    assert pipeline.ranges == [
        (requested_end - timedelta(hours=24), requested_end),
        (requested_end - timedelta(hours=24), requested_end),
    ]


def test_refresh_rejects_future_or_misaligned_ranges() -> None:
    requested_end = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
    pipeline = _FakePipeline(
        _import_summary(
            requested_end=requested_end,
            raw_records_created=1,
            raw_records_reused=0,
        )
    )

    with pytest.raises(BtcIntradayRefreshError, match="must not be in the future"):
        refresh_btc_intraday(
            cast(CoinbaseIntradayPipeline, pipeline),
            BtcIntradayRefreshRequest(requested_end=requested_end),
            now=requested_end - timedelta(minutes=1),
        )
    with pytest.raises(ValidationError, match="whole UTC minute"):
        BtcIntradayRefreshRequest(
            requested_end=requested_end + timedelta(seconds=1),
        )

    assert pipeline.ranges == []


def test_refresh_summary_rejects_unaccounted_source_minutes() -> None:
    summary = _import_summary(
        requested_end=datetime(2026, 7, 25, 16, 0, tzinfo=UTC),
        raw_records_created=1,
        raw_records_reused=0,
    )
    invalid = replace(summary, missing_intervals=())

    with pytest.raises(ValidationError, match="coverage does not match"):
        BtcIntradayRefreshSummary.from_import(invalid)
