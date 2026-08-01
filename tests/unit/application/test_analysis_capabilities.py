"""Tests for provider-independent asset analysis classification."""

import pytest
from pydantic import ValidationError

from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisCapabilities,
    AssetAnalysisFamily,
    CryptoAnalyticalProfile,
    FundamentalAnalysisMode,
    MarketAnalysisMode,
    analysis_capabilities_for,
)
from investment_analyst.catalog.models import CatalogAsset, CatalogCryptoProfile, ProviderBinding
from investment_analyst.core.models import AssetClass


def _binding(
    *,
    provider: str,
    namespace: str,
    identifier: str,
    capabilities: tuple[str, ...],
) -> ProviderBinding:
    return ProviderBinding(
        provider=provider,
        namespace=namespace,
        identifier=identifier,
        capabilities=capabilities,
    )


def _asset(
    *,
    asset_id: str,
    symbol: str,
    asset_class: AssetClass,
    exchange: str,
    quote_currency: str,
    bindings: tuple[ProviderBinding, ...],
    crypto_profile: CatalogCryptoProfile | None = None,
) -> CatalogAsset:
    return CatalogAsset(
        asset_id=asset_id,
        symbol=symbol,
        name=f"{symbol} test asset",
        asset_class=asset_class,
        quote_currency=quote_currency,
        exchange=exchange,
        aliases=(symbol,),
        provider_bindings=bindings,
        crypto_profile=crypto_profile,
    )


def test_bvl_equity_uses_listed_company_domains_without_us_assumptions() -> None:
    asset = _asset(
        asset_id="equity:pe:bvl:cverdec1",
        symbol="CVERDEC1",
        asset_class=AssetClass.EQUITY,
        exchange="BVL",
        quote_currency="PEN",
        bindings=(
            _binding(
                provider="bvl",
                namespace="symbol",
                identifier="CVERDEC1",
                capabilities=("market.daily_bars",),
            ),
        ),
    )

    profile = analysis_capabilities_for(asset)

    assert profile.exchange == "BVL"
    assert profile.family is AssetAnalysisFamily.LISTED_COMPANY
    assert profile.market_mode is MarketAnalysisMode.LISTED_SECURITY
    assert profile.fundamental_mode is FundamentalAnalysisMode.CORPORATE
    assert profile.market_data_configured
    assert not profile.fundamental_data_configured
    assert profile.declared_market_capabilities == ("market.daily_bars",)


def test_bvl_fundamental_source_remains_provider_specific_but_corporate() -> None:
    asset = _asset(
        asset_id="equity:pe:bvl:bvn",
        symbol="BVN",
        asset_class=AssetClass.EQUITY,
        exchange="BVL",
        quote_currency="PEN",
        bindings=(
            _binding(
                provider="bvl",
                namespace="symbol",
                identifier="BVN",
                capabilities=("market.daily_bars",),
            ),
            _binding(
                provider="smv",
                namespace="issuer_code",
                identifier="BVN",
                capabilities=(
                    "fundamentals.disclosures",
                    "fundamentals.financial_statements",
                ),
            ),
        ),
    )

    profile = analysis_capabilities_for(asset)

    assert profile.fundamental_mode is FundamentalAnalysisMode.CORPORATE
    assert profile.fundamental_data_configured
    assert profile.declared_fundamental_capabilities == (
        "fundamentals.disclosures",
        "fundamentals.financial_statements",
    )


@pytest.mark.parametrize(
    ("asset_class", "family", "market_mode", "fundamental_mode"),
    [
        (
            AssetClass.ETF,
            AssetAnalysisFamily.LISTED_FUND,
            MarketAnalysisMode.LISTED_SECURITY,
            FundamentalAnalysisMode.INVESTMENT_FUND,
        ),
        (
            AssetClass.CRYPTO,
            AssetAnalysisFamily.CRYPTOASSET,
            MarketAnalysisMode.CRYPTO_SPOT,
            FundamentalAnalysisMode.CRYPTO_NETWORK,
        ),
    ],
)
def test_funds_and_crypto_do_not_inherit_corporate_analysis(
    asset_class: AssetClass,
    family: AssetAnalysisFamily,
    market_mode: MarketAnalysisMode,
    fundamental_mode: FundamentalAnalysisMode,
) -> None:
    provider = "coinbase" if asset_class is AssetClass.CRYPTO else "alpaca"
    namespace = "product_id" if asset_class is AssetClass.CRYPTO else "symbol"
    identifier = "TEST-USD" if asset_class is AssetClass.CRYPTO else "TEST"
    asset = _asset(
        asset_id=f"{asset_class.value}:test",
        symbol="TEST",
        asset_class=asset_class,
        exchange="COINBASE" if asset_class is AssetClass.CRYPTO else "NYSE ARCA",
        quote_currency="USD",
        crypto_profile=(CatalogCryptoProfile.ALTCOIN if asset_class is AssetClass.CRYPTO else None),
        bindings=(
            _binding(
                provider=provider,
                namespace=namespace,
                identifier=identifier,
                capabilities=("market.daily_bars",),
            ),
        ),
    )

    profile = analysis_capabilities_for(asset)

    assert profile.family is family
    assert profile.market_mode is market_mode
    assert profile.fundamental_mode is fundamental_mode


def test_profile_rejects_domains_that_do_not_match_the_asset_class() -> None:
    with pytest.raises(ValidationError, match="do not match"):
        AssetAnalysisCapabilities(
            asset_id="crypto:test",
            asset_class=AssetClass.CRYPTO,
            exchange="TEST",
            family=AssetAnalysisFamily.LISTED_COMPANY,
            market_mode=MarketAnalysisMode.LISTED_SECURITY,
            fundamental_mode=FundamentalAnalysisMode.CORPORATE,
            declared_market_capabilities=("market.daily_bars",),
            declared_fundamental_capabilities=(),
            market_data_configured=True,
            fundamental_data_configured=False,
        )


def test_profile_rejects_truthy_configuration_flags() -> None:
    with pytest.raises(ValidationError, match="must be booleans"):
        AssetAnalysisCapabilities(
            asset_id="equity:test",
            asset_class=AssetClass.EQUITY,
            exchange="TEST",
            family=AssetAnalysisFamily.LISTED_COMPANY,
            market_mode=MarketAnalysisMode.LISTED_SECURITY,
            fundamental_mode=FundamentalAnalysisMode.CORPORATE,
            declared_market_capabilities=("market.daily_bars",),
            declared_fundamental_capabilities=(),
            market_data_configured=1,
            fundamental_data_configured=False,
        )


@pytest.mark.parametrize(
    ("catalog_profile", "analytical_profile", "unsupported"),
    [
        (CatalogCryptoProfile.BITCOIN, CryptoAnalyticalProfile.BITCOIN, ()),
        (CatalogCryptoProfile.ETHEREUM, CryptoAnalyticalProfile.ETHEREUM, ()),
        (CatalogCryptoProfile.ALTCOIN, CryptoAnalyticalProfile.ALTCOIN, ()),
        (
            CatalogCryptoProfile.STABLECOIN,
            CryptoAnalyticalProfile.UNSUPPORTED,
            ("stablecoin_or_wrapped_asset",),
        ),
        (
            CatalogCryptoProfile.WRAPPED,
            CryptoAnalyticalProfile.UNSUPPORTED,
            ("stablecoin_or_wrapped_asset",),
        ),
    ],
)
def test_crypto_profiles_are_explicit_and_isolated(
    catalog_profile: CatalogCryptoProfile,
    analytical_profile: CryptoAnalyticalProfile,
    unsupported: tuple[str, ...],
) -> None:
    asset = _asset(
        asset_id=f"crypto:test-{catalog_profile.value}",
        symbol="TEST",
        asset_class=AssetClass.CRYPTO,
        exchange="SYNTHETIC",
        quote_currency="USD",
        crypto_profile=catalog_profile,
        bindings=(
            _binding(
                provider="coinbase",
                namespace="product_id",
                identifier="TEST-USD",
                capabilities=("market.daily_bars",),
            ),
        ),
    )

    profile = analysis_capabilities_for(asset)

    assert profile.crypto_profile is analytical_profile
    assert profile.unsupported_reasons == unsupported
    assert profile.fundamental_mode is FundamentalAnalysisMode.CRYPTO_NETWORK
    assert not profile.fundamental_data_configured
