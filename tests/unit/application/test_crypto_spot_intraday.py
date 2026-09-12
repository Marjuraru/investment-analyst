"""Cross-identity tests for the generic crypto spot intraday contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.intraday_models import (
    AggregatedIntradayBar,
    IntradayAggregationRequest,
    IntradayAggregationSeries,
    IntradayInterval,
)
from investment_analyst.application.btc_intraday_models import (
    CryptoSpotIntradayChart,
    CryptoSpotIntradayChartRequest,
    CryptoSpotIntradayRefreshRequest,
    CryptoSpotIntradayRefreshSummary,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.catalog.provider_configuration import (
    resolve_coinbase_intraday_configuration,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextError,
)
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.core.models import DataQuality

_EVIDENCE_SIZE = 5


def _bar(
    *,
    asset_id: str,
    source_id: str,
    period_start: datetime,
    available_at: datetime,
) -> AggregatedIntradayBar:
    return AggregatedIntradayBar(
        asset_id=asset_id,
        source_id=source_id,
        interval=IntradayInterval.MINUTE_5,
        period_start=period_start,
        period_end=period_start + timedelta(minutes=5),
        available_at=available_at,
        source_bar_count=_EVIDENCE_SIZE,
        expected_source_bar_count=_EVIDENCE_SIZE,
        interval_complete=True,
        open=Decimal("117000.125"),
        high=Decimal("117100.5"),
        low=Decimal("116900.25"),
        close=Decimal("117050.75"),
        volume=Decimal("1.25"),
        quality=DataQuality.VALID,
        raw_record_ids=tuple(uuid4() for _ in range(_EVIDENCE_SIZE)),
        open_observation_id=uuid4(),
        high_observation_id=uuid4(),
        low_observation_id=uuid4(),
        close_observation_id=uuid4(),
        volume_input_observation_ids=tuple(uuid4() for _ in range(_EVIDENCE_SIZE)),
    )


def _series(
    *,
    asset_id: str,
    source_id: str,
    known_at: datetime,
) -> IntradayAggregationSeries:
    start = (known_at - timedelta(hours=24)).replace(second=0, microsecond=0)
    query = HistoricalBarQuery(
        asset_id=asset_id,
        source_id=source_id,
        start=start,
        end=known_at.replace(second=0, microsecond=0),
        known_at=known_at,
    )
    bar = _bar(
        asset_id=asset_id,
        source_id=source_id,
        period_start=start,
        available_at=start + timedelta(minutes=5),
    )
    return IntradayAggregationSeries(
        request=IntradayAggregationRequest(query=query, interval=IntradayInterval.MINUTE_5),
        bars=(bar,),
        source_bar_count=_EVIDENCE_SIZE,
        complete_interval_count=1,
        incomplete_interval_count=0,
    )


def _request(asset_id: str, known_at: datetime) -> CryptoSpotIntradayChartRequest:
    return CryptoSpotIntradayChartRequest(
        asset_id=asset_id,
        known_at=known_at,
        interval=IntradayInterval.MINUTE_5,
    )


def test_intraday_request_for_an_asset_without_minute_bars_fails_closed_and_never_returns_another_assets_data() -> (  # noqa: E501
    None
):
    known_at = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
    btc_source = "coinbase-exchange:btc-usd:minute-1-candles"

    resolver = ApplicationRuntime.create_default().provider_resolver
    with pytest.raises(ProviderAssetContextError):
        resolve_coinbase_intraday_configuration(resolver, asset_id="crypto:eth-usd")

    chart = CryptoSpotIntradayChart.from_series(
        _request("crypto:btc-usd", known_at),
        _series(asset_id="crypto:btc-usd", source_id=btc_source, known_at=known_at),
    )

    assert chart.schema_version == "crypto-spot-intraday-chart-v1"
    assert chart.asset_id == "crypto:btc-usd"
    assert chart.source_id == btc_source
    assert all(bar.asset_id == "crypto:btc-usd" for bar in chart.bars)
    assert all(bar.source_id == btc_source for bar in chart.bars)
    assert chart.to_json_dict()["bars"][0]["close"] == "117050.75"

    with pytest.raises(ValueError, match="does not match the requested asset"):
        CryptoSpotIntradayChart.from_series(
            _request("crypto:btc-usd", known_at),
            _series(asset_id="crypto:eth-usd", source_id=btc_source, known_at=known_at),
        )


def test_intraday_resolution_for_an_asset_without_minute_bars_fails_closed() -> None:
    resolver = ApplicationRuntime.create_default().provider_resolver

    with pytest.raises(ProviderAssetContextError):
        resolve_coinbase_intraday_configuration(resolver, asset_id="crypto:eth-usd")
    with pytest.raises((ProviderAssetContextError, AssetCatalogError)):
        resolve_coinbase_intraday_configuration(resolver, asset_id="crypto:unknown:asset")

    configuration = resolve_coinbase_intraday_configuration(
        resolver,
        asset_id="crypto:btc-usd",
    )
    assert configuration.asset_id == "crypto:btc-usd"
    assert configuration.source_id == "coinbase-exchange:btc-usd:minute-1-candles"


def test_missing_or_unknown_asset_id_fails_closed_on_every_crypto_read_and_refresh_path() -> None:
    known_at = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
    resolver = ApplicationRuntime.create_default().provider_resolver

    with pytest.raises(ValidationError, match="asset_id"):
        CryptoSpotIntradayChartRequest(  # type: ignore[call-arg]
            known_at=known_at,
            interval=IntradayInterval.MINUTE_5,
        )
    with pytest.raises(ValidationError, match="asset_id"):
        CryptoSpotIntradayRefreshRequest(  # type: ignore[call-arg]
            requested_end=known_at,
        )
    with pytest.raises(ValidationError, match="asset_id"):
        CryptoSpotIntradayRefreshRequest(asset_id="")  # type: ignore[arg-type]
    with pytest.raises((ProviderAssetContextError, AssetCatalogError)):
        resolve_coinbase_intraday_configuration(resolver, asset_id="crypto:unknown:asset")
    with pytest.raises(ProviderAssetContextError):
        resolve_coinbase_intraday_configuration(resolver, asset_id="crypto:eth-usd")


def test_intraday_requests_and_summaries_reject_missing_or_foreign_identity() -> None:
    known_at = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="asset_id"):
        CryptoSpotIntradayChartRequest(  # type: ignore[call-arg]
            known_at=known_at,
            interval=IntradayInterval.MINUTE_5,
        )
    with pytest.raises(ValidationError, match="asset_id"):
        CryptoSpotIntradayRefreshRequest(asset_id="")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="minute-candle source"):
        CryptoSpotIntradayRefreshSummary(
            asset_id="crypto:btc-usd",
            source_id="alpaca-market-data:iex:btc:daily-bars:adjustment-all",
            requested_start=known_at - timedelta(hours=24),
            requested_end=known_at,
            retrieved_at=known_at,
            request_count=1,
            candles_received=0,
            raw_records_created=0,
            raw_records_reused=0,
            observations_created=0,
            observations_reused=0,
            missing_intervals=(),
        )


def test_intraday_chart_preserves_point_in_time_decimal_and_deterministic_identity() -> None:
    known_at = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
    source = "coinbase-exchange:btc-usd:minute-1-candles"
    request = _request("crypto:btc-usd", known_at)
    series = _series(asset_id="crypto:btc-usd", source_id=source, known_at=known_at)

    first = CryptoSpotIntradayChart.from_series(request, series)
    second = CryptoSpotIntradayChart.from_series(request, series)

    assert first == second
    assert first.to_json_dict() == second.to_json_dict()
    assert first.model_dump()["bars"][0]["volume"] == Decimal("1.25")
    assert all(bar.available_at <= first.known_at for bar in first.bars)

    late_bar = _bar(
        asset_id="crypto:btc-usd",
        source_id=source,
        period_start=known_at - timedelta(hours=24),
        available_at=known_at + timedelta(minutes=1),
    )
    with pytest.raises(ValidationError, match="outside the requested scope"):
        CryptoSpotIntradayChart(
            asset_id="crypto:btc-usd",
            source_id=source,
            known_at=known_at,
            start=known_at - timedelta(hours=24),
            end=known_at,
            interval=IntradayInterval.MINUTE_5,
            bars=(late_bar,),
            source_bar_count=_EVIDENCE_SIZE,
            complete_interval_count=1,
            incomplete_interval_count=0,
        )
