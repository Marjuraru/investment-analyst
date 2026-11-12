"""Ephemeral presentation contract joining independent compact domain projections."""

from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

INVESTOR_OVERVIEW_SCHEMA_VERSION = "investor-overview-v1"
MAX_INVESTOR_OVERVIEW_ASSETS = 10
MARKET_WINDOW_DAYS = 365
FUNDAMENTAL_WINDOW_DAYS = 3660
MAX_INVESTOR_OVERVIEW_BYTES = 262_144


class InvestorValueState(StrEnum):
    """Closed availability states; integrity failures are errors, never states."""

    AVAILABLE = "available"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"
    NOT_CONFIGURED = "not_configured"
    NOT_SUPPORTED = "not_supported"
    INSUFFICIENT_DATA = "insufficient_data"


class InvestorReferences(ContractModel):
    """Typed identities of the persisted evidence behind one presented value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostic_id: UUID | None = None
    metric_result_id: UUID | None = None
    raw_record_id: UUID | None = None
    observation_ids: tuple[UUID, ...] = ()
    filing_accession_number: NonEmptyStr | None = None


class InvestorValue(ContractModel):
    """One exact Decimal-string value, or an explicit state with its reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: InvestorValueState
    value: Decimal | None = None
    unit: NonEmptyStr | None = None
    reason_codes: tuple[NonEmptyStr, ...] = ()
    reference_at: UTCDateTime | None = None
    available_at: UTCDateTime | None = None
    references: InvestorReferences = Field(default_factory=InvestorReferences)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("investor values must be finite")
        return value

    @model_validator(mode="after")
    def state_is_exact(self) -> "InvestorValue":
        if self.state is InvestorValueState.AVAILABLE:
            if self.value is None or self.unit is None:
                raise ValueError("an available value requires value and unit")
        elif self.value is not None:
            raise ValueError("an unavailable value must not carry a number")
        return self


class InvestorOverviewRequest(ContractModel):
    """One bounded point-in-time request for between one and ten catalog assets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    asset_ids: tuple[NonEmptyStr, ...]

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not 1 <= len(value) <= MAX_INVESTOR_OVERVIEW_ASSETS:
            raise ValueError(f"between 1 and {MAX_INVESTOR_OVERVIEW_ASSETS} asset_ids are required")
        if value != tuple(sorted(set(value))):
            raise ValueError("asset_ids must be unique and sorted")
        return value

    @property
    def market_end(self) -> date:
        return self.known_at.date() - timedelta(days=1)

    @property
    def market_start(self) -> date:
        return self.market_end - timedelta(days=MARKET_WINDOW_DAYS)

    @property
    def fundamental_start(self) -> date:
        return self.market_end - timedelta(days=FUNDAMENTAL_WINDOW_DAYS)


class InvestorOverviewWindows(ContractModel):
    """The exact visible windows used for this projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market_start: date
    market_end: date
    fundamental_start: date
    fundamental_end: date
    fundamental_frequency: Literal["annual"] = "annual"


class InvestorMarketView(ContractModel):
    """Last validated daily close and its one-day change when already derived."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    close: InvestorValue
    change_1d: InvestorValue
    currency: NonEmptyStr
    source_id: NonEmptyStr | None = None
    interval: NonEmptyStr = "1d"


class InvestorFundamentalView(ContractModel):
    """Independent annual fundamental diagnostic and two source ratios."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: InvestorValue
    verdict: NonEmptyStr | None = None
    algorithm_version: NonEmptyStr | None = None
    net_margin: InvestorValue
    liabilities_to_assets: InvestorValue


class InvestorValuationView(ContractModel):
    """Latest-annual valuation ratios of the canonical snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: NonEmptyStr | None = None
    basis: Literal["latest_annual"] = "latest_annual"
    annual_period_end: UTCDateTime | None = None
    price_date: UTCDateTime | None = None
    price_currency: NonEmptyStr | None = None
    price_to_earnings: InvestorValue
    price_to_book: InvestorValue
    enterprise_value_to_ebitda: InvestorValue


class InvestorOverviewAsset(ContractModel):
    """One catalog identity with independent market, fundamental and valuation views."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    exchange: NonEmptyStr
    quote_currency: NonEmptyStr
    market: InvestorMarketView
    fundamental: InvestorFundamentalView
    valuation: InvestorValuationView
    limitations: tuple[NonEmptyStr, ...] = ()


class InvestorOverviewResult(ContractModel):
    """Deterministic read-only presentation payload; it is never persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["investor-overview-v1"] = INVESTOR_OVERVIEW_SCHEMA_VERSION
    known_at: UTCDateTime
    catalog_version: int = Field(ge=1)
    catalog_sha256: NonEmptyStr
    windows: InvestorOverviewWindows
    assets: tuple[InvestorOverviewAsset, ...]

    @model_validator(mode="after")
    def validate_assets(self) -> "InvestorOverviewResult":
        asset_ids = tuple(item.asset_id for item in self.assets)
        if asset_ids != tuple(sorted(set(asset_ids))):
            raise ValueError("assets must be unique and sorted")
        return self
