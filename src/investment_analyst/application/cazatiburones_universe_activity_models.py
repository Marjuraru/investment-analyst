"""Contracts for the point-in-time Cazatiburones universe activity index."""

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class CazatiburonesUniverseActivityRequest(ContractModel):
    """Bounded point-in-time selection for the independent activity families."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    asset_ids: tuple[NonEmptyStr, ...] = ()

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 40:
            raise ValueError("at most 40 asset_ids are allowed")
        if value != tuple(sorted(set(value))):
            raise ValueError("asset_ids must be unique and sorted")
        return value


class CazatiburonesUniverseActivityFamily(ContractModel):
    """Evidence state for one activity family without cross-family aggregation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: CoverageCapability
    evidence: EvidenceState
    statements: int = Field(ge=0)
    latest_available_at: UTCDateTime | None = None
    latest_age_days: int | None = Field(default=None, ge=0)
    not_evaluable_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_evidence_grammar(self) -> "CazatiburonesUniverseActivityFamily":
        if self.evidence is EvidenceState.PRESENT and (
            self.statements == 0 or self.latest_available_at is None
        ):
            raise ValueError("present evidence requires statements and latest_available_at")
        if self.evidence is EvidenceState.MISSING and (
            self.statements != 0
            or self.latest_available_at is not None
            or self.latest_age_days is not None
            or self.not_evaluable_reason is not None
        ):
            raise ValueError("missing evidence requires zero statements and no reason")
        if self.not_evaluable_reason is not None and self.evidence is not EvidenceState.NOT_QUERIED:
            raise ValueError("not_evaluable_reason requires not_queried evidence")
        if self.evidence is EvidenceState.NOT_QUERIED and (
            self.statements != 0
            or self.latest_available_at is not None
            or self.latest_age_days is not None
        ):
            raise ValueError("not_queried evidence cannot contain statements")
        return self


class CazatiburonesUniverseActivityAsset(ContractModel):
    """One catalog asset with three independent activity-family views."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    symbol: NonEmptyStr
    name: NonEmptyStr
    insider: CazatiburonesUniverseActivityFamily
    beneficial: CazatiburonesUniverseActivityFamily
    institutional: CazatiburonesUniverseActivityFamily
    limitations: tuple[NonEmptyStr, ...]


class CazatiburonesUniverseActivityResult(ContractModel):
    """Versioned universe activity response for one catalog and information cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: NonEmptyStr = "cazatiburones-universe-activity-v1"
    catalog_version: int = Field(ge=1)
    catalog_sha256: NonEmptyStr
    request: CazatiburonesUniverseActivityRequest
    assets: tuple[CazatiburonesUniverseActivityAsset, ...]

    @model_validator(mode="after")
    def validate_assets(self) -> "CazatiburonesUniverseActivityResult":
        asset_ids = tuple(item.asset_id for item in self.assets)
        if asset_ids != tuple(sorted(set(asset_ids))):
            raise ValueError("assets must be unique and sorted")
        return self
