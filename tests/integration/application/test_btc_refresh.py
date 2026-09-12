"""Offline integration tests for the generic Coinbase daily market refresh."""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.btc_refresh import BtcMarketExecutionClock
from investment_analyst.application.btc_refresh_models import BtcMarketRefreshMode
from investment_analyst.application.crypto_spot_daily import (
    CryptoSpotDailyKnownAtTooEarlyError,
    CryptoSpotDailyRefreshPipeline,
)
from investment_analyst.application.crypto_spot_daily_models import (
    CryptoSpotDailyRefreshRequest,
)
from investment_analyst.application.crypto_spot_daily_planner import CryptoSpotDailyRefreshPlanner
from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.catalog.provider_configuration import resolve_coinbase_configuration
from investment_analyst.providers.asset_config import CoinbaseAssetConfiguration
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

FIXTURE_PATH = Path("tests/fixtures/coinbase/btc_usd_daily.json")
FETCHED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
RUN_AT = datetime(2026, 7, 12, 12, 5, tzinfo=UTC)
_PRODUCT_BASE_PRICES = {"BTC-USD": 100_000, "ETH-USD": 3_000, "SOL-USD": 150}


class FixtureTransport:
    """Return one deterministic Coinbase fixture and record URLs."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(
            status_code=200,
            body=FIXTURE_PATH.read_bytes(),
            headers={},
            url=url,
        )


class SyntheticTransport:
    """Answer any product request with deterministic daily candles for its window."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del headers, timeout_seconds
        self.calls.append(url)
        parts = urlsplit(url)
        product = unquote(parts.path.rsplit("/products/", 1)[1].split("/", 1)[0])
        query = parse_qs(parts.query)
        start = datetime.fromisoformat(query["start"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(query["end"][0].replace("Z", "+00:00"))
        base = _PRODUCT_BASE_PRICES[product]
        candles: list[list[float | int]] = []
        cursor = start
        offset = 0
        while cursor < end:
            low = float(base + offset)
            candles.append(
                [int(cursor.timestamp()), low, low + 10, low + 2, low + 5, 100.5],
            )
            cursor += timedelta(days=1)
            offset += 1
        return HttpResponse(
            status_code=200,
            body=json.dumps(candles).encode("utf-8"),
            headers={},
            url=url,
        )


def _configuration(asset_id: str) -> CoinbaseAssetConfiguration:
    return resolve_coinbase_configuration(
        ApplicationRuntime.create_default().provider_resolver,
        asset_id=asset_id,
    )


def _pipeline(
    storage: LocalStorage,
    transport: FixtureTransport | SyntheticTransport,
    *,
    configuration: CoinbaseAssetConfiguration,
    execution_clock: Callable[[], datetime] | None = None,
) -> CryptoSpotDailyRefreshPipeline:
    history = HistoricalMarketDataService(storage)
    fetch_clock = execution_clock or (lambda: FETCHED_AT)
    analytics_clock = execution_clock or (lambda: RUN_AT)
    return CryptoSpotDailyRefreshPipeline(
        asset_id=configuration.asset_id,
        source_id=configuration.source_id,
        refresh_planner=CryptoSpotDailyRefreshPlanner(
            storage,
            asset_id=configuration.asset_id,
            source_id=configuration.source_id,
        ),
        market_pipeline=CoinbaseHistoricalPipeline(
            storage,
            CoinbaseExchangeClient(
                transport,
                sleep=lambda _: None,
                clock=fetch_clock,
            ),
            configuration=configuration,
            clock=fetch_clock,
        ),
        statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=analytics_clock,
        ),
        diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=analytics_clock,
        ),
        clock=analytics_clock,
    )


def _request(
    asset_id: str = "crypto:btc-usd",
    *,
    known_at: datetime | None = None,
) -> CryptoSpotDailyRefreshRequest:
    return CryptoSpotDailyRefreshRequest(
        asset_id=asset_id,
        market_start=date(2026, 7, 9),
        market_end=date(2026, 7, 11),
        requested_known_at=known_at,
    )


def test_refresh_ingests_calculates_and_reruns_without_provider_call(tmp_path: Path) -> None:
    transport = FixtureTransport()
    configuration = _configuration("crypto:btc-usd")
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport, configuration=configuration)
        first = pipeline.run(_request())
        second = pipeline.run(_request())

        assert first.schema_version == "crypto-spot-daily-market-refresh-v1"
        assert first.asset_id == ASSET_ID
        assert first.source_id == SOURCE_ID
        assert first.refresh_plan.mode is BtcMarketRefreshMode.INITIAL
        assert first.intervals_executed == 1
        assert first.candles_received == 3
        assert first.raw_records_created == 3
        assert first.observations_created == 15
        assert first.metric_results_created > 0
        assert first.diagnostics_created == 1
        assert first.market_as_of == datetime(2026, 7, 11, tzinfo=UTC)
        assert first.analytics_start == datetime(2026, 7, 9, tzinfo=UTC)
        assert first.analytics_end == datetime(2026, 7, 12, tzinfo=UTC)
        assert first.analytics_lookback_days == 90
        assert first.traceability_verified is True

        assert second.refresh_plan.mode is BtcMarketRefreshMode.ALREADY_CURRENT
        assert second.effective_known_at == first.effective_known_at == FETCHED_AT
        assert second.intervals_executed == 0
        assert second.candles_received == 0
        assert second.metric_results_created == 0
        assert second.metric_results_reused == first.metric_results_created
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1
        assert len(transport.calls) == 1
        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 3
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 15


def test_explicit_cut_before_new_fetch_preserves_ingested_progress(tmp_path: Path) -> None:
    transport = FixtureTransport()
    configuration = _configuration("crypto:btc-usd")
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport, configuration=configuration)

        with pytest.raises(CryptoSpotDailyKnownAtTooEarlyError, match="predates newly fetched"):
            pipeline.run(_request(known_at=datetime(2026, 7, 12, 11, 59, tzinfo=UTC)))

        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 3
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 15
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []


def test_refresh_preserves_point_in_time_when_wall_clock_regresses(tmp_path: Path) -> None:
    high_watermark = RUN_AT + timedelta(microseconds=2)
    regressed = RUN_AT - timedelta(hours=1)
    wall_times = iter((RUN_AT, RUN_AT, high_watermark, regressed, regressed, regressed))
    execution_clock = BtcMarketExecutionClock(lambda: next(wall_times, regressed))
    transport = FixtureTransport()
    configuration = _configuration("crypto:btc-usd")

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        result = _pipeline(
            storage,
            transport,
            configuration=configuration,
            execution_clock=execution_clock,
        ).run(_request())

        assert result.effective_known_at == high_watermark
        assert result.traceability_verified is True
        assert result.metric_results_created > 0
        assert result.diagnostics_created == 1
        assert all(
            observation.available_at <= result.effective_known_at
            for observation in storage.observations.list(asset_id=ASSET_ID)
        )
        assert all(
            metric.available_at <= metric.computed_at
            for metric in storage.metric_results.list(asset_id=ASSET_ID)
        )


def test_already_current_uses_persisted_time_with_a_new_delayed_clock(tmp_path: Path) -> None:
    storage_paths = StoragePaths.from_root(tmp_path)
    configuration = _configuration("crypto:btc-usd")
    first_transport = FixtureTransport()
    first_clock = BtcMarketExecutionClock(lambda: RUN_AT)

    with LocalStorage(storage_paths) as storage:
        first = _pipeline(
            storage,
            first_transport,
            configuration=configuration,
            execution_clock=first_clock,
        ).run(_request())
        first_metric_ids = {item.result_id for item in storage.metric_results.list()}
        first_diagnostic_ids = {item.diagnostic_id for item in storage.diagnostics.list()}

    delayed_clock = BtcMarketExecutionClock(lambda: RUN_AT - timedelta(hours=1))
    second_transport = FixtureTransport()
    with LocalStorage(storage_paths) as storage:
        second = _pipeline(
            storage,
            second_transport,
            configuration=configuration,
            execution_clock=delayed_clock,
        ).run(_request())

        assert second.refresh_plan.mode is BtcMarketRefreshMode.ALREADY_CURRENT
        assert second.effective_known_at == first.effective_known_at == RUN_AT
        assert second.intervals_executed == 0
        assert second.metric_results_created == 0
        assert second.metric_results_reused == first.metric_results_created
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == first.diagnostics_created
        assert second.traceability_verified is True
        assert second_transport.calls == []
        assert {item.result_id for item in storage.metric_results.list()} == first_metric_ids
        assert {item.diagnostic_id for item in storage.diagnostics.list()} == first_diagnostic_ids


def test_btc_market_refresh_v1_is_retired_and_generic_daily_refresh_summary_is_field_equivalent() -> (  # noqa: E501
    None
):
    import investment_analyst.application.btc_refresh_models as refresh_models
    from investment_analyst.application.crypto_spot_daily_models import (
        CryptoSpotDailyRefreshSummary,
    )

    assert not hasattr(refresh_models, "BtcMarketRefreshSummary")
    assert not hasattr(refresh_models, "BtcMarketRefreshRequest")
    assert tuple(CryptoSpotDailyRefreshSummary.model_fields) == (
        "schema_version",
        "asset_id",
        "source_id",
        "request",
        "refresh_plan",
        "effective_known_at",
        "analytics_start",
        "analytics_end",
        "analytics_lookback_days",
        "intervals_executed",
        "candles_received",
        "raw_records_created",
        "raw_records_reused",
        "observations_created",
        "observations_reused",
        "missing_intervals",
        "metric_results_created",
        "metric_results_reused",
        "diagnostics_created",
        "diagnostics_reused",
        "diagnostic_verdict",
        "market_as_of",
        "traceability_verified",
    )
    assert CryptoSpotDailyRefreshSummary.model_fields["schema_version"].default == (
        "crypto-spot-daily-market-refresh-v1"
    )
    assert CryptoSpotDailyRefreshSummary.model_fields["analytics_lookback_days"].default == 90


def test_point_in_time_decimal_deterministic_identity_and_append_only_are_preserved_for_the_crypto_paths(  # noqa: E501
    tmp_path: Path,
) -> None:
    transport = FixtureTransport()
    configuration = _configuration("crypto:btc-usd")

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport, configuration=configuration)
        first = pipeline.run(_request())
        record_ids = {item.record_id for item in storage.raw_records.list(source_id=SOURCE_ID)}
        observations = storage.observations.list(asset_id=ASSET_ID)
        second = pipeline.run(_request())

        assert second.raw_records_created == 0
        assert second.intervals_executed == 0
        assert second.metric_results_reused == first.metric_results_created
        assert {item.record_id for item in storage.raw_records.list(source_id=SOURCE_ID)} == (
            record_ids
        )
        assert len(storage.observations.list(asset_id=ASSET_ID)) == len(observations)
        assert all(item.available_at <= first.effective_known_at for item in observations)
        assert all(isinstance(item.value, Decimal) for item in observations)
        assert {item.unit for item in observations} == {"USD", "BTC"}
        assert first.effective_known_at is not None


@pytest.mark.parametrize(
    ("asset_id", "market_source_id"),
    [
        ("crypto:btc-usd", "coinbase-exchange:btc-usd:daily-candles"),
        ("crypto:eth-usd", "coinbase-exchange:eth-usd:daily-candles"),
        ("crypto:sol-usd", "coinbase-exchange:sol-usd:daily-candles"),
    ],
)
def test_crypto_daily_path_is_proven_across_btc_eth_and_sol_without_per_asset_branching(
    tmp_path: Path,
    asset_id: str,
    market_source_id: str,
) -> None:
    transport = SyntheticTransport()
    configuration = _configuration(asset_id)

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport, configuration=configuration)
        assert type(pipeline) is CryptoSpotDailyRefreshPipeline

        summary = pipeline.run(_request(asset_id))

        assert summary.schema_version == "crypto-spot-daily-market-refresh-v1"
        assert summary.asset_id == asset_id
        assert summary.source_id == market_source_id
        assert summary.refresh_plan.mode is BtcMarketRefreshMode.INITIAL
        assert summary.candles_received == 3
        assert summary.observations_created == 15
        assert summary.diagnostics_created == 1
        assert len(transport.calls) == 1
        assert len(storage.observations.list(asset_id=asset_id)) == 15
        assert storage.observations.list(asset_id=asset_id, source_id=market_source_id)
        assert all(
            item.source.source_id == market_source_id
            for item in storage.observations.list(asset_id=asset_id)
        )
        assert len(storage.raw_records.list(source_id=market_source_id)) == 3

        other_assets = {
            "crypto:btc-usd",
            "crypto:eth-usd",
            "crypto:sol-usd",
        } - {asset_id}
        for other in sorted(other_assets):
            assert storage.observations.list(asset_id=other) == []
            assert storage.metric_results.list(asset_id=other) == []
