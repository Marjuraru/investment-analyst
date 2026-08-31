"""Unit coverage for typed configurations built from the central catalog."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    ProviderBinding,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_coinbase_intraday_configuration,
    resolve_deribit_configuration,
    resolve_sec_configuration,
    resolve_sec_cusip_binding,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextResolver,
    ProviderAssetNotConfiguredError,
    ProviderCapabilityMissingError,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    CoinbaseAssetConfiguration,
    DeribitAssetConfiguration,
    ProviderConfigurationError,
    SecAccountingStandard,
    SecAssetConfiguration,
    coinbase_source_id,
    deribit_source_ids,
    sec_source_ids,
)
from investment_analyst.providers.crypto.coinbase_exchange import (
    DAILY_GRANULARITY_SECONDS,
    MINUTE_GRANULARITY_SECONDS,
)
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import (
    SOURCE_ID as COINBASE_INTRADAY_SOURCE_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as COINBASE_ASSET_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import PRODUCT_ID, SOURCE_ID
from investment_analyst.providers.fundamentals.sec_edgar import APPLE_CIK, APPLE_TICKER
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.fundamentals.sec_raw_records import (
    COMPANY_FACTS_SOURCE_ID,
    SUBMISSIONS_SOURCE_ID,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    SOURCE_ID as ALPACA_SOURCE_ID,
)
from investment_analyst.providers.market.alpaca_stock import ADJUSTMENT, FEED


def _resolver() -> ProviderAssetContextResolver:
    return ProviderAssetContextResolver(AssetCatalogService.load_default())


def test_factories_preserve_current_provider_and_persisted_identities() -> None:
    alpaca = resolve_alpaca_configuration(_resolver())
    coinbase = resolve_coinbase_configuration(_resolver())
    coinbase_intraday = resolve_coinbase_intraday_configuration(_resolver())
    sec = resolve_sec_configuration(_resolver())

    assert alpaca == AlpacaAssetConfiguration(
        asset_id=APPLE_ASSET_ID,
        symbol=APPLE_TICKER,
        feed=FEED,
        adjustment=ADJUSTMENT,
        source_id=ALPACA_SOURCE_ID,
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )
    assert coinbase == CoinbaseAssetConfiguration(
        asset_id=COINBASE_ASSET_ID,
        product_id=PRODUCT_ID,
        source_id=SOURCE_ID,
        granularity_seconds=DAILY_GRANULARITY_SECONDS,
        base_unit="BTC",
        quote_unit="USD",
        symbol="BTC",
        name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        exchange="COINBASE",
    )
    assert coinbase_intraday == CoinbaseAssetConfiguration(
        asset_id=COINBASE_ASSET_ID,
        product_id=PRODUCT_ID,
        source_id=COINBASE_INTRADAY_SOURCE_ID,
        granularity_seconds=MINUTE_GRANULARITY_SECONDS,
        base_unit="BTC",
        quote_unit="USD",
        symbol="BTC",
        name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        exchange="COINBASE",
    )
    assert sec == SecAssetConfiguration(
        asset_id=APPLE_ASSET_ID,
        cik=APPLE_CIK,
        ticker=APPLE_TICKER,
        submissions_source_id=SUBMISSIONS_SOURCE_ID,
        companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )
    assert len(sec.cik) == 10
    assert resolve_sec_cusip_binding(_resolver()) == "037833100"


def test_alpaca_configuration_scales_from_catalog_without_changing_apple_identity() -> None:
    resolver = _resolver()

    barrick = resolve_alpaca_configuration(resolver, asset_id="equity:us:b")
    bvn = resolve_alpaca_configuration(resolver, asset_id="equity:us:bvn")
    gld = resolve_alpaca_configuration(resolver, asset_id="etf:us:gld")

    assert barrick.history_start == date(2025, 5, 10)
    assert (bvn.symbol, bvn.source_id, bvn.exchange) == (
        "BVN",
        "alpaca-market-data:iex:bvn:daily-bars:adjustment-all",
        "NYSE",
    )
    assert (gld.symbol, gld.source_id, gld.asset_class.value) == (
        "GLD",
        "alpaca-market-data:iex:gld:daily-bars:adjustment-all",
        "etf",
    )
    assert bvn.history_start is None
    assert gld.history_start is None


def test_coinbase_daily_configuration_resolves_inactive_ethereum_without_intraday() -> None:
    resolver = _resolver()

    ethereum = resolve_coinbase_configuration(resolver, asset_id="crypto:eth-usd")

    assert ethereum == CoinbaseAssetConfiguration(
        asset_id="crypto:eth-usd",
        product_id="ETH-USD",
        source_id="coinbase-exchange:eth-usd:daily-candles",
        granularity_seconds=DAILY_GRANULARITY_SECONDS,
        base_unit="ETH",
        quote_unit="USD",
        symbol="ETH",
        name="Ethereum",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        exchange="COINBASE",
    )
    with pytest.raises(ProviderCapabilityMissingError):
        resolve_coinbase_intraday_configuration(resolver, asset_id="crypto:eth-usd")


def test_coinbase_altcoin_history_start_is_explicit_and_btc_is_unchanged() -> None:
    resolver = _resolver()

    solana = resolve_coinbase_configuration(resolver, asset_id="crypto:sol-usd")
    bitcoin = resolve_coinbase_configuration(resolver)

    assert solana.history_start == date(2025, 1, 1)
    assert solana.source_id == "coinbase-exchange:sol-usd:daily-candles"
    assert bitcoin.history_start is None
    with pytest.raises(ValidationError, match="must be a date"):
        CoinbaseAssetConfiguration(
            asset_id="crypto:sol-usd",
            product_id="SOL-USD",
            source_id="coinbase-exchange:sol-usd:daily-candles",
            granularity_seconds=DAILY_GRANULARITY_SECONDS,
            base_unit="SOL",
            quote_unit="USD",
            symbol="SOL",
            name="Solana",
            asset_class=AssetClass.CRYPTO,
            quote_currency="USD",
            exchange="COINBASE",
            history_start=datetime(2025, 1, 1),
        )


def test_alpaca_configuration_requires_explicit_asset_metadata() -> None:
    with pytest.raises(ValidationError):
        AlpacaAssetConfiguration(
            asset_id=APPLE_ASSET_ID,
            symbol=APPLE_TICKER,
            feed=FEED,
            adjustment=ADJUSTMENT,
            source_id=ALPACA_SOURCE_ID,
        )


def test_alpaca_configuration_validates_optional_history_start() -> None:
    with pytest.raises(ValidationError, match="must use YYYY-MM-DD"):
        AlpacaAssetConfiguration(
            asset_id=APPLE_ASSET_ID,
            symbol=APPLE_TICKER,
            feed=FEED,
            adjustment=ADJUSTMENT,
            source_id=ALPACA_SOURCE_ID,
            name="Apple Inc.",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            exchange="NASDAQ",
            history_start="2025-99-99",
        )


def test_configurations_are_strict_frozen_and_preserve_identifier_text() -> None:
    configuration = resolve_sec_configuration(_resolver())

    with pytest.raises(ValidationError):
        configuration.cik = "1"
    with pytest.raises(ValidationError):
        SecAssetConfiguration(
            asset_id=APPLE_ASSET_ID,
            cik="320193",
            ticker=APPLE_TICKER,
            submissions_source_id=SUBMISSIONS_SOURCE_ID,
            companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
            name="Apple Inc.",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            exchange="NASDAQ",
        )
    with pytest.raises(ValidationError):
        CoinbaseAssetConfiguration(
            asset_id=COINBASE_ASSET_ID,
            product_id=PRODUCT_ID,
            source_id=SOURCE_ID,
            granularity_seconds=True,
            base_unit="BTC",
            quote_unit="USD",
            symbol="BTC",
            name="Bitcoin",
            asset_class=AssetClass.CRYPTO,
            quote_currency="USD",
            exchange="COINBASE",
        )


def test_sec_source_ids_are_issuer_specific_and_preserve_apple_identity() -> None:
    assert sec_source_ids("AAPL") == (
        SUBMISSIONS_SOURCE_ID,
        COMPANY_FACTS_SOURCE_ID,
    )
    assert sec_source_ids("AMD") == (
        "sec-edgar:amd:submissions",
        "sec-edgar:amd:companyfacts",
    )
    with pytest.raises(ProviderConfigurationError):
        sec_source_ids("AMD/USD")
    with pytest.raises(ProviderConfigurationError):
        sec_source_ids("amd")


def test_coinbase_source_identity_scales_by_product_and_granularity() -> None:
    assert coinbase_source_id("BTC-USD", DAILY_GRANULARITY_SECONDS) == SOURCE_ID
    assert coinbase_source_id("BTC-USD", MINUTE_GRANULARITY_SECONDS) == COINBASE_INTRADAY_SOURCE_ID
    assert (
        coinbase_source_id("ETH-USD", DAILY_GRANULARITY_SECONDS)
        == "coinbase-exchange:eth-usd:daily-candles"
    )
    with pytest.raises(ProviderConfigurationError):
        coinbase_source_id("eth/usd", DAILY_GRANULARITY_SECONDS)


@pytest.mark.parametrize(
    ("asset_id", "currency", "instrument_name", "expected_sources"),
    [
        (
            "crypto:btc-usd",
            "BTC",
            "BTC-PERPETUAL",
            (
                "deribit:btc-perpetual:funding-rate-history",
                "deribit:btc:dvol:daily",
                "deribit:btc-perpetual:book-summary",
            ),
        ),
        (
            "crypto:eth-usd",
            "ETH",
            "ETH-PERPETUAL",
            (
                "deribit:eth-perpetual:funding-rate-history",
                "deribit:eth:dvol:daily",
                "deribit:eth-perpetual:book-summary",
            ),
        ),
    ],
)
def test_deribit_configuration_resolves_exact_three_source_contract(
    asset_id: str,
    currency: str,
    instrument_name: str,
    expected_sources: tuple[str, str, str],
) -> None:
    configuration = resolve_deribit_configuration(_resolver(), asset_id=asset_id)

    assert configuration == DeribitAssetConfiguration(
        asset_id=asset_id,
        currency=currency,
        instrument_name=instrument_name,
        funding_source_id=expected_sources[0],
        dvol_source_id=expected_sources[1],
        summary_source_id=expected_sources[2],
        symbol=currency,
        name="Bitcoin" if currency == "BTC" else "Ethereum",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        exchange="COINBASE",
        provider_symbols={"coinbase_exchange": f"{currency}-USD"},
    )
    assert deribit_source_ids(currency, instrument_name) == expected_sources


def test_deribit_source_identity_rejects_cross_currency_or_non_perpetual() -> None:
    with pytest.raises(ProviderConfigurationError):
        deribit_source_ids("BTC", "ETH-PERPETUAL")
    with pytest.raises(ProviderConfigurationError):
        deribit_source_ids("BTC", "BTC-30DEC26")


def test_sec_configuration_resolves_a_future_us_issuer_without_apple_ids() -> None:
    capabilities = (
        "fundamentals.company_facts",
        "fundamentals.submissions",
    )
    catalog = AssetCatalogService(
        AssetCatalogDocument(
            catalog_version=1,
            assets=(
                CatalogAsset(
                    asset_id="equity:us:amd",
                    symbol="AMD",
                    name="Advanced Micro Devices, Inc.",
                    asset_class=AssetClass.EQUITY,
                    quote_currency="USD",
                    exchange="NASDAQ",
                    provider_symbols={},
                    aliases=("AMD",),
                    provider_bindings=(
                        ProviderBinding(
                            provider="sec",
                            namespace="cik",
                            identifier="0000002488",
                            capabilities=capabilities,
                        ),
                        ProviderBinding(
                            provider="sec",
                            namespace="taxonomy",
                            identifier="us-gaap",
                            capabilities=capabilities,
                        ),
                        ProviderBinding(
                            provider="sec",
                            namespace="ticker",
                            identifier="AMD",
                            capabilities=capabilities,
                        ),
                    ),
                ),
            ),
        )
    )

    configuration = resolve_sec_configuration(
        ProviderAssetContextResolver(catalog),
        asset_id="equity:us:amd",
    )

    assert configuration.asset_id == "equity:us:amd"
    assert configuration.name == "Advanced Micro Devices, Inc."
    assert configuration.submissions_source_id == "sec-edgar:amd:submissions"
    assert configuration.companyfacts_source_id == "sec-edgar:amd:companyfacts"


def test_default_catalog_resolves_official_amd_sec_identity() -> None:
    configuration = resolve_sec_configuration(
        _resolver(),
        asset_id="equity:us:amd",
    )

    assert configuration == SecAssetConfiguration(
        asset_id="equity:us:amd",
        cik="0000002488",
        ticker="AMD",
        submissions_source_id="sec-edgar:amd:submissions",
        companyfacts_source_id="sec-edgar:amd:companyfacts",
        name="Advanced Micro Devices, Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def test_default_catalog_resolves_bvn_as_annual_ifrs_foreign_issuer() -> None:
    configuration = resolve_sec_configuration(
        _resolver(),
        asset_id="equity:us:bvn",
    )

    assert configuration.cik == "0001013131"
    assert configuration.ticker == "BVN"
    assert configuration.accounting_standard is SecAccountingStandard.IFRS
    assert configuration.supported_forms == frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})
    assert configuration.supported_frequencies == ("annual",)


@pytest.mark.parametrize(
    ("asset_id", "cik", "ticker"),
    [
        ("equity:us:b", "0000756894", "B"),
        ("equity:us:tsm", "0001046179", "TSM"),
    ],
)
def test_default_catalog_resolves_additional_ifrs_foreign_issuers(
    asset_id: str,
    cik: str,
    ticker: str,
) -> None:
    configuration = resolve_sec_configuration(_resolver(), asset_id=asset_id)

    assert configuration.cik == cik
    assert configuration.ticker == ticker
    assert configuration.accounting_standard is SecAccountingStandard.IFRS
    assert configuration.supported_frequencies == ("annual",)


def test_sec_configuration_rejects_noncorporate_or_cross_issuer_identity() -> None:
    common = {
        "asset_id": "equity:us:amd",
        "cik": "0000002488",
        "ticker": "AMD",
        "submissions_source_id": "sec-edgar:amd:submissions",
        "companyfacts_source_id": "sec-edgar:amd:companyfacts",
        "name": "Advanced Micro Devices, Inc.",
        "quote_currency": "USD",
        "exchange": "NASDAQ",
    }
    with pytest.raises(ValidationError, match="requires an equity"):
        SecAssetConfiguration(**common, asset_class=AssetClass.ETF)
    with pytest.raises(ValidationError, match="configured ticker"):
        SecAssetConfiguration(
            **{**common, "companyfacts_source_id": COMPANY_FACTS_SOURCE_ID},
            asset_class=AssetClass.EQUITY,
        )


def test_invalid_provider_asset_pairs_fail_before_client_construction() -> None:
    resolver = _resolver()

    with pytest.raises(ProviderAssetNotConfiguredError):
        resolve_coinbase_configuration(resolver, asset_id=APPLE_ASSET_ID)
    with pytest.raises(ProviderAssetNotConfiguredError):
        resolve_coinbase_intraday_configuration(resolver, asset_id=APPLE_ASSET_ID)
    with pytest.raises(ProviderAssetNotConfiguredError):
        resolve_sec_configuration(resolver, asset_id=COINBASE_ASSET_ID)
