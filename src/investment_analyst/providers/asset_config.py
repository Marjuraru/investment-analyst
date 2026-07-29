"""Strict typed provider configurations resolved before client construction."""

import re

from pydantic import ConfigDict, StrictInt, field_validator, model_validator

from investment_analyst.core.models import AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class ProviderConfigurationError(ValueError):
    """Raised when a resolved provider configuration is inconsistent."""


_SEC_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")


def sec_source_ids(ticker: str) -> tuple[str, str]:
    """Derive deterministic issuer-specific SEC source IDs."""
    if ticker != ticker.strip() or not _SEC_TICKER_PATTERN.fullmatch(ticker):
        raise ProviderConfigurationError(
            "SEC ticker must use upper-case letters, digits, dot, or dash"
        )
    slug = ticker.casefold()
    return (
        f"sec-edgar:{slug}:submissions",
        f"sec-edgar:{slug}:companyfacts",
    )


class AlpacaAssetConfiguration(ContractModel):
    """Catalog-backed identifiers and metadata required by one Alpaca asset."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    feed: NonEmptyStr
    adjustment: NonEmptyStr
    source_id: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    exchange: NonEmptyStr


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
    """Catalog-backed identifiers and metadata required by one SEC issuer."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    cik: NonEmptyStr
    ticker: NonEmptyStr
    submissions_source_id: NonEmptyStr
    companyfacts_source_id: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    exchange: NonEmptyStr

    @field_validator("cik")
    @classmethod
    def validate_cik(cls, value: str) -> str:
        """Preserve a ten-digit SEC CIK as text."""
        if len(value) != 10 or not value.isdecimal():
            raise ValueError("SEC CIK must contain exactly ten digits")
        return value

    @model_validator(mode="after")
    def validate_sources(self) -> "SecAssetConfiguration":
        """Keep corporate scope and issuer-specific source identities explicit."""
        if self.asset_class is not AssetClass.EQUITY:
            raise ValueError("SEC corporate configuration requires an equity asset")
        expected_submissions, expected_companyfacts = sec_source_ids(self.ticker)
        if self.submissions_source_id != expected_submissions:
            raise ValueError("SEC submissions source_id does not match the configured ticker")
        if self.companyfacts_source_id != expected_companyfacts:
            raise ValueError("SEC companyfacts source_id does not match the configured ticker")
        if self.submissions_source_id == self.companyfacts_source_id:
            raise ValueError("SEC source identifiers must be distinct")
        return self
