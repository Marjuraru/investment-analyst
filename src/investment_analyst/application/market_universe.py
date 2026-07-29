"""Versioned, catalog-driven market universe exposed to local clients."""

from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisCapabilities,
    analysis_capabilities_for,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_coinbase_intraday_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID as BITCOIN_ASSET_ID
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID

_ALPACA_HISTORY_START = date(2016, 1, 1)
_COINBASE_HISTORY_START = date(2015, 7, 20)
_DAILY_MARKET_CAPABILITY = "market.daily_bars"
_MINUTE_MARKET_CAPABILITY = "market.minute_bars"
_FUNDAMENTAL_CAPABILITIES = frozenset({"fundamentals.company_facts", "fundamentals.submissions"})
_COMPLETE_ANALYSIS_ASSET_IDS = frozenset({APPLE_ASSET_ID})


class MarketAssetDescriptor(ContractModel):
    """One selectable asset and its current default market-data route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    exchange: NonEmptyStr
    quote_currency: NonEmptyStr
    provider: NonEmptyStr
    provider_identifier: NonEmptyStr
    source_id: NonEmptyStr
    chart_schema_version: NonEmptyStr
    volume_unit: NonEmptyStr
    default_market_start: date
    analysis: AssetAnalysisCapabilities
    has_fundamentals: bool
    fundamental_frequencies: tuple[DataFrequency, ...]
    supports_intraday: bool
    intraday_source_id: NonEmptyStr | None = None
    intraday_schema_version: NonEmptyStr | None = None
    refresh_kind: Literal["complete_analysis", "market_only"]

    @model_validator(mode="after")
    def validate_capabilities(self) -> "MarketAssetDescriptor":
        """Keep the visible capabilities and their source contracts aligned."""
        intraday_fields = (self.intraday_source_id, self.intraday_schema_version)
        if self.supports_intraday != all(value is not None for value in intraday_fields):
            raise ValueError("intraday support requires both source and schema identities")
        if self.analysis.asset_id != self.asset_id:
            raise ValueError("analysis capabilities must match descriptor asset_id")
        if self.analysis.asset_class is not self.asset_class:
            raise ValueError("analysis capabilities must match descriptor asset_class")
        if (self.analysis.exchange or "UNKNOWN") != self.exchange:
            raise ValueError("analysis capabilities must match descriptor exchange")
        if self.analysis.market_data_configured is not True:
            raise ValueError("visible market assets require configured market data")
        if self.has_fundamentals and not self.analysis.fundamental_data_configured:
            raise ValueError("fundamental analysis requires declared fundamental data")
        expected_frequencies = (
            tuple(sorted(set(self.fundamental_frequencies), key=lambda item: item.value))
            if self.has_fundamentals
            else ()
        )
        if self.fundamental_frequencies != expected_frequencies:
            raise ValueError("fundamental frequencies must be supported, unique, and ordered")
        if self.has_fundamentals != bool(self.fundamental_frequencies):
            raise ValueError("fundamental frequencies must match fundamental availability")
        if self.refresh_kind == "complete_analysis" and not self.has_fundamentals:
            raise ValueError("complete refresh requires fundamental capability")
        return self


class MarketAssetUniverse(ContractModel):
    """Deterministic market watchlist derived from the central catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["market-asset-universe-v3"] = "market-asset-universe-v3"
    catalog_version: int = Field(ge=1)
    assets: tuple[MarketAssetDescriptor, ...]

    @model_validator(mode="after")
    def validate_assets(self) -> "MarketAssetUniverse":
        """Require a non-empty, ordered universe without duplicated assets."""
        asset_ids = tuple(asset.asset_id for asset in self.assets)
        if not asset_ids:
            raise ValueError("market asset universe must not be empty")
        if asset_ids != tuple(sorted(asset_ids)):
            raise ValueError("market asset universe must be sorted by asset_id")
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("market asset universe must not contain duplicate assets")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives for local HTTP clients."""
        return self.model_dump(mode="json")


def build_market_asset_universe(
    catalog: AssetCatalogService,
    resolver: ProviderAssetContextResolver,
) -> MarketAssetUniverse:
    """Resolve every catalog daily-market asset through one supported provider."""
    descriptors = tuple(
        _descriptor(catalog, resolver, asset.asset_id)
        for asset in catalog.list_assets(capability=_DAILY_MARKET_CAPABILITY)
        if asset.is_active
    )
    return MarketAssetUniverse(
        catalog_version=catalog.catalog_version,
        assets=tuple(sorted(descriptors, key=lambda item: item.asset_id)),
    )


def _descriptor(
    catalog: AssetCatalogService,
    resolver: ProviderAssetContextResolver,
    asset_id: str,
) -> MarketAssetDescriptor:
    asset = catalog.get(asset_id)
    market_bindings = tuple(
        binding
        for binding in asset.provider_bindings
        if _DAILY_MARKET_CAPABILITY in binding.capabilities
        and binding.namespace in {"product_id", "symbol"}
    )
    if len(market_bindings) != 1:
        raise ValueError("each visible market asset requires exactly one default daily source")
    binding = market_bindings[0]
    capabilities = frozenset(
        capability for candidate in asset.provider_bindings for capability in candidate.capabilities
    )
    fundamental_pipeline_available = (
        asset.asset_class is AssetClass.EQUITY and _FUNDAMENTAL_CAPABILITIES.issubset(capabilities)
    )
    sec_configuration = (
        resolve_sec_configuration(resolver, asset_id=asset_id)
        if fundamental_pipeline_available
        else None
    )
    fundamental_frequencies = (
        tuple(DataFrequency(item) for item in sec_configuration.supported_frequencies)
        if sec_configuration is not None
        else ()
    )
    complete_refresh_available = (
        asset.asset_id in _COMPLETE_ANALYSIS_ASSET_IDS and fundamental_pipeline_available
    )
    analysis = analysis_capabilities_for(asset)

    if binding.provider == "alpaca":
        configuration = resolve_alpaca_configuration(resolver, asset_id=asset_id)
        return MarketAssetDescriptor(
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            name=asset.name,
            asset_class=asset.asset_class,
            exchange=asset.exchange or "UNKNOWN",
            quote_currency=asset.quote_currency,
            provider=binding.provider,
            provider_identifier=configuration.symbol,
            source_id=configuration.source_id,
            chart_schema_version=(
                "aapl-market-chart-v5"
                if asset.asset_id == APPLE_ASSET_ID
                else "listed-market-chart-v1"
            ),
            volume_unit="shares",
            default_market_start=max(
                _ALPACA_HISTORY_START,
                configuration.history_start or _ALPACA_HISTORY_START,
            ),
            analysis=analysis,
            has_fundamentals=fundamental_pipeline_available,
            fundamental_frequencies=fundamental_frequencies,
            supports_intraday=False,
            refresh_kind=("complete_analysis" if complete_refresh_available else "market_only"),
        )

    if binding.provider == "coinbase" and asset.asset_id == BITCOIN_ASSET_ID:
        daily = resolve_coinbase_configuration(resolver)
        intraday = resolve_coinbase_intraday_configuration(resolver)
        return MarketAssetDescriptor(
            asset_id=asset.asset_id,
            symbol=daily.product_id,
            name=asset.name,
            asset_class=asset.asset_class,
            exchange=asset.exchange or "COINBASE",
            quote_currency=asset.quote_currency,
            provider=binding.provider,
            provider_identifier=daily.product_id,
            source_id=daily.source_id,
            chart_schema_version="btc-market-chart-v1",
            volume_unit="BTC",
            default_market_start=_COINBASE_HISTORY_START,
            analysis=analysis,
            has_fundamentals=False,
            fundamental_frequencies=(),
            supports_intraday=_MINUTE_MARKET_CAPABILITY in binding.capabilities,
            intraday_source_id=intraday.source_id,
            intraday_schema_version="btc-intraday-chart-v1",
            refresh_kind="market_only",
        )

    raise ValueError(
        f"unsupported default daily-market provider for {asset_id}: {binding.provider}"
    )


__all__ = [
    "MarketAssetDescriptor",
    "MarketAssetUniverse",
    "build_market_asset_universe",
]
