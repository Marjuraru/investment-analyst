"""Strict contracts for descriptive forward-outcome reviews of screening conditions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BeforeValidator, ConfigDict, Field, JsonValue, field_validator, model_validator

from investment_analyst.alerts.analytical_models import (
    AnalyticalConditionOperator,
    AnalyticalConditionState,
)
from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models import AssetClass, DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

DESCRIPTIVE_CONDITION_OUTCOMES_SCHEMA = "descriptive-condition-outcomes-v1"
DESCRIPTIVE_CONDITION_OUTCOMES_REQUEST_SCHEMA = "descriptive-condition-outcomes-request-v1"
FORWARD_CLOSE_INTERVALS_HORIZON = "forward_observed_close_intervals_5-v1"
MAX_DESCRIPTIVE_CALENDAR_DAYS = 90
MAX_DESCRIPTIVE_OUTCOME_BYTES = 262_144
_FORWARD_INTERVALS = 5
_FORWARD_CLOSES = 6
_FORWARD_WINDOW_DAYS = 30

_DESCRIPTIVE_OUTCOMES_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "investment-analyst/descriptive-condition-outcomes-v1",
)


def _reject_decimal_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("descriptive outcome numeric values must use Decimal")
    return value


OutcomeDecimal = Annotated[Decimal, BeforeValidator(_reject_decimal_float)]


class ConditionEvidenceReason(StrEnum):
    """Typed causes for one condition without evaluable evidence."""

    NO_COMPATIBLE_METRIC = "no_compatible_metric"
    AMBIGUOUS_METRIC = "ambiguous_metric"
    INVALID_METRIC = "invalid_metric"


class DescriptiveOutcomeStatus(StrEnum):
    """Fixed status vocabulary for one forward observed-close review."""

    AVAILABLE = "available"
    PENDING_WINDOW = "pending_window"
    INSUFFICIENT_OBSERVED_BARS = "insufficient_observed_bars"
    AMBIGUOUS_CLOSE = "ambiguous_close"
    INVALID_CLOSE = "invalid_close"
    CONDITION_NOT_EVALUABLE = "condition_not_evaluable"


class DescriptiveOutcomeGroup(StrEnum):
    """Exact partition of observed periods by their condition state."""

    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    NOT_EVALUABLE = "not_evaluable"


def _utc_date_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


class DescriptiveOutcomeRequest(ContractModel):
    """One bounded request for descriptive condition outcomes."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["descriptive-condition-outcomes-request-v1"] = (
        DESCRIPTIVE_CONDITION_OUTCOMES_REQUEST_SCHEMA
    )
    asset_id: NonEmptyStr
    rule_id: NonEmptyStr
    expected_rule_fingerprint: str
    start_date: date
    end_date: date
    known_at: UTCDateTime

    @field_validator("expected_rule_fingerprint")
    @classmethod
    def require_sha256_fingerprint(cls, value: str) -> str:
        """Require the exact lowercase SHA-256 text used by the rule registry."""
        normalized = value.strip()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("expected_rule_fingerprint must be a lowercase SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def validate_bounded_range(self) -> DescriptiveOutcomeRequest:
        """Keep the public inclusive range and the analytical cut bounded."""
        if self.end_date < self.start_date:
            raise ValueError("end_date must not predate start_date")
        if self.requested_calendar_days > MAX_DESCRIPTIVE_CALENDAR_DAYS:
            raise ValueError("descriptive outcome range is limited to 90 calendar days")
        end_start = _utc_date_start(self.end_date)
        if self.known_at < end_start:
            raise ValueError("known_at must not predate the requested end_date")
        if self.known_at >= end_start + timedelta(days=MAX_DESCRIPTIVE_CALENDAR_DAYS + 1):
            raise ValueError("known_at is too far after the requested end_date")
        return self

    @property
    def start_utc(self) -> datetime:
        """Return the half-open start of the public inclusive range."""
        return _utc_date_start(self.start_date)

    @property
    def end_before_utc(self) -> datetime:
        """Return the half-open end of the public inclusive range."""
        return _utc_date_start(self.end_date + timedelta(days=1))

    @property
    def requested_calendar_days(self) -> int:
        """Return the inclusive number of requested calendar dates."""
        return (self.end_date - self.start_date).days + 1


class DescriptiveCloseObservation(ContractModel):
    """One selected observed close used by the forward interval projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: UUID
    period_start: UTCDateTime
    period_end: UTCDateTime
    observed_at: UTCDateTime
    available_at: UTCDateTime
    close: OutcomeDecimal
    quality: DataQuality

    @model_validator(mode="after")
    def validate_close_interval(self) -> DescriptiveCloseObservation:
        """Keep the interval coherent; value validity is reported by the outcome."""
        if self.period_start >= self.period_end:
            raise ValueError("close period_start must predate period_end")
        if not self.close.is_finite():
            raise ValueError("descriptive close values must be finite")
        return self


class DescriptiveConditionEvidence(ContractModel):
    """Tri-valued condition evidence with its exact metric provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    state: AnalyticalConditionState
    reason: ConditionEvidenceReason | None = None
    metric_key: NonEmptyStr
    operator: AnalyticalConditionOperator
    threshold: OutcomeDecimal
    retention_threshold: OutcomeDecimal
    unit: NonEmptyStr
    metric_result_id: UUID | None = None
    as_of: UTCDateTime | None = None
    available_at: UTCDateTime | None = None
    computed_at: UTCDateTime | None = None
    algorithm_version: NonEmptyStr | None = None
    parameters: dict[NonEmptyStr, JsonValue] = Field(default_factory=dict)
    quality: DataQuality | None = None
    observed_value: OutcomeDecimal | None = None
    screening_result_id: UUID | None = None
    explanation_es: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_condition_evidence(self) -> DescriptiveConditionEvidence:
        """Keep absent evidence explicit and evaluated evidence complete."""
        if self.state is AnalyticalConditionState.NOT_EVALUABLE:
            if self.reason is None:
                raise ValueError("not_evaluable conditions require a typed reason")
            if self.screening_result_id is not None or self.explanation_es is not None:
                raise ValueError("not_evaluable conditions cannot claim a screening result")
            if any(
                value is not None
                for value in (
                    self.metric_result_id,
                    self.as_of,
                    self.available_at,
                    self.computed_at,
                    self.algorithm_version,
                    self.quality,
                    self.observed_value,
                )
            ):
                raise ValueError("not_evaluable conditions cannot claim metric evidence")
            if self.parameters:
                raise ValueError("not_evaluable conditions cannot claim metric parameters")
            return self
        if self.reason is not None:
            raise ValueError("evaluated conditions must not carry an absence reason")
        if any(
            value is None
            for value in (
                self.metric_result_id,
                self.as_of,
                self.available_at,
                self.computed_at,
                self.algorithm_version,
                self.quality,
                self.observed_value,
                self.screening_result_id,
                self.explanation_es,
            )
        ):
            raise ValueError("evaluated conditions require complete metric provenance")
        if self.observed_value is None or not self.observed_value.is_finite():
            raise ValueError("evaluated conditions require a finite observed value")
        expected = (
            AnalyticalConditionState.MET
            if self.operator.compare(self.observed_value, self.threshold)
            else AnalyticalConditionState.NOT_MET
        )
        if self.state is not expected:
            raise ValueError("condition state does not match its exact comparison")
        return self


class DescriptiveOutcomeEvidence(ContractModel):
    """Forward five-interval evidence without executable-return claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DescriptiveOutcomeStatus
    reason: NonEmptyStr | None = None
    return_5_intervals: OutcomeDecimal | None = None
    observed_intervals: int = Field(ge=0, le=_FORWARD_INTERVALS)
    closes: tuple[DescriptiveCloseObservation, ...] = Field(
        default=(),
        max_length=_FORWARD_CLOSES,
    )
    elapsed_seconds: int | None = None
    bar_separation_seconds: tuple[int, ...] = ()
    evidence_available_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_outcome_evidence(self) -> DescriptiveOutcomeEvidence:
        """Keep status, interval count, rows, and return representation consistent."""
        if self.observed_intervals != max(len(self.closes) - 1, 0):
            raise ValueError("observed_intervals must match the selected closes")
        separations = tuple(
            int(
                (
                    self.closes[index].period_start - self.closes[index - 1].period_start
                ).total_seconds()
            )
            for index in range(1, len(self.closes))
        )
        if self.bar_separation_seconds != separations:
            raise ValueError("bar_separation_seconds must match the selected closes")
        if len(self.closes) >= 2:
            expected_elapsed = int(
                (self.closes[-1].period_start - self.closes[0].period_start).total_seconds()
            )
            if self.elapsed_seconds != expected_elapsed:
                raise ValueError("elapsed_seconds must match the selected closes")
        elif self.elapsed_seconds is not None:
            raise ValueError("fewer than two closes cannot claim elapsed_seconds")
        if self.status is DescriptiveOutcomeStatus.AVAILABLE:
            if len(self.closes) != _FORWARD_CLOSES:
                raise ValueError("available outcomes require six observed closes")
            if self.return_5_intervals is None:
                raise ValueError("available outcomes require the five-interval return")
            if not self.return_5_intervals.is_finite():
                raise ValueError("available outcome returns must be finite")
            if self.evidence_available_at is None:
                raise ValueError("available outcomes require evidence availability")
        else:
            if self.return_5_intervals is not None:
                raise ValueError("non-available outcomes cannot claim a return")
        if self.status is DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE and (
            self.closes or self.observed_intervals or self.evidence_available_at is not None
        ):
            raise ValueError("condition_not_evaluable outcomes cannot claim close evidence")
        return self

    @property
    def has_return(self) -> bool:
        """Return whether this outcome contributes one observed return."""
        return self.status is DescriptiveOutcomeStatus.AVAILABLE


class DescriptivePeriodOutcome(ContractModel):
    """One observed daily condition period and its forward outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period_as_of: UTCDateTime
    group: DescriptiveOutcomeGroup
    condition: DescriptiveConditionEvidence
    outcome: DescriptiveOutcomeEvidence

    @model_validator(mode="after")
    def validate_group_partition(self) -> DescriptivePeriodOutcome:
        """Keep the group a pure projection of the tri-valued condition."""
        expected = {
            AnalyticalConditionState.MET: DescriptiveOutcomeGroup.MATCHED,
            AnalyticalConditionState.NOT_MET: DescriptiveOutcomeGroup.NOT_MATCHED,
            AnalyticalConditionState.NOT_EVALUABLE: DescriptiveOutcomeGroup.NOT_EVALUABLE,
        }[self.condition.state]
        if self.group is not expected:
            raise ValueError("period group does not match its condition state")
        if self.condition.state is AnalyticalConditionState.NOT_EVALUABLE:
            if self.outcome.status is not DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE:
                raise ValueError(
                    "not_evaluable conditions require condition_not_evaluable outcomes"
                )
        elif self.outcome.status is DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE:
            raise ValueError("evaluated conditions cannot claim condition_not_evaluable outcomes")
        if self.condition.as_of is not None and self.condition.as_of != self.period_as_of:
            raise ValueError("period_as_of must match its condition evidence")
        return self


class DescriptiveGroupSummary(ContractModel):
    """Exact counts and observed return summaries for one condition group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group: DescriptiveOutcomeGroup
    denominator: int = Field(ge=0)
    available_count: int = Field(ge=0)
    pending_window_count: int = Field(ge=0)
    insufficient_observed_bars_count: int = Field(ge=0)
    ambiguous_close_count: int = Field(ge=0)
    invalid_close_count: int = Field(ge=0)
    condition_not_evaluable_count: int = Field(ge=0)
    positives: int = Field(ge=0)
    negatives: int = Field(ge=0)
    zeros: int = Field(ge=0)
    mean_return_5_intervals: OutcomeDecimal | None = None
    median_return_5_intervals: OutcomeDecimal | None = None

    @model_validator(mode="after")
    def validate_summary(self) -> DescriptiveGroupSummary:
        """Derive every count and the empty-group absence from the exact totals."""
        counted = (
            self.available_count
            + self.pending_window_count
            + self.insufficient_observed_bars_count
            + self.ambiguous_close_count
            + self.invalid_close_count
            + self.condition_not_evaluable_count
        )
        if counted != self.denominator:
            raise ValueError("group status counts must partition the denominator")
        if self.positives + self.negatives + self.zeros != self.available_count:
            raise ValueError("return signs must partition available outcomes")
        empty = self.available_count == 0
        if (self.mean_return_5_intervals is None) != empty:
            raise ValueError("empty groups require null mean returns")
        if (self.median_return_5_intervals is None) != empty:
            raise ValueError("empty groups require null median returns")
        if self.group is DescriptiveOutcomeGroup.NOT_EVALUABLE:
            if self.condition_not_evaluable_count != self.denominator:
                raise ValueError("not_evaluable groups require every period not evaluable")
            if self.available_count:
                raise ValueError("not_evaluable groups cannot contain observed returns")
        return self


def _mean(values: Sequence[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        return sum(values, Decimal("0")) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    with localcontext() as context:
        context.prec = 34
        context.rounding = ROUND_HALF_EVEN
        return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def summarize_descriptive_rows(
    rows: Sequence[DescriptivePeriodOutcome],
) -> tuple[DescriptiveGroupSummary, ...]:
    """Return the exact three-group partition for ordered period outcomes."""
    summaries: list[DescriptiveGroupSummary] = []
    for group in DescriptiveOutcomeGroup:
        selected = tuple(item for item in rows if item.group is group)
        returns = tuple(
            item.outcome.return_5_intervals
            for item in selected
            if item.outcome.return_5_intervals is not None
        )
        summaries.append(
            DescriptiveGroupSummary(
                group=group,
                denominator=len(selected),
                available_count=sum(
                    item.outcome.status is DescriptiveOutcomeStatus.AVAILABLE for item in selected
                ),
                pending_window_count=sum(
                    item.outcome.status is DescriptiveOutcomeStatus.PENDING_WINDOW
                    for item in selected
                ),
                insufficient_observed_bars_count=sum(
                    item.outcome.status is DescriptiveOutcomeStatus.INSUFFICIENT_OBSERVED_BARS
                    for item in selected
                ),
                ambiguous_close_count=sum(
                    item.outcome.status is DescriptiveOutcomeStatus.AMBIGUOUS_CLOSE
                    for item in selected
                ),
                invalid_close_count=sum(
                    item.outcome.status is DescriptiveOutcomeStatus.INVALID_CLOSE
                    for item in selected
                ),
                condition_not_evaluable_count=sum(
                    item.outcome.status is DescriptiveOutcomeStatus.CONDITION_NOT_EVALUABLE
                    for item in selected
                ),
                positives=sum(value > 0 for value in returns),
                negatives=sum(value < 0 for value in returns),
                zeros=sum(value == 0 for value in returns),
                mean_return_5_intervals=_mean(returns) if returns else None,
                median_return_5_intervals=_median(returns) if returns else None,
            )
        )
    return tuple(summaries)


class DescriptiveConditionOutcomeReport(ContractModel):
    """Deterministic read-only report over persisted condition and close evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["descriptive-condition-outcomes-v1"] = (
        DESCRIPTIVE_CONDITION_OUTCOMES_SCHEMA
    )
    report_id: UUID
    request: DescriptiveOutcomeRequest
    rule_id: NonEmptyStr
    rule_version: NonEmptyStr
    rule_fingerprint: str
    asset_id: NonEmptyStr
    asset_class: AssetClass
    source_id: NonEmptyStr
    currency: NonEmptyStr
    workspace_id: UUID
    horizon_version: Literal["forward_observed_close_intervals_5-v1"] = (
        FORWARD_CLOSE_INTERVALS_HORIZON
    )
    requested_calendar_days: int = Field(ge=1, le=MAX_DESCRIPTIVE_CALENDAR_DAYS)
    observed_periods: int = Field(ge=0, le=MAX_DESCRIPTIVE_CALENDAR_DAYS)
    matched_count: int = Field(ge=0)
    not_matched_count: int = Field(ge=0)
    not_evaluable_count: int = Field(ge=0)
    rows: tuple[DescriptivePeriodOutcome, ...] = Field(
        default=(),
        max_length=MAX_DESCRIPTIVE_CALENDAR_DAYS,
    )
    groups: tuple[DescriptiveGroupSummary, ...]
    latest_evidence_available_at: UTCDateTime | None = None
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...]

    @field_validator("rule_fingerprint")
    @classmethod
    def require_rule_fingerprint(cls, value: str) -> str:
        """Keep the frozen configuration identity explicit and exact."""
        normalized = value.strip()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("rule_fingerprint must be a lowercase SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def validate_report(self) -> DescriptiveConditionOutcomeReport:
        """Verify ordering, partition, summaries, availability, and identity."""
        periods = tuple(item.period_as_of for item in self.rows)
        if periods != tuple(sorted(periods)) or len(set(periods)) != len(periods):
            raise ValueError("report periods must be unique and ordered by as_of")
        if self.observed_periods != len(self.rows):
            raise ValueError("observed_periods must match the report rows")
        if self.requested_calendar_days != self.request.requested_calendar_days:
            raise ValueError("requested_calendar_days must match the request")
        if self.rule_fingerprint != self.request.expected_rule_fingerprint:
            raise ValueError("rule_fingerprint must match the requested fingerprint")
        if self.request.rule_id != self.rule_id or self.request.asset_id != self.asset_id:
            raise ValueError("report context must match its request")
        matched = sum(item.group is DescriptiveOutcomeGroup.MATCHED for item in self.rows)
        not_matched = sum(item.group is DescriptiveOutcomeGroup.NOT_MATCHED for item in self.rows)
        not_evaluable = sum(
            item.group is DescriptiveOutcomeGroup.NOT_EVALUABLE for item in self.rows
        )
        if (self.matched_count, self.not_matched_count, self.not_evaluable_count) != (
            matched,
            not_matched,
            not_evaluable,
        ):
            raise ValueError("report counts must partition the observed periods")
        if tuple(item.group for item in self.groups) != tuple(DescriptiveOutcomeGroup):
            raise ValueError("report groups must use the exact three-group order")
        expected_groups = summarize_descriptive_rows(self.rows)
        if self.groups != expected_groups:
            raise ValueError("report groups do not match the exact period outcomes")
        availability = tuple(
            value
            for item in self.rows
            for value in (
                item.condition.available_at,
                item.outcome.evidence_available_at,
            )
            if value is not None
        )
        expected_available_at = max(availability) if availability else None
        if self.latest_evidence_available_at != expected_available_at:
            raise ValueError("latest_evidence_available_at is inconsistent")
        if self.report_id != descriptive_outcome_report_id(
            request=self.request,
            rule_id=self.rule_id,
            rule_version=self.rule_version,
            rule_fingerprint=self.rule_fingerprint,
            asset_id=self.asset_id,
            asset_class=self.asset_class,
            source_id=self.source_id,
            currency=self.currency,
            workspace_id=self.workspace_id,
            requested_calendar_days=self.requested_calendar_days,
            observed_periods=self.observed_periods,
            rows=self.rows,
            groups=self.groups,
            latest_evidence_available_at=self.latest_evidence_available_at,
            limitations=self.limitations,
        ):
            raise ValueError("descriptive outcome report_id is not deterministic")
        if len(self.to_json_bytes()) > MAX_DESCRIPTIVE_OUTCOME_BYTES:
            raise ValueError("descriptive outcome report exceeds the 256 KiB contract")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON primitives for the local read-only API."""
        return self.model_dump(mode="json")

    def to_json_bytes(self) -> bytes:
        """Return the exact compact UTF-8 body served by the local API."""
        return json.dumps(
            self.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


def descriptive_outcome_report_id(
    *,
    request: DescriptiveOutcomeRequest,
    rule_id: str,
    rule_version: str,
    rule_fingerprint: str,
    asset_id: str,
    asset_class: AssetClass,
    source_id: str,
    currency: str,
    workspace_id: UUID,
    requested_calendar_days: int,
    observed_periods: int,
    rows: Sequence[DescriptivePeriodOutcome],
    groups: Sequence[DescriptiveGroupSummary],
    latest_evidence_available_at: datetime | None,
    limitations: Sequence[str],
) -> UUID:
    """Return one stable identity independent of the execution clock."""
    payload = {
        "schema_version": DESCRIPTIVE_CONDITION_OUTCOMES_SCHEMA,
        "request": request.model_dump(mode="json"),
        "rule_id": rule_id,
        "rule_version": rule_version,
        "rule_fingerprint": rule_fingerprint,
        "asset_id": asset_id,
        "asset_class": asset_class,
        "source_id": source_id,
        "currency": currency,
        "workspace_id": workspace_id,
        "horizon_version": FORWARD_CLOSE_INTERVALS_HORIZON,
        "requested_calendar_days": requested_calendar_days,
        "observed_periods": observed_periods,
        "rows": tuple(rows),
        "groups": tuple(groups),
        "latest_evidence_available_at": latest_evidence_available_at,
        "limitations": tuple(limitations),
    }
    encoded = canonical_analytical_json(payload).decode("utf-8")
    return uuid5(_DESCRIPTIVE_OUTCOMES_NAMESPACE, encoded)


def forward_window_end(eval_at: datetime) -> datetime:
    """Return the inclusive 30-day observation boundary for one condition."""
    return eval_at + timedelta(days=_FORWARD_WINDOW_DAYS)


def forward_interval_count() -> int:
    """Return the fixed number of intervals used by the horizon contract."""
    return _FORWARD_INTERVALS


def forward_close_count() -> int:
    """Return the fixed number of closes used by the horizon contract."""
    return _FORWARD_CLOSES


__all__ = [
    "ConditionEvidenceReason",
    "DESCRIPTIVE_CONDITION_OUTCOMES_REQUEST_SCHEMA",
    "DESCRIPTIVE_CONDITION_OUTCOMES_SCHEMA",
    "DescriptiveCloseObservation",
    "DescriptiveConditionEvidence",
    "DescriptiveConditionOutcomeReport",
    "DescriptiveGroupSummary",
    "DescriptiveOutcomeEvidence",
    "DescriptiveOutcomeGroup",
    "DescriptiveOutcomeRequest",
    "DescriptiveOutcomeStatus",
    "DescriptivePeriodOutcome",
    "FORWARD_CLOSE_INTERVALS_HORIZON",
    "MAX_DESCRIPTIVE_CALENDAR_DAYS",
    "MAX_DESCRIPTIVE_OUTCOME_BYTES",
    "OutcomeDecimal",
    "descriptive_outcome_report_id",
    "forward_close_count",
    "forward_interval_count",
    "forward_window_end",
    "summarize_descriptive_rows",
]
