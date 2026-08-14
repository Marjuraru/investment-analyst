"""Tests for the catalog-driven local market universe."""

from datetime import date

import pytest
from pydantic import ValidationError

from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisCapabilities,
    AssetAnalysisFamily,
    CryptoAnalyticalProfile,
    FundamentalAnalysisMode,
    MarketAnalysisMode,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    MarketAssetUniverse,
)
from investment_analyst.core.models import AssetClass, DataFrequency


def test_default_universe_exposes_supported_assets_and_source_contracts() -> None:
    universe = InvestmentAnalystApplication.create_default().list_market_assets()

    assert universe.schema_version == "market-asset-universe-v4"
    assert universe.catalog_version == 1
    assert len(universe.assets) == 19
    assert tuple(item.asset_id for item in universe.assets) == tuple(
        sorted(item.asset_id for item in universe.assets)
    )
    by_symbol = {item.symbol: item for item in universe.assets}
    assert {
        "BTC-USD",
        "ETH-USD",
        "AAPL",
        "AMD",
        "B",
        "BVN",
        "CDE",
        "GBTC",
        "HYMC",
        "IBIT",
        "INTC",
        "MSTR",
        "MU",
        "MUX",
        "NEM",
        "PLTR",
        "SCCO",
        "TSM",
    }.issubset(by_symbol)

    apple = by_symbol["AAPL"]
    assert apple.provider == "alpaca"
    assert apple.refresh_kind == "complete_analysis"
    assert apple.has_fundamentals
    assert apple.fundamental_frequencies == (
        DataFrequency.ANNUAL,
        DataFrequency.QUARTERLY,
    )
    assert apple.analysis.family is AssetAnalysisFamily.LISTED_COMPANY
    assert apple.analysis.market_mode is MarketAnalysisMode.LISTED_SECURITY
    assert apple.analysis.fundamental_mode is FundamentalAnalysisMode.CORPORATE
    assert apple.analysis.fundamental_data_configured
    assert apple.has_corporate_valuation
    assert apple.default_market_start == date(2016, 1, 1)
    assert not apple.supports_intraday

    amd = by_symbol["AMD"]
    assert amd.analysis.fundamental_data_configured
    assert amd.analysis.declared_fundamental_capabilities == (
        "fundamentals.company_facts",
        "fundamentals.submissions",
    )
    assert amd.has_fundamentals
    assert amd.has_corporate_valuation
    assert amd.refresh_kind == "market_only"
    for symbol in ("B", "BVN", "TSM"):
        foreign_issuer = by_symbol[symbol]
        assert foreign_issuer.has_fundamentals
        assert not foreign_issuer.has_corporate_valuation
        assert foreign_issuer.fundamental_frequencies == (DataFrequency.ANNUAL,)
    intel = by_symbol["INTC"]
    assert intel.analysis.fundamental_data_configured
    assert intel.has_fundamentals
    assert intel.refresh_kind == "market_only"
    for symbol in ("MSTR", "MU", "PLTR"):
        issuer = by_symbol[symbol]
        assert issuer.analysis.fundamental_data_configured
        assert issuer.has_fundamentals
        assert issuer.refresh_kind == "market_only"
    for symbol in ("CDE", "HYMC", "MUX", "NEM", "SCCO"):
        issuer = by_symbol[symbol]
        assert issuer.analysis.fundamental_data_configured
        assert issuer.has_fundamentals
        assert issuer.refresh_kind == "market_only"
    for asset in universe.assets:
        if asset.analysis.fundamental_mode is FundamentalAnalysisMode.CORPORATE:
            assert asset.has_fundamentals is asset.analysis.fundamental_data_configured

    bitcoin = by_symbol["BTC-USD"]
    assert bitcoin.provider == "coinbase"
    assert bitcoin.refresh_kind == "market_only"
    assert bitcoin.supports_intraday
    assert bitcoin.supports_crypto_derivatives
    assert bitcoin.analysis.family is AssetAnalysisFamily.CRYPTOASSET
    assert bitcoin.analysis.market_mode is MarketAnalysisMode.CRYPTO_SPOT
    assert bitcoin.analysis.fundamental_mode is FundamentalAnalysisMode.CRYPTO_NETWORK
    assert not bitcoin.analysis.fundamental_data_configured
    assert not bitcoin.has_corporate_valuation
    assert bitcoin.intraday_source_id == "coinbase-exchange:btc-usd:minute-1-candles"
    assert bitcoin.default_market_start == date(2015, 7, 20)
    ethereum = by_symbol["ETH-USD"]
    assert ethereum.asset_id == "crypto:eth-usd"
    assert ethereum.provider == "coinbase"
    assert ethereum.source_id == "coinbase-exchange:eth-usd:daily-candles"
    assert ethereum.volume_unit == "ETH"
    assert ethereum.chart_schema_version == "crypto-spot-daily-market-chart-v1"
    assert not ethereum.supports_intraday
    assert ethereum.supports_crypto_derivatives

    assert not apple.supports_crypto_derivatives
    assert not by_symbol["IBIT"].supports_crypto_derivatives

    barrick = by_symbol["B"]
    assert barrick.asset_id == "equity:us:b"
    assert barrick.provider_identifier == "B"
    assert barrick.source_id == "alpaca-market-data:iex:b:daily-bars:adjustment-all"
    assert barrick.default_market_start == date(2025, 5, 10)
    assert barrick.analysis.family is AssetAnalysisFamily.LISTED_COMPANY
    assert barrick.has_fundamentals


def test_market_descriptor_rejects_incomplete_intraday_contract() -> None:
    with pytest.raises(ValidationError, match="intraday support"):
        MarketAssetDescriptor(
            asset_id="crypto:test-usd",
            symbol="TEST-USD",
            name="Test",
            asset_class=AssetClass.CRYPTO,
            exchange="TEST",
            quote_currency="USD",
            provider="test",
            provider_identifier="TEST-USD",
            source_id="test:daily",
            chart_schema_version="test-chart-v1",
            volume_unit="TEST",
            default_market_start=date(2020, 1, 1),
            analysis=AssetAnalysisCapabilities(
                asset_id="crypto:test-usd",
                asset_class=AssetClass.CRYPTO,
                exchange="TEST",
                family=AssetAnalysisFamily.CRYPTOASSET,
                market_mode=MarketAnalysisMode.CRYPTO_SPOT,
                fundamental_mode=FundamentalAnalysisMode.CRYPTO_NETWORK,
                crypto_profile=CryptoAnalyticalProfile.ALTCOIN,
                declared_market_capabilities=("market.minute_bars",),
                declared_fundamental_capabilities=(),
                market_data_configured=True,
                fundamental_data_configured=False,
            ),
            has_fundamentals=False,
            fundamental_frequencies=(),
            supports_intraday=True,
            refresh_kind="market_only",
        )


def test_market_universe_rejects_unsorted_or_duplicate_assets() -> None:
    universe = InvestmentAnalystApplication.create_default().list_market_assets()
    first, second = universe.assets[:2]

    with pytest.raises(ValidationError, match="sorted"):
        MarketAssetUniverse(catalog_version=1, assets=(second, first))
    with pytest.raises(ValidationError, match="duplicate"):
        MarketAssetUniverse(catalog_version=1, assets=(first, first))
