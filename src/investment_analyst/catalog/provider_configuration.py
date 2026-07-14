"""Build provider-specific configurations from one resolved catalog context."""

from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    CoinbaseAssetConfiguration,
    SecAssetConfiguration,
)
from investment_analyst.providers.crypto.coinbase_exchange import DAILY_GRANULARITY_SECONDS
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as COINBASE_ASSET_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import SOURCE_ID as COINBASE_SOURCE_ID
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID
from investment_analyst.providers.fundamentals.sec_raw_records import (
    COMPANY_FACTS_SOURCE_ID,
    SUBMISSIONS_SOURCE_ID,
)
from investment_analyst.providers.market.alpaca_normalizer import SOURCE_ID as ALPACA_SOURCE_ID
from investment_analyst.providers.market.alpaca_stock import ADJUSTMENT, FEED


def resolve_alpaca_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str = APPLE_ASSET_ID,
) -> AlpacaAssetConfiguration:
    """Resolve the current Apple Alpaca IEX configuration once."""
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
        source_id=ALPACA_SOURCE_ID,
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


def resolve_sec_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str = APPLE_ASSET_ID,
) -> SecAssetConfiguration:
    """Resolve the current Apple SEC issuer configuration once."""
    context = resolver.resolve(
        asset_id,
        provider="sec",
        required_namespaces=("cik", "ticker"),
        required_capabilities=(
            "fundamentals.company_facts",
            "fundamentals.submissions",
        ),
    )
    return SecAssetConfiguration(
        asset_id=context.asset.asset_id,
        cik=context.require_identifier("cik"),
        ticker=context.require_identifier("ticker"),
        submissions_source_id=SUBMISSIONS_SOURCE_ID,
        companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
    )
