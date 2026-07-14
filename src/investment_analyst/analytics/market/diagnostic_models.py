"""Strict models for point-in-time market diagnostics."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.core.models import (
    DataQuality,
    DiagnosticResult,
    DiagnosticVerdict,
    MetricResult,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_MAX_WINDOW = 10_000
_RETURN_KEY = "market.history.simple_return_1d"
_SMA_KEY = "market.history.sma"
_VOLATILITY_KEY = "market.history.rolling_daily_volatility"
_RELATIVE_VOLUME_KEY = "market.history.relative_volume"


def _validate_window(value: object, *, minimum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if value > _MAX_WINDOW:
        raise ValueError(f"{name} must not exceed {_MAX_WINDOW}")
    return value


def _reject_decimal_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("diagnostic numeric values must use Decimal")
    return value


DiagnosticDecimal = Annotated[Decimal, BeforeValidator(_reject_decimal_float)]


def _parameter_int(result: MetricResult, name: str) -> int:
    value = result.parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"metric parameter {name!r} must be an integer")
    return value


def _parameter_known_at(result: MetricResult) -> datetime:
    value = result.parameters.get("known_at")
    if not isinstance(value, str):
        raise ValueError("metric parameter 'known_at' must be an ISO-8601 string")
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("metric parameter 'known_at' is not valid ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("metric parameter 'known_at' must include timezone information")
    return parsed.astimezone(UTC)


class MarketDiagnosticRequest(ContractModel):
    """Fixed-version request for one market-condition diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    query: HistoricalBarQuery
    short_sma_window: int = 5
    long_sma_window: int = 20
    volatility_window: int = 20
    relative_volume_window: int = 20

    @field_validator("short_sma_window", "long_sma_window", mode="before")
    @classmethod
    def validate_sma_window(cls, value: object, info) -> int:
        """Validate SMA windows without accepting booleans as integers."""
        return _validate_window(value, minimum=1, name=info.field_name)

    @field_validator("volatility_window", mode="before")
    @classmethod
    def validate_volatility_window(cls, value: object) -> int:
        """Require a valid sample-volatility window."""
        return _validate_window(value, minimum=2, name="volatility_window")

    @field_validator("relative_volume_window", mode="before")
    @classmethod
    def validate_relative_volume_window(cls, value: object) -> int:
        """Require a valid relative-volume baseline window."""
        return _validate_window(value, minimum=1, name="relative_volume_window")

    @model_validator(mode="after")
    def validate_window_order(self) -> "MarketDiagnosticRequest":
        """Require a strictly shorter short-term SMA window."""
        if self.short_sma_window >= self.long_sma_window:
            raise ValueError("short_sma_window must be smaller than long_sma_window")
        return self


class MarketMetricSnapshot(ContractModel):
    """Five compatible persisted metrics selected at one common as-of timestamp."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    known_at: UTCDateTime
    as_of: UTCDateTime
    simple_return: MetricResult
    short_sma: MetricResult
    long_sma: MetricResult
    rolling_volatility: MetricResult
    relative_volume: MetricResult

    @model_validator(mode="after")
    def validate_snapshot(self) -> "MarketMetricSnapshot":
        """Validate common identity, context, keys, units, and metric configurations."""
        ordered = self.metric_results()
        expected_keys = (
            _RETURN_KEY,
            _SMA_KEY,
            _SMA_KEY,
            _VOLATILITY_KEY,
            _RELATIVE_VOLUME_KEY,
        )
        expected_units = ("ratio", "USD", "USD", "ratio", "ratio")
        for result, metric_key, unit in zip(ordered, expected_keys, expected_units, strict=True):
            if result.asset_id != self.asset_id:
                raise ValueError("all snapshot metrics must belong to the same asset")
            if result.as_of != self.as_of:
                raise ValueError("all snapshot metrics must share the same as_of")
            if result.available_at > self.known_at:
                raise ValueError("snapshot metric was not available at known_at")
            if result.metric_key != metric_key:
                raise ValueError("snapshot contains an unexpected metric key")
            if result.unit != unit:
                raise ValueError("snapshot metric has an unexpected unit")
            if result.parameters.get("source_id") != self.source_id:
                raise ValueError("snapshot metric source_id does not match")
            if _parameter_known_at(result) != self.known_at:
                raise ValueError("snapshot metric known_at does not match")

        short_window = _parameter_int(self.short_sma, "window")
        long_window = _parameter_int(self.long_sma, "window")
        volatility_window = _parameter_int(self.rolling_volatility, "window")
        relative_volume_window = _parameter_int(self.relative_volume, "window")
        if short_window >= long_window:
            raise ValueError("snapshot short SMA window must be smaller than long SMA window")
        if volatility_window < 2:
            raise ValueError("snapshot volatility window must be at least 2")
        if relative_volume_window < 1:
            raise ValueError("snapshot relative-volume window must be positive")

        identifiers = self.metric_result_ids()
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("snapshot metric result IDs must be distinct")
        return self

    def metric_results(self) -> tuple[MetricResult, ...]:
        """Return metrics in the deterministic diagnostic order."""
        return (
            self.simple_return,
            self.short_sma,
            self.long_sma,
            self.rolling_volatility,
            self.relative_volume,
        )

    def metric_result_ids(self) -> tuple[UUID, ...]:
        """Return metric IDs in return, short SMA, long SMA, volatility, volume order."""
        return tuple(result.result_id for result in self.metric_results())


class MarketDiagnosticComputation(ContractModel):
    """In-memory result of one deterministic market-diagnostic computation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: MarketDiagnosticRequest
    snapshot: MarketMetricSnapshot | None
    diagnostic: DiagnosticResult
    missing_requirements: tuple[NonEmptyStr, ...]
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_computation(self) -> "MarketDiagnosticComputation":
        """Keep snapshot presence, missing requirements, and verdict consistent."""
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        if self.snapshot is None:
            if self.diagnostic.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA:
                raise ValueError("missing snapshot requires INSUFFICIENT_DATA")
            if not self.missing_requirements:
                raise ValueError("missing snapshot requires missing_requirements")
        else:
            if self.diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
                raise ValueError("complete snapshot cannot produce INSUFFICIENT_DATA")
            if self.missing_requirements:
                raise ValueError("complete snapshot must not report missing requirements")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "request": self.request.model_dump(mode="json"),
            "snapshot": self.snapshot.model_dump(mode="json") if self.snapshot else None,
            "diagnostic": self.diagnostic.model_dump(mode="json"),
            "missing_requirements": list(self.missing_requirements),
            "traceability_verified": self.traceability_verified,
        }


class MarketDiagnosticRunSummary(ContractModel):
    """Auditable summary of one diagnostic persistence run."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    known_at: UTCDateTime
    as_of: UTCDateTime
    computed_at: UTCDateTime
    verdict: DiagnosticVerdict
    final_score: DiagnosticDecimal
    confidence: DiagnosticDecimal
    quality: DataQuality
    selected_metric_result_ids: tuple[UUID, ...]
    missing_requirements: tuple[NonEmptyStr, ...]
    diagnostics_generated: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_summary(self) -> "MarketDiagnosticRunSummary":
        """Validate run counters and diagnostic-state consistency."""
        if not self.final_score.is_finite() or not self.confidence.is_finite():
            raise ValueError("summary scores must be finite")
        if not Decimal("0") <= self.final_score <= Decimal("100"):
            raise ValueError("final_score must be between 0 and 100")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")
        if self.diagnostics_generated != self.diagnostics_created + self.diagnostics_reused:
            raise ValueError("diagnostics_generated must equal created plus reused")
        if self.diagnostics_generated != 1:
            raise ValueError("each run must generate exactly one diagnostic")
        if self.verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
            if self.selected_metric_result_ids:
                raise ValueError("insufficient data must not select metric results")
            if not self.missing_requirements:
                raise ValueError("insufficient data requires missing requirements")
        else:
            if len(self.selected_metric_result_ids) != 5:
                raise ValueError("normal diagnostics require five selected metrics")
            if self.missing_requirements:
                raise ValueError("normal diagnostics must not report missing requirements")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return self.model_dump(mode="json")
