"""Integration tests verifying pipelines query existing metrics strictly by identity."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.analytics.crypto.derivatives_engine import CryptoDerivativesMetricEngine
from investment_analyst.analytics.crypto.derivatives_pipeline import (
    CryptoDerivativesMetricPipeline,
)
from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import COINBASE_SOURCE_ID
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.catalog.provider_configuration import resolve_deribit_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import DataQuality, MetricResult
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
)
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_pipeline import DeribitEvidencePipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.repositories import DuckDBMetricResultRepository

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "deribit"
_DERIBIT_START = datetime(2026, 8, 1, tzinfo=UTC)
_DERIBIT_END = datetime(2026, 8, 3, tzinfo=UTC)
_DERIBIT_KNOWN = datetime(2026, 8, 5, tzinfo=UTC)


class _Transport:
    def __init__(self, *bodies: bytes) -> None:
        self._bodies = list(bodies)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        del headers, timeout_seconds, max_response_bytes
        return HttpResponse(
            status_code=200,
            body=self._bodies.pop(0),
            headers={},
            url=url,
        )


def _store_coinbase_bars(storage: LocalStorage, count: int = 4) -> tuple[datetime, datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        retrieved = timestamp + timedelta(hours=1)
        close = Decimal("100") + Decimal(index * 2)
        candle = CoinbaseCandle(
            product_id="BTC-USD",
            start=timestamp,
            low=close - 2,
            high=close + 2,
            open=close - 1,
            close=close,
            volume=Decimal("100") + Decimal(index * 10),
            raw_values=(
                str(int(timestamp.timestamp())),
                str(close - 2),
                str(close + 2),
                str(close - 1),
                str(close),
                str(Decimal("100") + Decimal(index * 10)),
            ),
        )
        raw = candle_to_raw_record(
            candle,
            retrieved_at=retrieved,
            request_url="https://api.exchange.coinbase.com/test",
        )
        storage.raw_records.save(raw)
        for observation in candle_to_observations(
            candle,
            raw,
            normalized_at=retrieved + timedelta(minutes=1),
        ):
            storage.observations.save(observation)
    return start, start + timedelta(days=count)


def test_statistics_pipeline_reads_existing_rows_by_identity_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A1: statistics_pipeline reads existing rows by identity and never queries another family."""
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    asset_id = "crypto:btc-usd"

    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        start, end = _store_coinbase_bars(storage)

        # Seed an unrelated metric from another family for the same asset
        unrelated_metric = MetricResult(
            result_id=UUID("80000000-0000-4000-8000-000000000999"),
            asset_id=asset_id,
            metric_key="crypto.derivatives.funding_rate",
            value=Decimal("0.0001"),
            unit="ratio",
            as_of=start,
            available_at=start + timedelta(hours=1),
            computed_at=start + timedelta(hours=1),
            parameters={"source_id": "deribit:btc-perpetual", "kind": "unrelated"},
            input_observation_ids=[UUID("71000000-0000-4000-8000-000000000999")],
            algorithm_version="deribit-funding-v1",
            quality=DataQuality.VALID,
        )
        storage.metric_results.save(unrelated_metric)

        history = HistoricalMarketDataService(storage)
        pipeline = MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        request = MarketStatisticsRequest(
            query=HistoricalBarQuery(
                asset_id=asset_id,
                source_id=COINBASE_SOURCE_ID,
                start=start,
                end=end,
                known_at=fixed_clock,
            ),
            sma_windows=(2,),
            volatility_window=2,
            relative_volume_window=2,
            ema_windows=(2,),
        )

        list_calls: list[dict[str, object]] = []
        original_list = DuckDBMetricResultRepository.list

        def tracked_list(self_repo: DuckDBMetricResultRepository, **kwargs: object):
            list_calls.append(kwargs)
            return original_list(self_repo, **kwargs)

        monkeypatch.setattr(DuckDBMetricResultRepository, "list", tracked_list)

        first_run = pipeline.run(request)
        assert first_run.results_created == first_run.results_generated > 0
        assert first_run.results_reused == 0

        # Verify that metric_results.list was NOT called during pipeline run
        assert len(list_calls) == 0

        # Second run should reuse all results by identity without listing
        second_run = pipeline.run(request)
        assert second_run.results_created == 0
        assert second_run.results_reused == second_run.results_generated
        assert len(list_calls) == 0

        # Reused results are identical to first run
        first_results = storage.metric_results.get_many(
            {
                r.result_id
                for r in storage.metric_results.list(asset_id=asset_id)
                if r.metric_key != "crypto.derivatives.funding_rate"
            }
        )
        assert len(first_results) == first_run.results_generated


def test_derivatives_pipeline_reads_existing_rows_by_identity_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2: derivatives_pipeline reads existing rows by identity and reuses them identically."""
    configuration = resolve_deribit_configuration(
        ProviderAssetContextResolver(AssetCatalogService.load_default()),
        asset_id="crypto:btc-usd",
    )

    with LocalStorage(StoragePaths.from_root(tmp_path / "storage")) as storage:
        evidence = DeribitEvidencePipeline(
            storage,
            DeribitClient(
                _Transport(
                    (_FIXTURES / "btc_funding_history.json").read_bytes(),
                    (_FIXTURES / "btc_dvol_daily.json").read_bytes(),
                    (_FIXTURES / "btc_perpetual_summary.json").read_bytes(),
                ),
                sleep=lambda _: None,
                clock=lambda: datetime(2026, 8, 4, tzinfo=UTC),
            ),
            configuration=configuration,
            clock=lambda: datetime(2026, 8, 4, tzinfo=UTC),
        )
        evidence.import_funding(_DERIBIT_START, _DERIBIT_END)
        evidence.import_dvol(_DERIBIT_START, _DERIBIT_END)
        evidence.capture_summary()

        # Seed an unrelated metric for the same asset
        unrelated_metric = MetricResult(
            result_id=UUID("80000000-0000-4000-8000-000000000888"),
            asset_id=configuration.asset_id,
            metric_key="market.history.sma",
            value=Decimal("50000"),
            unit="USD",
            as_of=_DERIBIT_START,
            available_at=_DERIBIT_START + timedelta(hours=1),
            computed_at=_DERIBIT_START + timedelta(hours=1),
            parameters={"source_id": "coinbase:btc-usd", "window": 20},
            input_observation_ids=[UUID("71000000-0000-4000-8000-000000000888")],
            algorithm_version="market-sma-v1",
            quality=DataQuality.VALID,
        )
        storage.metric_results.save(unrelated_metric)

        list_calls: list[dict[str, object]] = []
        original_list = DuckDBMetricResultRepository.list

        def tracked_list(self_repo: DuckDBMetricResultRepository, **kwargs: object):
            list_calls.append(kwargs)
            return original_list(self_repo, **kwargs)

        monkeypatch.setattr(DuckDBMetricResultRepository, "list", tracked_list)

        pipeline = CryptoDerivativesMetricPipeline(
            storage,
            CryptoDerivativesMetricEngine(),
            clock=lambda: _DERIBIT_KNOWN,
        )
        first = pipeline.run(
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_DERIBIT_KNOWN,
            as_of_from=_DERIBIT_START,
            as_of_before=_DERIBIT_END,
        )
        assert first.results_created == len(first.results) > 0
        assert first.results_reused == 0
        assert len(list_calls) == 0

        repeated = CryptoDerivativesMetricPipeline(
            storage,
            CryptoDerivativesMetricEngine(),
            clock=lambda: _DERIBIT_KNOWN + timedelta(days=1),
        ).run(
            asset_id=configuration.asset_id,
            funding_source_id=configuration.funding_source_id,
            dvol_source_id=configuration.dvol_source_id,
            summary_source_id=configuration.summary_source_id,
            known_at=_DERIBIT_KNOWN,
            as_of_from=_DERIBIT_START,
            as_of_before=_DERIBIT_END,
        )

        assert repeated.results_created == 0
        assert repeated.results_reused == len(first.results)
        assert repeated.results == first.results
        assert len(list_calls) == 0
        assert repeated.traceability_verified
