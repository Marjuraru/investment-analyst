"""Build provider-specific configurations from one resolved catalog context."""

from datetime import date

from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    CoinbaseAssetConfiguration,
    DeribitAssetConfiguration,
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
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as COINBASE_ASSET_ID,
)
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
    history_starts = tuple(
        binding.identifier for binding in context.bindings if binding.namespace == "history_start"
    )
    if len(history_starts) > 1:
        raise ValueError("Alpaca history_start binding is ambiguous")
    history_start: date | None = None
    if history_starts:
        try:
            history_start = date.fromisoformat(history_starts[0])
        except ValueError as error:
            raise ValueError("Alpaca history_start binding must use YYYY-MM-DD") from error
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
        history_start=history_start,
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
    product_id = context.require_identifier("product_id")
    base_unit, quote_unit = product_id.split("-", maxsplit=1)
    return CoinbaseAssetConfiguration(
        asset_id=context.asset.asset_id,
        product_id=product_id,
        source_id=coinbase_source_id(product_id, DAILY_GRANULARITY_SECONDS),
        granularity_seconds=DAILY_GRANULARITY_SECONDS,
        base_unit=base_unit,
        quote_unit=quote_unit,
        symbol=context.asset.symbol,
        name=context.asset.name,
        asset_class=context.asset.asset_class,
        quote_currency=context.asset.quote_currency,
        exchange=context.asset.exchange or "UNKNOWN",
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
    product_id = context.require_identifier("product_id")
    base_unit, quote_unit = product_id.split("-", maxsplit=1)
    return CoinbaseAssetConfiguration(
        asset_id=context.asset.asset_id,
        product_id=product_id,
        source_id=coinbase_source_id(product_id, MINUTE_GRANULARITY_SECONDS),
        granularity_seconds=MINUTE_GRANULARITY_SECONDS,
        base_unit=base_unit,
        quote_unit=quote_unit,
        symbol=context.asset.symbol,
        name=context.asset.name,
        asset_class=context.asset.asset_class,
        quote_currency=context.asset.quote_currency,
        exchange=context.asset.exchange or "UNKNOWN",
    )


def resolve_deribit_configuration(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str,
) -> DeribitAssetConfiguration:
    """Resolve the complete public Deribit derivatives contract for one crypto asset."""
    context = resolver.resolve(
        asset_id,
        provider="deribit",
        required_namespaces=("currency", "instrument_name"),
        required_capabilities=(
            "derivatives.funding.hourly",
            "derivatives.perpetual.snapshot",
            "derivatives.volatility_index.daily",
        ),
    )
    currency = context.require_identifier("currency")
    instrument_name = context.require_identifier("instrument_name")
    funding_source_id, dvol_source_id, summary_source_id = deribit_source_ids(
        currency,
        instrument_name,
    )
    return DeribitAssetConfiguration(
        asset_id=context.asset.asset_id,
        currency=currency,
        instrument_name=instrument_name,
        funding_source_id=funding_source_id,
        dvol_source_id=dvol_source_id,
        summary_source_id=summary_source_id,
        symbol=context.asset.symbol,
        name=context.asset.name,
        asset_class=context.asset.asset_class,
        quote_currency=context.asset.quote_currency,
        exchange=context.asset.exchange or "UNKNOWN",
        provider_symbols=context.asset.provider_symbols,
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
