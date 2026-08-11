"""Strict typed provider configurations resolved before client construction."""

import re
from datetime import date, datetime
from enum import StrEnum

from pydantic import ConfigDict, StrictInt, field_validator, model_validator

from investment_analyst.core.models import AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr


class ProviderConfigurationError(ValueError):
    """Raised when a resolved provider configuration is inconsistent."""


_SEC_TICKER_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
_COINBASE_PRODUCT_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
_DERIBIT_CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{2,16}$")
_DERIBIT_PERPETUAL_PATTERN = re.compile(r"^[A-Z0-9]{2,16}-PERPETUAL$")


class SecAccountingStandard(StrEnum):
    """Supported issuer taxonomy and filing profile declared by the catalog."""

    US_GAAP = "us-gaap"
    IFRS = "ifrs-full"


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


def coinbase_source_id(product_id: str, granularity_seconds: int) -> str:
    """Derive a provider/product/granularity source identity without symbol branching."""
    if product_id != product_id.strip() or not _COINBASE_PRODUCT_PATTERN.fullmatch(product_id):
        raise ProviderConfigurationError("Coinbase product_id must use BASE-QUOTE")
    if isinstance(granularity_seconds, bool) or granularity_seconds <= 0:
        raise ProviderConfigurationError("Coinbase granularity must be a positive integer")
    slug = product_id.casefold()
    suffix = (
        "daily-candles"
        if granularity_seconds == 86_400
        else f"minute-{granularity_seconds // 60}-candles"
        if granularity_seconds % 60 == 0
        else f"second-{granularity_seconds}-candles"
    )
    return f"coinbase-exchange:{slug}:{suffix}"


def deribit_source_ids(currency: str, instrument_name: str) -> tuple[str, str, str]:
    """Derive the three immutable Deribit source IDs for one perpetual."""
    if currency != currency.strip() or not _DERIBIT_CURRENCY_PATTERN.fullmatch(currency):
        raise ProviderConfigurationError("Deribit currency must use upper-case letters or digits")
    if (
        instrument_name != instrument_name.strip()
        or not _DERIBIT_PERPETUAL_PATTERN.fullmatch(instrument_name)
        or instrument_name != f"{currency}-PERPETUAL"
    ):
        raise ProviderConfigurationError("Deribit instrument_name must match CURRENCY-PERPETUAL")
    slug = currency.casefold()
    perpetual_slug = instrument_name.casefold()
    return (
        f"deribit:{perpetual_slug}:funding-rate-history",
        f"deribit:{slug}:dvol:daily",
        f"deribit:{perpetual_slug}:book-summary",
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
    history_start: date | None = None

    @field_validator("history_start", mode="before")
    @classmethod
    def validate_history_start(cls, value: object) -> object:
        """Accept one explicit provider-safe ISO date without datetime coercion."""
        if value is None:
            return None
        if isinstance(value, datetime):
            raise ValueError("Alpaca history_start must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("Alpaca history_start must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError("Alpaca history_start must be a date")
        return value


class CoinbaseAssetConfiguration(ContractModel):
    """Catalog-backed Coinbase product, units, and granularity contract."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    product_id: NonEmptyStr
    source_id: NonEmptyStr
    granularity_seconds: StrictInt
    base_unit: NonEmptyStr
    quote_unit: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    exchange: NonEmptyStr

    @field_validator("granularity_seconds")
    @classmethod
    def validate_granularity(cls, value: int) -> int:
        """Require a positive strict integer granularity."""
        if value <= 0:
            raise ValueError("granularity_seconds must be positive")
        return value

    @model_validator(mode="after")
    def validate_product_contract(self) -> "CoinbaseAssetConfiguration":
        """Keep declared units and source identity aligned with the product."""
        parts = self.product_id.split("-")
        if len(parts) != 2 or any(not part for part in parts):
            raise ValueError("Coinbase product_id must use BASE-QUOTE")
        if (self.base_unit, self.quote_unit) != (parts[0], parts[1]):
            raise ValueError("Coinbase units must match product_id")
        if self.source_id != coinbase_source_id(self.product_id, self.granularity_seconds):
            raise ValueError("Coinbase source_id must match product and granularity")
        if self.asset_class is not AssetClass.CRYPTO:
            raise ValueError("Coinbase candle configuration requires a crypto asset")
        if self.quote_currency != self.quote_unit:
            raise ValueError("Coinbase quote_currency must match product quote unit")
        return self


class DeribitAssetConfiguration(ContractModel):
    """Catalog-backed Deribit currency, perpetual, units, and source identities."""

    model_config = ConfigDict(frozen=True)

    asset_id: NonEmptyStr
    currency: NonEmptyStr
    instrument_name: NonEmptyStr
    funding_source_id: NonEmptyStr
    dvol_source_id: NonEmptyStr
    summary_source_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    quote_currency: NonEmptyStr
    exchange: NonEmptyStr
    provider_symbols: dict[NonEmptyStr, NonEmptyStr]

    @model_validator(mode="after")
    def validate_deribit_contract(self) -> "DeribitAssetConfiguration":
        """Keep catalog identifiers, sources, and v1 units aligned."""
        expected = deribit_source_ids(self.currency, self.instrument_name)
        actual = (
            self.funding_source_id,
            self.dvol_source_id,
            self.summary_source_id,
        )
        if actual != expected:
            raise ValueError("Deribit source IDs must match currency and perpetual instrument")
        if self.asset_class is not AssetClass.CRYPTO:
            raise ValueError("Deribit derivatives configuration requires a crypto asset")
        if self.symbol != self.currency:
            raise ValueError("Deribit currency must match the canonical asset symbol")
        if self.quote_currency != "USD":
            raise ValueError("Deribit derivatives v1 requires USD quote currency")
        return self


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
    accounting_standard: SecAccountingStandard = SecAccountingStandard.US_GAAP

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
        if self.quote_currency != "USD":
            raise ValueError("current SEC fact profiles require USD issuer reporting")
        return self

    @property
    def supported_forms(self) -> frozenset[str]:
        """Return filing forms eligible under the declared accounting profile."""
        if self.accounting_standard is SecAccountingStandard.IFRS:
            return frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})
        return frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})

    @property
    def supported_frequencies(self) -> tuple[str, ...]:
        """Return deterministic analytical frequencies exposed by this profile."""
        if self.accounting_standard is SecAccountingStandard.IFRS:
            return ("annual",)
        return ("annual", "quarterly")
