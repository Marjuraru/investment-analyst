"""Bounded application services for local BTC-USD intraday data."""

from datetime import UTC, datetime, timedelta

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    MarketHistoryError,
)
from investment_analyst.analytics.market.intraday_models import IntradayAggregationRequest
from investment_analyst.analytics.market.intraday_service import (
    IntradayAggregationError,
    IntradayAggregationService,
)
from investment_analyst.application.btc_intraday_models import (
    BtcIntradayChart,
    BtcIntradayChartRequest,
    BtcIntradayRefreshRequest,
    BtcIntradayRefreshSummary,
)
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeError
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import SOURCE_ID
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseIntradayPipeline
from investment_analyst.storage import LocalStorage, StorageError


class BtcIntradayChartQueryError(RuntimeError):
    """Raised when stored one-minute evidence cannot form a safe chart."""


class BtcIntradayRefreshError(RuntimeError):
    """Raised when an explicit one-minute provider refresh cannot complete."""


def query_btc_intraday_chart(
    storage: LocalStorage,
    request: BtcIntradayChartRequest,
) -> BtcIntradayChart:
    """Reconstruct and aggregate a fixed 24-hour point-in-time window."""
    query = HistoricalBarQuery(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        start=request.query_start,
        end=request.query_end,
        known_at=request.known_at,
    )
    aggregation_request = IntradayAggregationRequest(
        query=query,
        interval=request.interval,
    )
    try:
        minute_series = HistoricalMarketDataService(storage).query(query)
        series = IntradayAggregationService().aggregate(minute_series, aggregation_request)
        return BtcIntradayChart.from_series(request, series)
    except (IntradayAggregationError, MarketHistoryError, StorageError, ValueError) as error:
        raise BtcIntradayChartQueryError(
            "stored BTC-USD minute evidence could not form the requested chart"
        ) from error


def refresh_btc_intraday(
    pipeline: CoinbaseIntradayPipeline,
    request: BtcIntradayRefreshRequest,
    *,
    now: datetime | None = None,
) -> BtcIntradayRefreshSummary:
    """Import exactly the latest requested 24 whole UTC hours append-only."""
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    end = request.requested_end or current
    if end > current:
        raise BtcIntradayRefreshError("requested intraday end must not be in the future")
    start = end - timedelta(hours=request.hours)
    try:
        return BtcIntradayRefreshSummary.from_import(pipeline.run(start, end))
    except (CoinbaseExchangeError, StorageError, ValueError) as error:
        raise BtcIntradayRefreshError("Coinbase intraday refresh did not complete") from error
