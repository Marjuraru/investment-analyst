"""Strict contracts for one incremental Coinbase BTC-USD market refresh."""

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DiagnosticVerdict
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID


class BtcRefreshMode(StrEnum):
    """Requested Coinbase refresh behavior."""

    AUTO = "auto"
    FULL = "full"


class BtcMarketRefreshMode(StrEnum):
    """Resolved Coinbase coverage plan."""

    INITIAL = "initial"
    INCREMENTAL = "incremental"
    ALREADY_CURRENT = "already_current"
    BACKFILL = "backfill"
    FULL = "full"


class BtcMarketDateInterval(ContractModel):
    """One inclusive UTC calendar-date interval requested from Coinbase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Reject datetimes where calendar dates are required."""
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "BtcMarketDateInterval":
        """Require a non-empty inclusive interval."""
        if self.start > self.end:
            raise ValueError("interval start must not be later than end")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit inclusive calendar bounds."""
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


class BtcMarketRefreshPlan(ContractModel):
    """Read-only plan based only on persisted Coinbase daily-candle edges."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    requested_start: date
    requested_end: date
    persisted_earliest: UTCDateTime | None = None
    persisted_latest: UTCDateTime | None = None
    persisted_latest_available_at: UTCDateTime | None = None
    fetch_intervals: tuple[BtcMarketDateInterval, ...]
    mode: BtcMarketRefreshMode
    market_fetch_required: bool
    reason: NonEmptyStr
    traceability_verified: bool

    @field_validator("requested_start", "requested_end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Reject datetimes where calendar dates are required."""
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @field_validator("market_fetch_required", "traceability_verified", mode="before")
    @classmethod
    def require_booleans(cls, value: object, info) -> object:
        """Reject truthy integers and strings as flags."""
        if not isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be a bool")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> "BtcMarketRefreshPlan":
        """Keep range, coverage edges, intervals, and resolved mode coherent."""
        if self.requested_start > self.requested_end:
            raise ValueError("requested_start must not be later than requested_end")
        if (self.persisted_earliest is None) != (self.persisted_latest is None):
            raise ValueError("persisted coverage bounds must be both present or both absent")
        if (self.persisted_earliest is None) != (self.persisted_latest_available_at is None):
            raise ValueError("persisted availability must accompany persisted coverage")
        if (
            self.persisted_earliest is not None
            and self.persisted_latest is not None
            and self.persisted_earliest > self.persisted_latest
        ):
            raise ValueError("persisted_earliest must not exceed persisted_latest")
        if len(self.fetch_intervals) > 2:
            raise ValueError("at most two Coinbase fetch intervals are supported")
        for interval in self.fetch_intervals:
            if interval.start < self.requested_start or interval.end > self.requested_end:
                raise ValueError("fetch intervals must remain inside the requested range")
        for previous, current in zip(self.fetch_intervals, self.fetch_intervals[1:], strict=False):
            if previous.end >= current.start:
                raise ValueError("fetch intervals must be ordered and non-overlapping")
        if self.market_fetch_required != bool(self.fetch_intervals):
            raise ValueError("market_fetch_required must match fetch_intervals")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")

        full = BtcMarketDateInterval(start=self.requested_start, end=self.requested_end)
        if self.mode is BtcMarketRefreshMode.ALREADY_CURRENT and self.fetch_intervals:
            raise ValueError("already_current must not contain fetch intervals")
        if self.mode is BtcMarketRefreshMode.INITIAL and (
            self.fetch_intervals != (full,) or self.persisted_earliest is not None
        ):
            raise ValueError("initial must fetch the complete range without prior coverage")
        if (
            self.mode in {BtcMarketRefreshMode.INCREMENTAL, BtcMarketRefreshMode.FULL}
            and len(self.fetch_intervals) != 1
        ):
            raise ValueError(f"{self.mode.value} must contain one fetch interval")
        if self.mode is BtcMarketRefreshMode.FULL and self.fetch_intervals != (full,):
            raise ValueError("full must fetch the complete requested range")
        if self.mode is BtcMarketRefreshMode.BACKFILL and (
            not self.fetch_intervals or self.fetch_intervals[0].start != self.requested_start
        ):
            raise ValueError("backfill must begin at requested_start")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one compact auditable plan."""
        return {
            "mode": self.mode.value,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "persisted_earliest": (
                self.persisted_earliest.isoformat() if self.persisted_earliest else None
            ),
            "persisted_latest": (
                self.persisted_latest.isoformat() if self.persisted_latest else None
            ),
            "persisted_latest_available_at": (
                self.persisted_latest_available_at.isoformat()
                if self.persisted_latest_available_at
                else None
            ),
            "fetch_intervals": [item.to_json_dict() for item in self.fetch_intervals],
            "market_fetch_required": self.market_fetch_required,
            "reason": self.reason,
            "traceability_verified": self.traceability_verified,
        }


class BtcMarketRefreshRequest(ContractModel):
    """Bounded request for a Coinbase-only market update."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: Literal["crypto:btc-usd"] = ASSET_ID
    market_start: date
    market_end: date
    refresh_mode: BtcRefreshMode = BtcRefreshMode.AUTO
    requested_known_at: UTCDateTime | None = None

    @field_validator("market_start", "market_end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Accept ISO JSON dates while rejecting datetimes."""
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
    def validate_range(self) -> "BtcMarketRefreshRequest":
        """Allow one or more inclusive completed UTC calendar days."""
        if self.market_start > self.market_end:
            raise ValueError("market_start must not be later than market_end")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-safe request."""
        return {
            "asset_id": self.asset_id,
            "market_start": self.market_start.isoformat(),
            "market_end": self.market_end.isoformat(),
            "refresh_mode": self.refresh_mode.value,
            "requested_known_at": (
                self.requested_known_at.isoformat() if self.requested_known_at else None
            ),
        }


class BtcMarketRefreshSummary(ContractModel):
    """Compact outcome of ingestion plus independent market analytics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["btc-market-refresh-v1"] = "btc-market-refresh-v1"
    asset_id: Literal["crypto:btc-usd"] = ASSET_ID
    source_id: Literal["coinbase-exchange:btc-usd:daily-candles"] = SOURCE_ID
    request: BtcMarketRefreshRequest
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
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("refresh counters must be integers")
        return value

    @field_validator("traceability_verified", mode="before")
    @classmethod
    def require_traceability_boolean(cls, value: object) -> object:
        """Reject truthy values and require verified output."""
        if not isinstance(value, bool):
            raise ValueError("traceability_verified must be a bool")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "BtcMarketRefreshSummary":
        """Keep request, plan, execution counts, and cut aligned."""
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
        requested_start = datetime.combine(self.request.market_start, time.min, tzinfo=UTC)
        requested_end = datetime.combine(
            self.request.market_end + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )
        if not requested_start <= self.analytics_start < self.analytics_end <= requested_end:
            raise ValueError("analytics bounds must remain inside the requested interval")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON primitives without raw provider payloads."""
        return {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "request": self.request.to_json_dict(),
            "refresh_plan": self.refresh_plan.to_json_dict(),
            "effective_known_at": self.effective_known_at.isoformat(),
            "analytics_start": self.analytics_start.isoformat(),
            "analytics_end": self.analytics_end.isoformat(),
            "analytics_lookback_days": self.analytics_lookback_days,
            "intervals_executed": self.intervals_executed,
            "candles_received": self.candles_received,
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "missing_intervals": [item.isoformat() for item in self.missing_intervals],
            "metric_results_created": self.metric_results_created,
            "metric_results_reused": self.metric_results_reused,
            "diagnostics_created": self.diagnostics_created,
            "diagnostics_reused": self.diagnostics_reused,
            "diagnostic_verdict": self.diagnostic_verdict.value,
            "market_as_of": self.market_as_of.isoformat() if self.market_as_of else None,
            "traceability_verified": self.traceability_verified,
        }
