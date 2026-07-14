"""Offline compatibility checks for catalog-resolved provider composition."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_normalizer import SOURCE_ID as COINBASE_SOURCE_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.fundamentals.sec_edgar import (
    COMPANY_FACTS_PATH,
    SUBMISSIONS_PATH,
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_pipeline import SecAaplFundamentalsPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_normalizer import SOURCE_ID as ALPACA_SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockClient
from investment_analyst.storage import LocalStorage, StoragePaths

ALPACA_FIXTURE = Path("tests/fixtures/alpaca/aapl_daily.json").read_bytes()
COINBASE_FIXTURE = Path("tests/fixtures/coinbase/btc_usd_daily.json").read_bytes()
SUBMISSIONS_FIXTURE = Path("tests/fixtures/sec/aapl_submissions.json").read_bytes()
COMPANY_FACTS_FIXTURE = Path("tests/fixtures/sec/aapl_companyfacts.json").read_bytes()
FETCHED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 14, 12, 1, tzinfo=UTC)


class FixtureTransport:
    """Return provider fixtures and capture exact URLs and headers."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers)))
        if self.provider == "alpaca":
            body = ALPACA_FIXTURE
        elif self.provider == "coinbase":
            body = COINBASE_FIXTURE
        else:
            body = SUBMISSIONS_FIXTURE if "/submissions/" in url else COMPANY_FACTS_FIXTURE
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def _resolver() -> ProviderAssetContextResolver:
    return ProviderAssetContextResolver(AssetCatalogService.load_default())


def test_catalog_resolved_market_paths_preserve_urls_and_deterministic_ids(tmp_path) -> None:
    alpaca_configuration = resolve_alpaca_configuration(_resolver())
    coinbase_configuration = resolve_coinbase_configuration(_resolver())
    alpaca_transport = FixtureTransport("alpaca")
    coinbase_transport = FixtureTransport("coinbase")
    alpaca_client = AlpacaStockClient(
        alpaca_transport,
        AlpacaCredentials(api_key="key", secret_key="secret"),
        clock=lambda: FETCHED_AT,
    )
    coinbase_client = CoinbaseExchangeClient(
        coinbase_transport,
        sleep=lambda _: None,
        clock=lambda: FETCHED_AT,
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        alpaca = AlpacaHistoricalPipeline(
            storage,
            alpaca_client,
            configuration=alpaca_configuration,
            clock=lambda: NORMALIZED_AT,
        ).run(
            datetime(2026, 7, 7, tzinfo=UTC),
            datetime(2026, 7, 10, tzinfo=UTC),
        )
        coinbase = CoinbaseHistoricalPipeline(
            storage,
            coinbase_client,
            configuration=coinbase_configuration,
            clock=lambda: NORMALIZED_AT,
        ).run(
            datetime(2026, 7, 9, tzinfo=UTC),
            datetime(2026, 7, 12, tzinfo=UTC),
        )
        alpaca_ids = {
            record.record_id for record in storage.raw_records.list(source_id=ALPACA_SOURCE_ID)
        }
        coinbase_ids = {
            record.record_id for record in storage.raw_records.list(source_id=COINBASE_SOURCE_ID)
        }

    assert alpaca.raw_records_created == 3
    assert coinbase.raw_records_created == 3
    assert len(alpaca_ids) == 3
    assert len(coinbase_ids) == 3
    assert "/v2/stocks/AAPL/bars" in alpaca_transport.calls[0][0]
    assert "feed=iex" in alpaca_transport.calls[0][0]
    assert "/products/BTC-USD/candles" in coinbase_transport.calls[0][0]
    assert "granularity=86400" in coinbase_transport.calls[0][0]
    assert len(alpaca_transport.calls) == 1
    assert len(coinbase_transport.calls) == 1


def test_catalog_resolved_sec_path_preserves_urls_headers_and_idempotence(tmp_path) -> None:
    configuration = resolve_sec_configuration(_resolver())
    identity = SecEdgarIdentity("Investment Analyst integration@example.com")

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_transport = FixtureTransport("sec")
        first_client = SecEdgarClient(
            first_transport,
            identity,
            cik=configuration.cik,
            ticker=configuration.ticker,
            sleep=lambda _: None,
            clock=lambda: FETCHED_AT,
        )
        first = SecAaplFundamentalsPipeline(
            storage,
            first_client,
            configuration=configuration,
        ).run()
        second_transport = FixtureTransport("sec")
        second_client = SecEdgarClient(
            second_transport,
            identity,
            cik=configuration.cik,
            ticker=configuration.ticker,
            sleep=lambda _: None,
            clock=lambda: FETCHED_AT,
        )
        second = SecAaplFundamentalsPipeline(
            storage,
            second_client,
            configuration=configuration,
        ).run()

    urls = [url for url, _headers in first_transport.calls]
    assert urls == [
        f"https://data.sec.gov{SUBMISSIONS_PATH}",
        f"https://data.sec.gov{COMPANY_FACTS_PATH}",
    ]
    assert first_transport.calls[0][1] == {
        "Accept": "application/json",
        "User-Agent": "Investment Analyst integration@example.com",
    }
    assert first.raw_records_created == 2
    assert second.raw_records_created == 0
    assert second.raw_records_reused == 2
    assert first.submissions_record_id == second.submissions_record_id
    assert first.companyfacts_record_id == second.companyfacts_record_id
    assert len(first_transport.calls) == 2
    assert len(second_transport.calls) == 2


def test_catalog_and_legacy_paths_produce_identical_persisted_identities(tmp_path) -> None:
    alpaca_configuration = resolve_alpaca_configuration(_resolver())
    coinbase_configuration = resolve_coinbase_configuration(_resolver())

    def import_market(root: Path, *, configured: bool) -> tuple[set[object], set[object]]:
        alpaca_transport = FixtureTransport("alpaca")
        coinbase_transport = FixtureTransport("coinbase")
        alpaca_client = AlpacaStockClient(
            alpaca_transport,
            AlpacaCredentials(api_key="key", secret_key="secret"),
            clock=lambda: FETCHED_AT,
        )
        coinbase_client = CoinbaseExchangeClient(
            coinbase_transport,
            sleep=lambda _: None,
            clock=lambda: FETCHED_AT,
        )
        with LocalStorage(StoragePaths.from_root(root)) as storage:
            alpaca_arguments = {"configuration": alpaca_configuration} if configured else {}
            coinbase_arguments = {"configuration": coinbase_configuration} if configured else {}
            AlpacaHistoricalPipeline(
                storage,
                alpaca_client,
                clock=lambda: NORMALIZED_AT,
                **alpaca_arguments,
            ).run(
                datetime(2026, 7, 7, tzinfo=UTC),
                datetime(2026, 7, 10, tzinfo=UTC),
            )
            CoinbaseHistoricalPipeline(
                storage,
                coinbase_client,
                clock=lambda: NORMALIZED_AT,
                **coinbase_arguments,
            ).run(
                datetime(2026, 7, 9, tzinfo=UTC),
                datetime(2026, 7, 12, tzinfo=UTC),
            )
            raw_ids = {record.record_id for record in storage.raw_records.list()}
            observation_ids = {
                observation.observation_id for observation in storage.observations.list()
            }
        return raw_ids, observation_ids

    legacy_ids = import_market(tmp_path / "legacy", configured=False)
    catalog_ids = import_market(tmp_path / "catalog", configured=True)

    assert catalog_ids == legacy_ids
