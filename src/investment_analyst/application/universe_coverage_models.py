"""Read-only contracts for catalog capability and local evidence coverage."""

from datetime import date
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import AssetClass
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class CoverageCapability(StrEnum):
    """Whether a capability is declared for an asset."""

    SUPPORTED = "supported"
    NOT_CONFIGURED = "not_configured"
    NOT_APPLICABLE = "not_applicable"


class EvidenceState(StrEnum):
    """Whether a read-only local query found evidence."""

    PRESENT = "present"
    MISSING = "missing"
    NOT_QUERIED = "not_queried"


class UniverseCoverageRequest(ContractModel):
    """A bounded point-in-time request for declared universe coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    market_start: date
    market_end: date
    fundamental_start: date
    fundamental_end: date
    frequency: NonEmptyStr = "annual"
    asset_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, value: str) -> str:
        if value not in {"annual", "quarterly"}:
            raise ValueError("frequency must be annual or quarterly")
        return value

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 100:
            raise ValueError("at most 100 asset_ids are allowed")
        if value != tuple(sorted(set(value))):
            raise ValueError("asset_ids must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_ranges(self) -> "UniverseCoverageRequest":
        if self.market_start > self.market_end:
            raise ValueError("market_start must not be after market_end")
        if self.fundamental_start > self.fundamental_end:
            raise ValueError("fundamental_start must not be after fundamental_end")
        if (self.market_end - self.market_start).days > 365:
            raise ValueError("market range must not exceed 366 inclusive days")
        if (self.fundamental_end - self.fundamental_start).days > 3660:
            raise ValueError("fundamental range must not exceed ten years")
        if self.market_end > self.known_at.date() or self.fundamental_end > self.known_at.date():
            raise ValueError("range end must not be after known_at date")
        return self


class UniverseMarketCoverage(ContractModel):
    """Configured daily source and point-in-time local bar coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: CoverageCapability
    evidence: EvidenceState
    source_id: NonEmptyStr | None = None
    volume_unit: NonEmptyStr | None = None
    history_start: date | None = None
    bar_count: int = Field(ge=0)
    candidate_versions: int = Field(ge=0)
    discarded_revisions: int = Field(ge=0)
    earliest_timestamp: UTCDateTime | None = None
    latest_timestamp: UTCDateTime | None = None
    latest_available_at: UTCDateTime | None = None


class UniverseCoverageAsset(ContractModel):
    """One catalog identity and independent market/fundamental declarations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    asset_class: AssetClass
    exchange: NonEmptyStr
    quote_currency: NonEmptyStr
    market: UniverseMarketCoverage
    fundamentals: CoverageCapability
    corporate_valuation: CoverageCapability
    limitations: tuple[NonEmptyStr, ...]


class UniverseCoverageResult(ContractModel):
    """Deterministic read-only coverage response for one catalog and cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: NonEmptyStr = "universe-coverage-v1"
    catalog_version: int = Field(ge=1)
    catalog_sha256: NonEmptyStr
    request: UniverseCoverageRequest
    assets: tuple[UniverseCoverageAsset, ...]

    @model_validator(mode="after")
    def validate_assets(self) -> "UniverseCoverageResult":
        asset_ids = tuple(item.asset_id for item in self.assets)
        if asset_ids != tuple(sorted(set(asset_ids))):
            raise ValueError("assets must be unique and sorted")
        return self
