"""Build provider-specific configurations from one resolved catalog context."""

from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    CoinbaseAssetConfiguration,
    SecAccountingStandard,
    SecAssetConfiguration,
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
from investment_analyst.providers.crypto.coinbase_normalizer import SOURCE_ID as COINBASE_SOURCE_ID
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.market.alpaca_normalizer import alpaca_source_id
from investment_analyst.providers.market.alpaca_stock import ADJUSTMENT, FEED
from investment_analyst.providers.peru.asset_config import SmvBvlAssetConfiguration


def resolve_alpaca_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str = APPLE_ASSET_ID,
) -> AlpacaAssetConfiguration:
    """Resolve one catalog-backed Alpaca IEX configuration."""
    context = resolver.resolve(
        asset_id,
        provider="alpaca",
        required_namespaces=("symbol",),
        required_capabilities=("market.daily_bars",),
    )
    return AlpacaAssetConfiguration(
        asset_id=context.asset.asset_id,
        symbol=context.require_identifier("symbol"),
        feed=FEED,
        adjustment=ADJUSTMENT,
        source_id=alpaca_source_id(context.require_identifier("symbol")),
        name=context.asset.name,
        asset_class=context.asset.asset_class,
        quote_currency=context.asset.quote_currency,
        exchange=context.asset.exchange or "UNKNOWN",
    )


def resolve_coinbase_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str = COINBASE_ASSET_ID,
) -> CoinbaseAssetConfiguration:
    """Resolve the current Coinbase daily-candle configuration once."""
    context = resolver.resolve(
        asset_id,
        provider="coinbase",
        required_namespaces=("product_id",),
        required_capabilities=("market.daily_bars",),
    )
    return CoinbaseAssetConfiguration(
        asset_id=context.asset.asset_id,
        product_id=context.require_identifier("product_id"),
        source_id=COINBASE_SOURCE_ID,
        granularity_seconds=DAILY_GRANULARITY_SECONDS,
    )


def resolve_coinbase_intraday_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str = COINBASE_ASSET_ID,
) -> CoinbaseAssetConfiguration:
    """Resolve the separate Coinbase one-minute candle configuration."""
    context = resolver.resolve(
        asset_id,
        provider="coinbase",
        required_namespaces=("product_id",),
        required_capabilities=("market.minute_bars",),
    )
    return CoinbaseAssetConfiguration(
        asset_id=context.asset.asset_id,
        product_id=context.require_identifier("product_id"),
        source_id=COINBASE_INTRADAY_SOURCE_ID,
        granularity_seconds=MINUTE_GRANULARITY_SECONDS,
    )


def resolve_sec_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str = APPLE_ASSET_ID,
) -> SecAssetConfiguration:
    """Resolve one catalog-backed SEC corporate issuer configuration."""
    context = resolver.resolve(
        asset_id,
        provider="sec",
        required_namespaces=("cik", "taxonomy", "ticker"),
        required_capabilities=(
            "fundamentals.company_facts",
            "fundamentals.submissions",
        ),
    )
    ticker = context.require_identifier("ticker")
    submissions_source_id, companyfacts_source_id = sec_source_ids(ticker)
    return SecAssetConfiguration(
        asset_id=context.asset.asset_id,
        cik=context.require_identifier("cik"),
        ticker=ticker,
        submissions_source_id=submissions_source_id,
        companyfacts_source_id=companyfacts_source_id,
        name=context.asset.name,
        asset_class=context.asset.asset_class,
        quote_currency=context.asset.quote_currency,
        exchange=context.asset.exchange or "UNKNOWN",
        accounting_standard=SecAccountingStandard(context.require_identifier("taxonomy")),
    )


def resolve_smv_bvl_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str,
) -> SmvBvlAssetConfiguration:
    """Resolve one BVL listing and its exact-name SMV registry query."""
    bvl_context = resolver.resolve(
        asset_id,
        provider="bvl",
        required_namespaces=("isin", "mnemonic"),
        required_capabilities=("registry.exchange_listing",),
    )
    smv_context = resolver.resolve(
        asset_id,
        provider="smv",
        required_namespaces=("legal_name",),
        required_capabilities=("registry.issuer",),
    )
    security_codes = tuple(
        binding.identifier
        for binding in smv_context.bindings
        if binding.namespace == "security_code"
    )
    if len(security_codes) > 1:
        raise ValueError("SMV security code binding is ambiguous")
    return SmvBvlAssetConfiguration(
        asset_id=bvl_context.asset.asset_id,
        name=bvl_context.asset.name,
        asset_class=bvl_context.asset.asset_class,
        exchange=bvl_context.asset.exchange or "UNKNOWN",
        quote_currency=bvl_context.asset.quote_currency,
        mnemonic=bvl_context.require_identifier("mnemonic"),
        isin=bvl_context.require_identifier("isin"),
        legal_name=smv_context.require_identifier("legal_name"),
        reported_security_code=security_codes[0] if security_codes else None,
    )
