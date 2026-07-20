"""Strict contracts for bounded point-in-time Apple fundamental trends."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
)
from investment_analyst.providers.fundamentals.sec_query_models import SecFundamentalPeriodView

_HISTORY_START = datetime(1970, 1, 1, tzinfo=UTC)
_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})

FUNDAMENTAL_TREND_LIMITATIONS = (
    "Facts come from official SEC EDGAR Company Facts selected point-in-time.",
    "Quarterly revenue and net income are period values, not trailing-twelve-month values.",
    "Balance-sheet facts and flow facts retain their distinct accounting meanings.",
    "This view does not include valuation, estimates, peer comparison, or recommendations.",
)


class AaplFundamentalTrendRequest(ContractModel):
    """Request a bounded SEC period history at one explicit knowledge cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    frequency: DataFrequency
    period_limit: int = Field(ge=2, le=12)

    @field_validator("known_at")
    @classmethod
    def validate_known_at(cls, value: datetime) -> datetime:
        """Reject cuts outside the supported history horizon."""
        if value <= _HISTORY_START:
            raise ValueError("known_at must be later than 1970-01-01T00:00:00Z")
        return value

    @field_validator("period_limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value: object) -> object:
        """Reject booleans masquerading as bounded integers."""
        if isinstance(value, bool):
            raise ValueError("period_limit must be an integer between 2 and 12")
        return value

    @model_validator(mode="after")
    def validate_frequency(self) -> "AaplFundamentalTrendRequest":
        """Keep the public trend scope annual or quarterly."""
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        return self


class AaplFundamentalTrendCoverage(ContractModel):
    """Read-only selection counts retained from the SEC point-in-time query."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observations_examined: int = Field(ge=0)
    observations_eligible: int = Field(ge=0)
    observations_selected: int = Field(ge=0)
    observations_superseded: int = Field(ge=0)
    periods_returned: int = Field(ge=0, le=12)
    earliest_period_end: UTCDateTime | None = None
    latest_period_end: UTCDateTime | None = None
    latest_period_complete: bool

    @model_validator(mode="after")
    def validate_bounds(self) -> "AaplFundamentalTrendCoverage":
        """Keep empty and non-empty coverage bounds unambiguous."""
        bounds = (self.earliest_period_end, self.latest_period_end)
        if self.periods_returned == 0 and bounds != (None, None):
            raise ValueError("empty fundamental coverage must not define period bounds")
        if self.periods_returned > 0 and None in bounds:
            raise ValueError("non-empty fundamental coverage requires period bounds")
        if (
            self.earliest_period_end is not None
            and self.latest_period_end is not None
            and self.earliest_period_end > self.latest_period_end
        ):
            raise ValueError("fundamental coverage period bounds are reversed")
        return self


class AaplFundamentalTrend(ContractModel):
    """Versioned exact-data contract for the local fundamental trend view."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["aapl-fundamental-trend-v1"] = "aapl-fundamental-trend-v1"
    asset_id: Literal["equity:us:aapl"] = ASSET_ID
    source_id: Literal["sec-edgar:aapl:companyfacts"] = COMPANYFACTS_SOURCE_ID
    request: AaplFundamentalTrendRequest
    periods: tuple[SecFundamentalPeriodView, ...]
    coverage: AaplFundamentalTrendCoverage
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...] = FUNDAMENTAL_TREND_LIMITATIONS

    @model_validator(mode="after")
    def validate_trend(self) -> "AaplFundamentalTrend":
        """Validate boundedness, order, point-in-time availability, and source scope."""
        if len(self.periods) != self.coverage.periods_returned:
            raise ValueError("fundamental period count must match coverage")
        if len(self.periods) > self.request.period_limit:
            raise ValueError("fundamental period count exceeds the requested limit")
        period_ends = tuple(period.period_end for period in self.periods)
        if period_ends != tuple(sorted(period_ends)) or len(period_ends) != len(set(period_ends)):
            raise ValueError("fundamental periods must be ordered and unique")
        for period in self.periods:
            if period.frequency is not self.request.frequency:
                raise ValueError("fundamental period frequency does not match the request")
            if period.period_end > self.request.known_at:
                raise ValueError("fundamental period is later than known_at")
            for fact in period.facts:
                if fact.source_id != self.source_id:
                    raise ValueError("fundamental fact uses an unsupported source")
                if fact.available_at > self.request.known_at:
                    raise ValueError("fundamental fact was unavailable at known_at")
        if period_ends:
            if self.coverage.earliest_period_end != period_ends[0]:
                raise ValueError("fundamental earliest period does not match coverage")
            if self.coverage.latest_period_end != period_ends[-1]:
                raise ValueError("fundamental latest period does not match coverage")
            if self.coverage.latest_period_complete != self.periods[-1].is_complete:
                raise ValueError("fundamental latest-period completeness does not match coverage")
        elif self.coverage.latest_period_complete:
            raise ValueError("empty fundamental coverage cannot report a complete latest period")
        if self.limitations != FUNDAMENTAL_TREND_LIMITATIONS:
            raise ValueError("fundamental trend limitations must preserve the versioned contract")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact JSON contract while preserving exact fact values."""
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "known_at": self.request.known_at.isoformat(),
            "frequency": self.request.frequency.value,
            "period_limit": self.request.period_limit,
            "periods": [period.to_json_dict() for period in self.periods],
            "coverage": self.coverage.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
            "limitations": list(self.limitations),
        }


__all__ = [
    "AaplFundamentalTrend",
    "AaplFundamentalTrendCoverage",
    "AaplFundamentalTrendRequest",
    "FUNDAMENTAL_TREND_LIMITATIONS",
]
