"""Longitudinal and bounded-scope integration tests for daily market refreshes."""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import MarketDiagnosticMetricSelector
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.application.crypto_spot_daily import (
    CryptoSpotDailyRefreshPipeline,
)
from investment_analyst.application.crypto_spot_daily_models import (
    CryptoSpotDailyRefreshRequest,
    CryptoSpotDailyRefreshSummary,
)
from investment_analyst.application.crypto_spot_daily_planner import CryptoSpotDailyRefreshPlanner
from investment_analyst.application.listed_market_refresh import (
    ListedMarketKnownAtTooEarlyError,
    ListedMarketRefreshError,
    ListedMarketRefreshPipeline,
)
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
    ListedMarketRefreshSummary,
)
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.catalog.provider_configuration import resolve_coinbase_configuration
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_ALPACA_CONFIG = AlpacaAssetConfiguration(
    asset_id="equity:us:bvn",
    symbol="BVN",
    feed="iex",
    adjustment="all",
    source_id="alpaca-market-data:iex:bvn:daily-bars:adjustment-all",
    name="Compañía de Minas Buenaventura S.A.A.",
    asset_class=AssetClass.EQUITY,
    quote_currency="USD",
    exchange="NYSE",
)


class SyntheticAlpacaTransport:
    """Deterministic synthetic transport answering daily Alpaca bar requests."""

    def __init__(
        self,
        *,
        fail_page_token: str | None = None,
        max_available_date: date | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.fail_page_token = fail_page_token
        self.max_available_date = max_available_date

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)

        page_token = params.get("page_token", [None])[0]
        if self.fail_page_token is not None and page_token == self.fail_page_token:
            return HttpResponse(
                status_code=500,
                body=b'{"message": "internal error"}',
                headers={},
                url=url,
            )

        start = datetime.fromisoformat(params["start"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(params["end"][0].replace("Z", "+00:00"))

        bars: list[dict[str, object]] = []
        cursor = start
        offset = 0
        while cursor < end:
            if self.max_available_date is None or cursor.date() <= self.max_available_date:
                price = float(Decimal("100.00") + Decimal(str(offset % 20)))
                bars.append(
                    {
                        "t": cursor.isoformat().replace("+00:00", "Z"),
                        "o": price,
                        "h": price + 2.0,
                        "l": price - 1.5,
                        "c": price + 0.5,
                        "v": 250000 + (offset * 100),
                        "n": 1200 + offset,
                        "vw": price + 0.25,
                    }
                )
            cursor += timedelta(days=1)
            offset += 1

        path_parts = parsed.path.strip("/").split("/")
        symbol = path_parts[2] if len(path_parts) > 2 else "BVN"
        next_token = "page_2" if (self.fail_page_token is not None and page_token is None) else None
        payload = {"bars": bars, "symbol": symbol, "next_page_token": next_token}
        return HttpResponse(
            status_code=200,
            body=json.dumps(payload).encode("utf-8"),
            headers={},
            url=url,
        )


class SyntheticCoinbaseTransport:
    """Deterministic synthetic transport answering daily Coinbase candle requests."""

    def __init__(
        self,
        *,
        fail_url_fragment: str | None = None,
        max_available_date: date | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.fail_url_fragment = fail_url_fragment
        self.max_available_date = max_available_date

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        if self.fail_url_fragment and self.fail_url_fragment in url:
            return HttpResponse(status_code=500, body=b'{"message": "failed"}', headers={}, url=url)

        parts = urlsplit(url)
        query = parse_qs(parts.query)
        start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(query["end"][0].replace("Z", "+00:00"))

        candles: list[list[float | int]] = []
        cursor = start
        offset = 0
        while cursor < end:
            if self.max_available_date is None or cursor.date() <= self.max_available_date:
                base = 90_000.0 + float(offset % 100)
                candles.append(
                    [int(cursor.timestamp()), base - 50.0, base + 100.0, base, base + 25.0, 150.5]
                )
            cursor += timedelta(days=1)
            offset += 1

        return HttpResponse(
            status_code=200,
            body=json.dumps(candles).encode("utf-8"),
            headers={},
            url=url,
        )


def _build_alpaca_pipeline(
    storage: LocalStorage,
    transport: SyntheticAlpacaTransport,
    *,
    clock: Callable[[], datetime],
    query_spy: list[HistoricalBarQuery] | None = None,
) -> ListedMarketRefreshPipeline:
    history = HistoricalMarketDataService(storage)
    stats_pipeline = MarketStatisticsPipeline(
        storage,
        history,
        MarketStatisticsEngine(),
        clock=clock,
    )
    diag_pipeline = MarketDiagnosticPipeline(
        storage,
        MarketDiagnosticMetricSelector(storage),
        MarketDiagnosticEngine(),
        clock=clock,
    )
    if query_spy is not None:
        orig_stats_run = stats_pipeline.run

        def spied_stats_run(request: MarketStatisticsRequest):
            query_spy.append(request.query)
            return orig_stats_run(request)

        stats_pipeline.run = spied_stats_run

    return ListedMarketRefreshPipeline(
        configuration=_ALPACA_CONFIG,
        refresh_planner=AaplMarketRefreshPlanner(_ALPACA_CONFIG, storage),
        market_pipeline=AlpacaHistoricalPipeline(
            storage,
            AlpacaStockClient(
                transport,
                AlpacaCredentials("simulated-key", "simulated-secret"),
                clock=clock,
            ),
            configuration=_ALPACA_CONFIG,
            clock=clock,
        ),
        statistics_pipeline=stats_pipeline,
        diagnostic_pipeline=diag_pipeline,
        clock=clock,
    )


def _build_coinbase_pipeline(
    storage: LocalStorage,
    transport: SyntheticCoinbaseTransport,
    *,
    clock: Callable[[], datetime],
    query_spy: list[HistoricalBarQuery] | None = None,
) -> CryptoSpotDailyRefreshPipeline:
    config = resolve_coinbase_configuration(
        ApplicationRuntime.create_default().provider_resolver,
        asset_id="crypto:btc-usd",
    )
    history = HistoricalMarketDataService(storage)
    stats_pipeline = MarketStatisticsPipeline(
        storage,
        history,
        MarketStatisticsEngine(),
        clock=clock,
    )
    diag_pipeline = MarketDiagnosticPipeline(
        storage,
        MarketDiagnosticMetricSelector(storage),
        MarketDiagnosticEngine(),
        clock=clock,
    )
    if query_spy is not None:
        orig_stats_run = stats_pipeline.run

        def spied_stats_run(request: MarketStatisticsRequest):
            query_spy.append(request.query)
            return orig_stats_run(request)

        stats_pipeline.run = spied_stats_run

    return CryptoSpotDailyRefreshPipeline(
        asset_id=config.asset_id,
        source_id=config.source_id,
        refresh_planner=CryptoSpotDailyRefreshPlanner(
            storage,
            asset_id=config.asset_id,
            source_id=config.source_id,
        ),
        market_pipeline=CoinbaseHistoricalPipeline(
            storage,
            CoinbaseExchangeClient(
                transport,
                sleep=lambda _: None,
                clock=clock,
            ),
            configuration=config,
            clock=clock,
        ),
        statistics_pipeline=stats_pipeline,
        diagnostic_pipeline=diag_pipeline,
        clock=clock,
    )


def test_long_history_refresh_keeps_operational_scope(tmp_path: Path) -> None:
    # 636 days range: 2025-01-01 to 2026-09-28
    market_start = date(2025, 1, 1)
    market_end = date(2026, 9, 28)
    sim_clock = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

    # 1. Alpaca flow with >600 days
    alpaca_transport = SyntheticAlpacaTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path / "alpaca")) as storage:
        pipeline = _build_alpaca_pipeline(storage, alpaca_transport, clock=lambda: sim_clock)
        request = ListedMarketRefreshRequest(
            asset_id=_ALPACA_CONFIG.asset_id,
            market_start=market_start,
            market_end=market_end,
        )
        summary = pipeline.run(request)

        # Operational window must be strictly 90 days, NOT 636 days!
        expected_end = datetime(2026, 9, 29, 0, 0, tzinfo=UTC)
        expected_start = expected_end - timedelta(days=90)  # 2026-07-01
        assert summary.analytics_end == expected_end
        assert summary.analytics_start == expected_start
        assert summary.analytics_end - summary.analytics_start == timedelta(days=90)
        assert summary.metric_results_created > 0
        assert summary.diagnostics_created == 1
        assert summary.traceability_verified is True

        # Total observations in storage cover the full requested range
        all_obs = storage.observations.list(asset_id=_ALPACA_CONFIG.asset_id)
        assert len(all_obs) > 600 * 7

    # 2. Coinbase flow with >600 days
    coinbase_transport = SyntheticCoinbaseTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path / "coinbase")) as storage:
        pipeline = _build_coinbase_pipeline(storage, coinbase_transport, clock=lambda: sim_clock)
        request = CryptoSpotDailyRefreshRequest(
            asset_id="crypto:btc-usd",
            market_start=market_start,
            market_end=market_end,
        )
        summary = pipeline.run(request)

        assert summary.analytics_end == expected_end
        assert summary.analytics_start == expected_start
        assert summary.analytics_end - summary.analytics_start == timedelta(days=90)
        assert summary.metric_results_created > 0
        assert summary.diagnostics_created == 1
        assert summary.traceability_verified is True

        all_obs = storage.observations.list(asset_id="crypto:btc-usd")
        assert len(all_obs) > 600 * 5


def test_summary_matches_executed_queries(tmp_path: Path) -> None:
    # Requested end is 2026-07-11 (end=2026-07-12T00:00:00Z)
    # Available data stops at 2026-07-09 (projected end=2026-07-10T00:00:00Z)
    req_start = date(2026, 7, 1)
    req_end = date(2026, 7, 11)
    max_data_date = date(2026, 7, 9)
    sim_clock = datetime(2026, 7, 12, 14, 0, tzinfo=UTC)

    # 1. Test Alpaca
    alpaca_queries: list[HistoricalBarQuery] = []
    alpaca_transport = SyntheticAlpacaTransport(max_available_date=max_data_date)
    with LocalStorage(StoragePaths.from_root(tmp_path / "alpaca_match")) as storage:
        pipeline = _build_alpaca_pipeline(
            storage,
            alpaca_transport,
            clock=lambda: sim_clock,
            query_spy=alpaca_queries,
        )
        request = ListedMarketRefreshRequest(
            asset_id=_ALPACA_CONFIG.asset_id,
            market_start=req_start,
            market_end=req_end,
        )
        # First run ingests data up to 2026-07-09
        first = pipeline.run(request)
        assert first.bars_received > 0

        # Second run without new inputs projects cut
        alpaca_queries.clear()
        summary = pipeline.run(request)

        # Summary analytics_end must match projected end (2026-07-10), not requested (2026-07-12)
        assert summary.analytics_end == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
        assert summary.analytics_end < datetime(2026, 7, 12, 0, 0, tzinfo=UTC)

        assert len(alpaca_queries) >= 1
        executed_query = alpaca_queries[0]
        assert executed_query.start == summary.analytics_start
        assert executed_query.end == summary.analytics_end
        assert executed_query.known_at == summary.analytics_known_at

        # Compare results directly with pure engine execution on the same query
        history = HistoricalMarketDataService(storage)
        direct_stats = MarketStatisticsPipeline(
            storage, history, MarketStatisticsEngine(), clock=lambda: sim_clock
        ).run(MarketStatisticsRequest(query=executed_query))
        assert direct_stats.latest_as_of == summary.market_as_of

        # Serialization roundtrip verification
        roundtripped = ListedMarketRefreshSummary.model_validate(summary.model_dump())
        assert roundtripped == summary

    # 2. Test Coinbase
    coinbase_queries: list[HistoricalBarQuery] = []
    coinbase_transport = SyntheticCoinbaseTransport(max_available_date=max_data_date)
    with LocalStorage(StoragePaths.from_root(tmp_path / "coinbase_match")) as storage:
        pipeline = _build_coinbase_pipeline(
            storage,
            coinbase_transport,
            clock=lambda: sim_clock,
            query_spy=coinbase_queries,
        )
        request = CryptoSpotDailyRefreshRequest(
            asset_id="crypto:btc-usd",
            market_start=req_start,
            market_end=req_end,
        )
        first_cb = pipeline.run(request)
        assert first_cb.candles_received > 0

        coinbase_queries.clear()
        summary = pipeline.run(request)

        assert summary.analytics_end == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
        assert summary.analytics_end < datetime(2026, 7, 12, 0, 0, tzinfo=UTC)

        assert len(coinbase_queries) >= 1
        executed_query = coinbase_queries[0]
        assert executed_query.start == summary.analytics_start
        assert executed_query.end == summary.analytics_end
        assert executed_query.known_at == summary.analytics_known_at

        history = HistoricalMarketDataService(storage)
        direct_stats = MarketStatisticsPipeline(
            storage, history, MarketStatisticsEngine(), clock=lambda: sim_clock
        ).run(MarketStatisticsRequest(query=executed_query))
        assert direct_stats.latest_as_of == summary.market_as_of

        roundtripped = CryptoSpotDailyRefreshSummary.model_validate(summary.model_dump())
        assert roundtripped == summary


def test_empty_daily_rerun_and_revision_preserve_pit(tmp_path: Path) -> None:
    # Bootstrap -> new bar -> rerun with different clock -> empty extension -> later revision
    base_date = date(2026, 7, 5)
    end_date = date(2026, 7, 8)
    boot_clock = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)

    transport = SyntheticCoinbaseTransport(max_available_date=end_date)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        current_clock = boot_clock
        pipeline = _build_coinbase_pipeline(storage, transport, clock=lambda: current_clock)

        # 1. Bootstrap
        boot_req = CryptoSpotDailyRefreshRequest(
            asset_id="crypto:btc-usd",
            market_start=base_date,
            market_end=end_date,
        )
        first = pipeline.run(boot_req)
        assert first.metric_results_created > 0
        assert first.diagnostics_created == 1
        first_metric_ids = {m.result_id for m in storage.metric_results.list()}
        assert first.market_as_of is not None

        # 2. Add new bar (2026-07-09)
        transport.max_available_date = date(2026, 7, 9)
        new_bar_clock = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
        current_clock = new_bar_clock
        second_req = CryptoSpotDailyRefreshRequest(
            asset_id="crypto:btc-usd",
            market_start=base_date,
            market_end=date(2026, 7, 9),
        )
        second = pipeline.run(second_req)
        assert second.candles_received == 1
        assert second.metric_results_created > 0
        assert first_metric_ids.issubset({m.result_id for m in storage.metric_results.list()})
        second_metric_ids = {m.result_id for m in storage.metric_results.list()}
        second_diag_ids = {d.diagnostic_id for d in storage.diagnostics.list()}

        # 3. Rerun with different clock -> 0 created, preserved IDs
        later_clock = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
        current_clock = later_clock
        third = pipeline.run(second_req)
        assert third.candles_received == 0
        assert third.metric_results_created == 0
        assert third.diagnostics_created == 0
        assert third.metric_results_reused == len(second_metric_ids)
        assert third.diagnostics_reused == 1
        assert {m.result_id for m in storage.metric_results.list()} == second_metric_ids
        assert {d.diagnostic_id for d in storage.diagnostics.list()} == second_diag_ids

        # 4. Extension with empty response
        ext_clock = datetime(2026, 7, 12, 10, 0, tzinfo=UTC)
        current_clock = ext_clock
        ext_req = CryptoSpotDailyRefreshRequest(
            asset_id="crypto:btc-usd",
            market_start=base_date,
            market_end=date(2026, 7, 11),
        )
        fourth = pipeline.run(ext_req)
        assert fourth.candles_received == 0
        assert fourth.observations_created == 0
        # market_as_of remains unchanged from second run
        assert fourth.market_as_of == second.market_as_of

        # 5. Later revision available at later time
        history = HistoricalMarketDataService(storage)
        early_query = HistoricalBarQuery(
            asset_id="crypto:btc-usd",
            source_id="coinbase-exchange:btc-usd:daily-candles",
            start=datetime(2026, 7, 5, 0, 0, tzinfo=UTC),
            end=datetime(2026, 7, 9, 0, 0, tzinfo=UTC),
            known_at=boot_clock,
        )
        early_series = history.query(early_query)
        assert len(early_series.bars) == 4

        # Add later revision for 2026-07-06 with availability at revision_clock
        revision_clock = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
        rev_obs = storage.observations.list(asset_id="crypto:btc-usd")[0].model_copy(
            update={
                "observation_id": uuid4(),
                "available_at": revision_clock,
                "value": Decimal("99999.0"),
            }
        )
        storage.observations.save(rev_obs)

        # Query with boot_clock still sees original revision
        pit_series = history.query(early_query)
        assert pit_series == early_series


def test_failed_fetch_and_future_cut_preserve_confirmed_progress(tmp_path: Path) -> None:
    clock = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    # 1. Incomplete paginated fetch does NOT emit coverage receipt but preserves confirmed progress
    transport = SyntheticAlpacaTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path / "partial")) as storage:
        pipeline = _build_alpaca_pipeline(storage, transport, clock=lambda: clock)
        # Step 1: Successful initial fetch for 2026-07-06 to 2026-07-07
        boot_req = ListedMarketRefreshRequest(
            asset_id=_ALPACA_CONFIG.asset_id,
            market_start=date(2026, 7, 6),
            market_end=date(2026, 7, 7),
        )
        boot_summary = pipeline.run(boot_req)
        assert boot_summary.bars_received > 0
        initial_raw_count = len(storage.raw_records.list())
        assert initial_raw_count > 0
        receipts_before = [
            r
            for r in storage.raw_records.list()
            if r.schema_version == "alpaca-market-fetch-receipt-v1"
        ]
        assert len(receipts_before) == 1

        # Step 2: Next fetch has page 2 failure (paginated fetch incomplete)
        transport.fail_page_token = "page_2"
        second_req = ListedMarketRefreshRequest(
            asset_id=_ALPACA_CONFIG.asset_id,
            market_start=date(2026, 7, 6),
            market_end=date(2026, 7, 9),
        )
        with pytest.raises(ListedMarketRefreshError):
            pipeline.run(second_req)

        # Incomplete paginated fetch did NOT emit coverage receipt for the incomplete interval
        receipts_after = [
            r
            for r in storage.raw_records.list()
            if r.schema_version == "alpaca-market-fetch-receipt-v1"
        ]
        assert len(receipts_after) == 1
        assert receipts_after == receipts_before
        # Confirmed progress from earlier successful interval is preserved
        assert len(storage.raw_records.list()) >= initial_raw_count
        assert len(storage.observations.list()) > 0

    # 2. Explicit cut earlier than newly fetched evidence fails closed and preserves progress
    valid_transport = SyntheticAlpacaTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path / "early_cut")) as storage:
        pipeline = _build_alpaca_pipeline(storage, valid_transport, clock=lambda: clock)
        # Fetch evidence available at 2026-07-10 12:00 UTC
        # Request with requested_known_at = 2026-07-09 (predates evidence)
        too_early_cut = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
        request = ListedMarketRefreshRequest(
            asset_id=_ALPACA_CONFIG.asset_id,
            market_start=date(2026, 7, 7),
            market_end=date(2026, 7, 9),
            requested_known_at=too_early_cut,
        )
        with pytest.raises(ListedMarketKnownAtTooEarlyError, match="predates newly fetched"):
            pipeline.run(request)

        # Confirmed raw records and observations are preserved in storage!
        assert len(storage.raw_records.list()) > 0
        assert len(storage.observations.list()) > 0
        # But zero metrics or diagnostics were created for this rejected cut
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []
