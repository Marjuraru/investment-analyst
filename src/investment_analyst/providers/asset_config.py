"""Strict typed provider configurations resolved before client construction."""

from pydantic import ConfigDict, StrictInt, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class ProviderConfigurationError(ValueError):
    """Raised when a resolved provider configuration is inconsistent."""


class AlpacaAssetConfiguration(ContractModel):
    """Fixed market-data identifiers required by the current Alpaca flow."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    feed: NonEmptyStr
    adjustment: NonEmptyStr
    source_id: NonEmptyStr


class CoinbaseAssetConfiguration(ContractModel):
    """Fixed market-data identifiers required by the current Coinbase flow."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    product_id: NonEmptyStr
    source_id: NonEmptyStr
    granularity_seconds: StrictInt

    @field_validator("granularity_seconds")
    @classmethod
    def validate_granularity(cls, value: int) -> int:
        """Require a positive strict integer granularity."""
        if value <= 0:
            raise ValueError("granularity_seconds must be positive")
        return value


class SecAssetConfiguration(ContractModel):
    """Fixed issuer identifiers required by the current Apple SEC flow."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    cik: NonEmptyStr
    ticker: NonEmptyStr
    submissions_source_id: NonEmptyStr
    companyfacts_source_id: NonEmptyStr

    @field_validator("cik")
    @classmethod
    def validate_cik(cls, value: str) -> str:
        """Preserve a ten-digit SEC CIK as text."""
        if len(value) != 10 or not value.isdecimal():
            raise ValueError("SEC CIK must contain exactly ten digits")
        return value

    @model_validator(mode="after")
    def validate_sources(self) -> "SecAssetConfiguration":
        """Require distinct source identifiers for the two SEC datasets."""
        if self.submissions_source_id == self.companyfacts_source_id:
            raise ValueError("SEC source identifiers must be distinct")
        return self
