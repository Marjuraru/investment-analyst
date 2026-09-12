"""Strict shared contracts for Coinbase incremental market refresh planning."""

from datetime import date, datetime
from enum import StrEnum

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


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
