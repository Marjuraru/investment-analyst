"""Versioned, catalog-driven market universe exposed to local clients."""

from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.application.analysis_capabilities import (
    AssetAnalysisCapabilities,
    AssetAnalysisFamily,
    CryptoAnalyticalProfile,
    FundamentalAnalysisMode,
    analysis_capabilities_for,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_coinbase_configuration,
    resolve_coinbase_intraday_configuration,
    resolve_deribit_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextError,
    ProviderAssetContextResolver,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID as APPLE_ASSET_ID

_ALPACA_HISTORY_START = date(2016, 1, 1)
_COINBASE_HISTORY_START = date(2015, 7, 20)
_DAILY_MARKET_CAPABILITY = "market.daily_bars"
_MINUTE_MARKET_CAPABILITY = "market.minute_bars"
_FUNDAMENTAL_CAPABILITIES = frozenset({"fundamentals.company_facts", "fundamentals.submissions"})


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
    has_corporate_valuation: bool = False
    fundamental_frequencies: tuple[DataFrequency, ...]
    fundamental_source_ids: tuple[NonEmptyStr, ...] = ()
    supports_intraday: bool
    supports_crypto_derivatives: bool = False
    intraday_source_id: NonEmptyStr | None = None
    intraday_schema_version: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_capabilities(self) -> "MarketAssetDescriptor":
        """Keep the visible capabilities and their source contracts aligned."""
        intraday_fields = (self.intraday_source_id, self.intraday_schema_version)
        if self.supports_intraday != all(value is not None for value in intraday_fields):
            raise ValueError("intraday support requires both source and schema identities")
        if self.supports_crypto_derivatives and (
            self.asset_class is not AssetClass.CRYPTO
            or self.analysis.family is not AssetAnalysisFamily.CRYPTOASSET
        ):
            raise ValueError("crypto derivatives require a cryptoasset descriptor")
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
        if self.has_corporate_valuation and (
            not self.has_fundamentals
            or self.analysis.family is not AssetAnalysisFamily.LISTED_COMPANY
            or self.analysis.fundamental_mode is not FundamentalAnalysisMode.CORPORATE
        ):
            raise ValueError("corporate valuation requires a listed corporate issuer")
        expected_frequencies = (
            tuple(sorted(set(self.fundamental_frequencies), key=lambda item: item.value))
            if self.has_fundamentals
            else ()
        )
        if self.fundamental_frequencies != expected_frequencies:
            raise ValueError("fundamental frequencies must be supported, unique, and ordered")
        if self.has_fundamentals != bool(self.fundamental_frequencies):
            raise ValueError("fundamental frequencies must match fundamental availability")
        expected_sources = tuple(sorted(set(self.fundamental_source_ids)))
        if self.fundamental_source_ids != expected_sources:
            raise ValueError("fundamental source IDs must be unique and sorted")
        if self.has_fundamentals != bool(self.fundamental_source_ids):
            raise ValueError("fundamental source IDs must match fundamental availability")
        return self


class MarketAssetUniverse(ContractModel):
    """Deterministic market watchlist derived from the central catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["market-asset-universe-v5"] = "market-asset-universe-v5"
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
            has_corporate_valuation=(
                fundamental_pipeline_available
                and asset.security_unit_factor is not None
                and asset.security_unit_basis is not None
                and asset.security_unit_basis_version is not None
                and asset.security_unit_market_adjustment == "all"
            ),
            fundamental_frequencies=fundamental_frequencies,
            fundamental_source_ids=(
                tuple(
                    sorted(
                        (
                            sec_configuration.submissions_source_id,
                            sec_configuration.companyfacts_source_id,
                        )
                    )
                )
                if sec_configuration is not None
                else ()
            ),
            supports_intraday=False,
        )

    if binding.provider == "coinbase" and analysis.crypto_profile is not None:
        daily = resolve_coinbase_configuration(resolver, asset_id=asset_id)
        supports_crypto_derivatives = _supports_crypto_derivatives(resolver, asset_id=asset_id)
        supports_intraday = _MINUTE_MARKET_CAPABILITY in binding.capabilities
        intraday = (
            resolve_coinbase_intraday_configuration(resolver, asset_id=asset_id)
            if supports_intraday
            else None
        )
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
            chart_schema_version=(
                "btc-market-chart-v1"
                if analysis.crypto_profile is CryptoAnalyticalProfile.BITCOIN
                else "crypto-spot-daily-market-chart-v1"
            ),
            volume_unit=daily.base_unit,
            default_market_start=_COINBASE_HISTORY_START,
            analysis=analysis,
            has_fundamentals=False,
            has_corporate_valuation=False,
            fundamental_frequencies=(),
            fundamental_source_ids=(),
            supports_intraday=supports_intraday,
            supports_crypto_derivatives=supports_crypto_derivatives,
            intraday_source_id=intraday.source_id if intraday is not None else None,
            intraday_schema_version=("btc-intraday-chart-v1" if intraday is not None else None),
        )

    raise ValueError(
        f"unsupported default daily-market provider for {asset_id}: {binding.provider}"
    )


def _supports_crypto_derivatives(
    resolver: ProviderAssetContextResolver,
    *,
    asset_id: str,
) -> bool:
    """Return whether the catalog resolves the complete Deribit v1 data family."""
    try:
        resolve_deribit_configuration(resolver, asset_id=asset_id)
    except ProviderAssetContextError:
        return False
    return True


__all__ = [
    "MarketAssetDescriptor",
    "MarketAssetUniverse",
    "build_market_asset_universe",
]
