"""Provider-independent analytical classification derived from the asset catalog."""

from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.core.models import AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr

_MARKET_PREFIX = "market."
_FUNDAMENTAL_PREFIX = "fundamentals."


class AssetAnalysisFamily(StrEnum):
    """Analytical family selected from the economic nature of the instrument."""

    LISTED_COMPANY = "listed_company"
    LISTED_FUND = "listed_fund"
    CRYPTOASSET = "cryptoasset"


class MarketAnalysisMode(StrEnum):
    """Independent market-analysis contract appropriate for one asset family."""

    LISTED_SECURITY = "listed_security"
    CRYPTO_SPOT = "crypto_spot"


class FundamentalAnalysisMode(StrEnum):
    """Fundamental domain whose formulas may be applicable to one asset family."""

    CORPORATE = "corporate"
    INVESTMENT_FUND = "investment_fund"
    CRYPTO_NETWORK = "crypto_network"


_EXPECTED_MODES = {
    AssetClass.EQUITY: (
        AssetAnalysisFamily.LISTED_COMPANY,
        MarketAnalysisMode.LISTED_SECURITY,
        FundamentalAnalysisMode.CORPORATE,
    ),
    AssetClass.ETF: (
        AssetAnalysisFamily.LISTED_FUND,
        MarketAnalysisMode.LISTED_SECURITY,
        FundamentalAnalysisMode.INVESTMENT_FUND,
    ),
    AssetClass.CRYPTO: (
        AssetAnalysisFamily.CRYPTOASSET,
        MarketAnalysisMode.CRYPTO_SPOT,
        FundamentalAnalysisMode.CRYPTO_NETWORK,
    ),
}


class AssetAnalysisCapabilities(ContractModel):
    """Declared data coverage and analytical domains for one catalog asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["asset-analysis-capabilities-v1"] = "asset-analysis-capabilities-v1"
    asset_id: NonEmptyStr
    asset_class: AssetClass
    exchange: NonEmptyStr | None = None
    family: AssetAnalysisFamily
    market_mode: MarketAnalysisMode
    fundamental_mode: FundamentalAnalysisMode
    declared_market_capabilities: tuple[NonEmptyStr, ...]
    declared_fundamental_capabilities: tuple[NonEmptyStr, ...]
    market_data_configured: bool
    fundamental_data_configured: bool

    @field_validator(
        "market_data_configured",
        "fundamental_data_configured",
        mode="before",
    )
    @classmethod
    def require_boolean_flags(cls, value: object) -> object:
        """Reject integers and strings masquerading as configuration state."""
        if not isinstance(value, bool):
            raise ValueError("data configuration flags must be booleans")
        return value

    @model_validator(mode="after")
    def validate_profile(self) -> "AssetAnalysisCapabilities":
        """Keep class, domains, capability namespaces, and flags consistent."""
        expected = _EXPECTED_MODES[self.asset_class]
        if (self.family, self.market_mode, self.fundamental_mode) != expected:
            raise ValueError("analysis domains do not match the asset class")
        _validate_capability_tuple(
            self.declared_market_capabilities,
            prefix=_MARKET_PREFIX,
            label="market",
        )
        _validate_capability_tuple(
            self.declared_fundamental_capabilities,
            prefix=_FUNDAMENTAL_PREFIX,
            label="fundamental",
        )
        if self.market_data_configured != bool(self.declared_market_capabilities):
            raise ValueError("market_data_configured must match declared capabilities")
        if self.fundamental_data_configured != bool(self.declared_fundamental_capabilities):
            raise ValueError("fundamental_data_configured must match declared capabilities")
        return self


def analysis_capabilities_for(asset: CatalogAsset) -> AssetAnalysisCapabilities:
    """Classify one asset without assuming a country, exchange, or provider."""
    declared = {
        capability for binding in asset.provider_bindings for capability in binding.capabilities
    }
    market_capabilities = tuple(
        sorted(item for item in declared if item.startswith(_MARKET_PREFIX))
    )
    fundamental_capabilities = tuple(
        sorted(item for item in declared if item.startswith(_FUNDAMENTAL_PREFIX))
    )
    family, market_mode, fundamental_mode = _EXPECTED_MODES[asset.asset_class]
    return AssetAnalysisCapabilities(
        asset_id=asset.asset_id,
        asset_class=asset.asset_class,
        exchange=asset.exchange,
        family=family,
        market_mode=market_mode,
        fundamental_mode=fundamental_mode,
        declared_market_capabilities=market_capabilities,
        declared_fundamental_capabilities=fundamental_capabilities,
        market_data_configured=bool(market_capabilities),
        fundamental_data_configured=bool(fundamental_capabilities),
    )


def _validate_capability_tuple(
    capabilities: tuple[str, ...],
    *,
    prefix: str,
    label: str,
) -> None:
    if capabilities != tuple(sorted(set(capabilities))):
        raise ValueError(f"declared {label} capabilities must be sorted and unique")
    if any(not capability.startswith(prefix) for capability in capabilities):
        raise ValueError(f"declared {label} capabilities use the wrong namespace")


__all__ = [
    "AssetAnalysisCapabilities",
    "AssetAnalysisFamily",
    "FundamentalAnalysisMode",
    "MarketAnalysisMode",
    "analysis_capabilities_for",
]
