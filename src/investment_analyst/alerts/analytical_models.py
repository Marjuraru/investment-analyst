"""Strict contracts for deterministic metric-based analytical screening."""

import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from investment_analyst.core.models import AssetClass, DataQuality, MetricResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_ANALYTICAL_SCREENING_NAMESPACE = UUID("0639f6ed-c77b-586e-bcef-fc25eaa5d2b2")


def _reject_decimal_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("screening numeric values must use Decimal")
    return value


ScreeningDecimal = Annotated[Decimal, BeforeValidator(_reject_decimal_float)]


class AnalyticalScreeningDomain(StrEnum):
    """Independent analytical domains supported by the first engine."""

    MARKET = "market"
    FUNDAMENTALS = "fundamentals"


class AnalyticalRuleState(StrEnum):
    """Operational state of one versioned analytical rule."""

    DRAFT = "draft"
    SILENT = "silent"
    ACTIVE = "active"
    PAUSED = "paused"


class AnalyticalConditionOperator(StrEnum):
    """Explicit Decimal comparisons supported without expression evaluation."""

    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    EQUAL = "eq"

    def compare(self, value: Decimal, threshold: Decimal) -> bool:
        """Apply the declared comparison without float coercion."""
        if self is AnalyticalConditionOperator.GREATER_THAN:
            return value > threshold
        if self is AnalyticalConditionOperator.GREATER_THAN_OR_EQUAL:
            return value >= threshold
        if self is AnalyticalConditionOperator.LESS_THAN:
            return value < threshold
        if self is AnalyticalConditionOperator.LESS_THAN_OR_EQUAL:
            return value <= threshold
        return value == threshold


class AnalyticalConditionState(StrEnum):
    """Tri-valued analytical condition outcome."""

    MET = "met"
    NOT_MET = "not_met"
    NOT_EVALUABLE = "not_evaluable"


class AnalyticalScreeningCondition(ContractModel):
    """One versioned metric comparison with an exact evidence contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    condition_id: NonEmptyStr
    label_es: NonEmptyStr
    domain: AnalyticalScreeningDomain
    metric_key: NonEmptyStr
    algorithm_version: NonEmptyStr
    unit: NonEmptyStr
    operator: AnalyticalConditionOperator
    threshold: ScreeningDecimal
    exit_threshold: ScreeningDecimal | None = None
    parameter_filters: dict[NonEmptyStr, JsonValue] = Field(default_factory=dict)
    accepted_qualities: tuple[DataQuality, ...] = (DataQuality.VALID,)
    limitations: tuple[NonEmptyStr, ...] = ()

    @field_validator("threshold")
    @classmethod
    def require_finite_threshold(cls, value: Decimal) -> Decimal:
        """Reject thresholds that cannot participate in exact comparisons."""
        if not value.is_finite():
            raise ValueError("screening threshold must be finite")
        return value

    @field_validator("exit_threshold")
    @classmethod
    def require_finite_exit_threshold(cls, value: Decimal | None) -> Decimal | None:
        """Reject non-finite optional hysteresis thresholds."""
        if value is not None and not value.is_finite():
            raise ValueError("screening exit_threshold must be finite")
        return value

    @model_validator(mode="after")
    def validate_condition(self) -> "AnalyticalScreeningCondition":
        """Keep quality and parameter matching deterministic."""
        qualities = tuple(item.value for item in self.accepted_qualities)
        if not qualities:
            raise ValueError("accepted_qualities must not be empty")
        if qualities != tuple(sorted(set(qualities))):
            raise ValueError("accepted_qualities must be unique and sorted")
        try:
            json.dumps(
                self.parameter_filters,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as error:
            raise ValueError("parameter_filters must be JSON serializable") from error
        if self.exit_threshold is not None:
            if self.operator is AnalyticalConditionOperator.EQUAL:
                raise ValueError("equal conditions cannot define exit_threshold")
            if (
                self.operator
                in {
                    AnalyticalConditionOperator.GREATER_THAN,
                    AnalyticalConditionOperator.GREATER_THAN_OR_EQUAL,
                }
                and self.exit_threshold > self.threshold
            ):
                raise ValueError("greater-than exit_threshold must not exceed the entry threshold")
            if (
                self.operator
                in {
                    AnalyticalConditionOperator.LESS_THAN,
                    AnalyticalConditionOperator.LESS_THAN_OR_EQUAL,
                }
                and self.exit_threshold < self.threshold
            ):
                raise ValueError("less-than exit_threshold must not be below the entry threshold")
        return self

    @property
    def retention_threshold(self) -> Decimal:
        """Return the explicit exit threshold or the entry threshold without hysteresis."""
        return self.exit_threshold if self.exit_threshold is not None else self.threshold


class AnalyticalScreeningRule(ContractModel):
    """One immutable all-conditions rule, independent from alert delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["analytical-screening-rule-v1"] = "analytical-screening-rule-v1"
    rule_id: NonEmptyStr
    rule_version: NonEmptyStr
    name_es: NonEmptyStr
    description_es: NonEmptyStr
    state: AnalyticalRuleState
    domain: AnalyticalScreeningDomain
    asset_classes: tuple[AssetClass, ...]
    join: Literal["all"] = "all"
    conditions: tuple[AnalyticalScreeningCondition, ...]
    confirmations_required: int = Field(default=1, ge=1, le=20)
    cooldown_seconds: int = Field(default=86_400, ge=0, le=31_536_000)
    limitations: tuple[NonEmptyStr, ...] = ()

    @field_validator("confirmations_required", "cooldown_seconds", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("screening rule counts must be integers")
        return value

    @model_validator(mode="after")
    def validate_rule(self) -> "AnalyticalScreeningRule":
        """Require one ordered domain and an explicit compatible universe."""
        asset_classes = tuple(item.value for item in self.asset_classes)
        if not asset_classes:
            raise ValueError("asset_classes must not be empty")
        if asset_classes != tuple(sorted(set(asset_classes))):
            raise ValueError("asset_classes must be unique and sorted")
        if not self.conditions:
            raise ValueError("conditions must not be empty")
        identifiers = tuple(item.condition_id for item in self.conditions)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("conditions must be unique and sorted by condition_id")
        if any(item.domain is not self.domain for item in self.conditions):
            raise ValueError("all conditions must match the rule domain")
        return self

    def semantic_fingerprint(self) -> str:
        """Hash the complete versioned rule semantics."""
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()


class AnalyticalScreeningRequest(ContractModel):
    """One explicit point-in-time metric snapshot supplied to the pure engine."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    rule: AnalyticalScreeningRule
    asset_id: NonEmptyStr
    asset_class: AssetClass
    source_id: NonEmptyStr
    known_at: UTCDateTime
    computed_at: UTCDateTime
    metrics: tuple[MetricResult, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> "AnalyticalScreeningRequest":
        """Reject mixed assets, sources, periods, cuts, or future evidence."""
        if self.asset_class not in self.rule.asset_classes:
            raise ValueError("asset_class is not compatible with the screening rule")
        if self.computed_at < self.known_at:
            raise ValueError("computed_at must not predate known_at")
        if not self.metrics:
            return self
        as_of_values = {item.as_of for item in self.metrics}
        if len(as_of_values) != 1:
            raise ValueError("screening metrics must share one common as_of")
        identities = tuple(item.result_id for item in self.metrics)
        if len(set(identities)) != len(identities):
            raise ValueError("screening metric result IDs must be unique")
        for metric in self.metrics:
            if metric.asset_id != self.asset_id:
                raise ValueError("screening metric belongs to another asset")
            if metric.available_at > self.known_at:
                raise ValueError("screening metric was unavailable at known_at")
            source_id = metric.parameters.get("source_id")
            if not isinstance(source_id, str) or source_id != self.source_id:
                raise ValueError("screening metric source_id does not match the request")
        return self


class AnalyticalConditionResult(ContractModel):
    """One tri-valued condition result with optional exact metric evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    condition_id: NonEmptyStr
    state: AnalyticalConditionState
    metric_key: NonEmptyStr
    operator: AnalyticalConditionOperator
    threshold: ScreeningDecimal
    retention_threshold: ScreeningDecimal
    unit: NonEmptyStr
    retention_state: AnalyticalConditionState
    observed_value: ScreeningDecimal | None = None
    metric_result_id: UUID | None = None
    as_of: UTCDateTime | None = None
    available_at: UTCDateTime | None = None
    quality: DataQuality | None = None
    explanation_es: NonEmptyStr

    @model_validator(mode="after")
    def validate_outcome(self) -> "AnalyticalConditionResult":
        """Keep evidence completeness and comparison state internally consistent."""
        evidence = (
            self.observed_value,
            self.metric_result_id,
            self.as_of,
            self.available_at,
            self.quality,
        )
        if self.state is AnalyticalConditionState.NOT_EVALUABLE:
            if self.retention_state is not AnalyticalConditionState.NOT_EVALUABLE:
                raise ValueError("unevaluable entry evidence requires unevaluable retention")
            if any(item is not None for item in evidence):
                raise ValueError("not_evaluable conditions cannot claim metric evidence")
            return self
        if any(item is None for item in evidence):
            raise ValueError("evaluated conditions require complete metric evidence")
        if self.observed_value is None or not self.observed_value.is_finite():
            raise ValueError("observed screening value must be finite")
        expected = (
            AnalyticalConditionState.MET
            if self.operator.compare(self.observed_value, self.threshold)
            else AnalyticalConditionState.NOT_MET
        )
        if self.state is not expected:
            raise ValueError("condition state does not match its exact comparison")
        expected_retention = (
            AnalyticalConditionState.MET
            if self.operator.compare(self.observed_value, self.retention_threshold)
            else AnalyticalConditionState.NOT_MET
        )
        if self.retention_state is not expected_retention:
            raise ValueError("retention_state does not match its hysteresis comparison")
        return self


class AnalyticalScreeningResult(ContractModel):
    """Deterministic result that does not itself create or deliver an alert."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["analytical-screening-result-v1"] = "analytical-screening-result-v1"
    result_id: UUID
    rule: AnalyticalScreeningRule
    asset_id: NonEmptyStr
    asset_class: AssetClass
    source_id: NonEmptyStr
    known_at: UTCDateTime
    as_of: UTCDateTime | None = None
    computed_at: UTCDateTime
    conditions: tuple[AnalyticalConditionResult, ...]
    matched: bool
    retained: bool | None
    activated: bool
    explanation_es: NonEmptyStr
    traceability_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_result(self) -> "AnalyticalScreeningResult":
        """Verify rule alignment, point-in-time evidence, activation, and identity."""
        if self.asset_class not in self.rule.asset_classes:
            raise ValueError("result asset_class is not compatible with its rule")
        if self.computed_at < self.known_at:
            raise ValueError("computed_at must not predate known_at")
        expected_conditions = tuple(item.condition_id for item in self.rule.conditions)
        actual_conditions = tuple(item.condition_id for item in self.conditions)
        if actual_conditions != expected_conditions:
            raise ValueError("condition results do not match the rule order")
        evidence_as_of = {item.as_of for item in self.conditions if item.as_of is not None}
        if self.as_of is None:
            if evidence_as_of:
                raise ValueError("result without as_of cannot contain metric evidence")
        elif evidence_as_of and evidence_as_of != {self.as_of}:
            raise ValueError("all condition evidence must match the result as_of")
        for item in self.conditions:
            if item.available_at is not None and item.available_at > self.known_at:
                raise ValueError("condition evidence was unavailable at known_at")
        matched = all(item.state is AnalyticalConditionState.MET for item in self.conditions)
        if self.matched is not matched:
            raise ValueError("matched must require every condition to be met")
        retained = (
            None
            if any(
                item.retention_state is AnalyticalConditionState.NOT_EVALUABLE
                for item in self.conditions
            )
            else all(
                item.retention_state is AnalyticalConditionState.MET for item in self.conditions
            )
        )
        if self.retained is not retained:
            raise ValueError("retained must match every condition's hysteresis state")
        enabled = self.rule.state in {
            AnalyticalRuleState.SILENT,
            AnalyticalRuleState.ACTIVE,
        }
        if self.activated is not (matched and enabled):
            raise ValueError("activated must match rule state and condition outcomes")
        if self.result_id != analytical_screening_result_id(
            self.rule,
            asset_id=self.asset_id,
            asset_class=self.asset_class,
            source_id=self.source_id,
            known_at=self.known_at,
            as_of=self.as_of,
            conditions=self.conditions,
        ):
            raise ValueError("analytical screening result_id is not deterministic")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON primitives for future persistence and replay."""
        return self.model_dump(mode="json")

    def semantic_fingerprint(self) -> str:
        """Hash reproducible semantics while excluding recomputation time."""
        payload = self.model_dump(mode="json", exclude={"computed_at"})
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()


def analytical_screening_result_id(
    rule: AnalyticalScreeningRule,
    *,
    asset_id: str,
    asset_class: AssetClass,
    source_id: str,
    known_at: UTCDateTime,
    as_of: UTCDateTime | None,
    conditions: tuple[AnalyticalConditionResult, ...],
) -> UUID:
    """Return one stable identity independent from recomputation time."""
    condition_payload = [
        item.model_dump(mode="json", exclude={"explanation_es"}) for item in conditions
    ]
    payload = {
        "rule_fingerprint": rule.semantic_fingerprint(),
        "asset_id": asset_id,
        "asset_class": asset_class.value,
        "source_id": source_id,
        "known_at": known_at.isoformat(),
        "as_of": as_of.isoformat() if as_of else None,
        "conditions": condition_payload,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return uuid5(_ANALYTICAL_SCREENING_NAMESPACE, encoded)


__all__ = [
    "AnalyticalConditionOperator",
    "AnalyticalConditionResult",
    "AnalyticalConditionState",
    "AnalyticalRuleState",
    "AnalyticalScreeningCondition",
    "AnalyticalScreeningDomain",
    "AnalyticalScreeningRequest",
    "AnalyticalScreeningResult",
    "AnalyticalScreeningRule",
    "ScreeningDecimal",
    "analytical_screening_result_id",
]
