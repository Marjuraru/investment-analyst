"""Catalog-scoped contracts for daily Coinbase spot-market refreshes."""
# ruff: noqa: E501

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshPlan,
    BtcRefreshMode,
)
from investment_analyst.core.models import DiagnosticVerdict
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class CryptoSpotDailyRefreshRequest(ContractModel):
    """One bounded daily Coinbase refresh resolved explicitly from the catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    market_start: date
    market_end: date
    refresh_mode: BtcRefreshMode = BtcRefreshMode.AUTO
    requested_known_at: UTCDateTime | None = None

    @field_validator("market_start", "market_end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        if isinstance(value, datetime):
            raise ValueError(f"{info.field_name} must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{info.field_name} must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "CryptoSpotDailyRefreshRequest":
        if self.market_start > self.market_end:
            raise ValueError("market_start must not be later than market_end")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class CryptoSpotDailyRefreshSummary(ContractModel):
    """Traceable market-only outcome for one non-BTC Coinbase daily source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["crypto-spot-daily-market-refresh-v1"] = (
        "crypto-spot-daily-market-refresh-v1"
    )
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    request: CryptoSpotDailyRefreshRequest
    refresh_plan: BtcMarketRefreshPlan
    effective_known_at: UTCDateTime
    analytics_start: UTCDateTime
    analytics_end: UTCDateTime
    analytics_lookback_days: Literal[90] = 90
    intervals_executed: int = Field(ge=0)
    candles_received: int = Field(ge=0)
    raw_records_created: int = Field(ge=0)
    raw_records_reused: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    missing_intervals: tuple[UTCDateTime, ...]
    metric_results_created: int = Field(ge=0)
    metric_results_reused: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)
    diagnostic_verdict: DiagnosticVerdict
    market_as_of: UTCDateTime | None = None
    traceability_verified: bool

    @field_validator(
        "intervals_executed",
        "candles_received",
        "raw_records_created",
        "raw_records_reused",
        "observations_created",
        "observations_reused",
        "metric_results_created",
        "metric_results_reused",
        "diagnostics_created",
        "diagnostics_reused",
        mode="before",
    )
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("refresh counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "CryptoSpotDailyRefreshSummary":
        if self.asset_id != self.request.asset_id:
            raise ValueError("summary asset_id must match the request")
        if self.refresh_plan.requested_start != self.request.market_start:
            raise ValueError("refresh plan start must match the request")
        if self.refresh_plan.requested_end != self.request.market_end:
            raise ValueError("refresh plan end must match the request")
        if self.intervals_executed != len(self.refresh_plan.fetch_intervals):
            raise ValueError("intervals_executed must match the refresh plan")
        if (
            self.request.requested_known_at is not None
            and self.effective_known_at != self.request.requested_known_at
        ):
            raise ValueError("an explicit known_at must be preserved exactly")
        if self.market_as_of is not None and self.market_as_of > self.effective_known_at:
            raise ValueError("market_as_of must not exceed effective_known_at")
        start = datetime.combine(self.request.market_start, time.min, tzinfo=UTC)
        end = datetime.combine(self.request.market_end + timedelta(days=1), time.min, tzinfo=UTC)
        if not start <= self.analytics_start < self.analytics_end <= end:
            raise ValueError("analytics bounds must remain inside the requested interval")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")
