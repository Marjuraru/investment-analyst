"""Strict contracts for descriptive rules over materialized valuation history."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.valuation.history_models import CorporateValuationHistoryPoint
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

RuleOperator = Literal["at_or_below_empirical_percentile", "at_or_above_empirical_percentile"]
RuleStatus = Literal["met", "not_met", "not_evaluable"]


class CorporateValuationHistoryRule(ContractModel):
    """One immutable, versioned descriptive threshold rule."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    rule_id: NonEmptyStr
    rule_version: NonEmptyStr
    name: NonEmptyStr
    limitations: tuple[NonEmptyStr, ...]
    metric_key: NonEmptyStr
    operator: RuleOperator
    threshold: Decimal
    minimum_prior_points: int = Field(ge=3, le=250)

    @field_validator("threshold", mode="before")
    @classmethod
    def validate_threshold(cls, value: object) -> object:
        if isinstance(value, (bool, float)):
            raise ValueError("threshold must be an exact Decimal")
        decimal = Decimal(value) if isinstance(value, str) else value
        if (
            not isinstance(decimal, Decimal)
            or not decimal.is_finite()
            or not Decimal("0") <= decimal <= Decimal("1")
        ):
            raise ValueError("threshold must be a finite Decimal from 0 to 1")
        return decimal

    @field_validator("minimum_prior_points", mode="before")
    @classmethod
    def reject_boolean_minimum(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("minimum_prior_points must be an integer")
        return value


class CorporateValuationHistoryRuleRequest(ContractModel):
    """One bounded point-in-time evaluation; no rule state is persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["corporate-valuation-history-rule-request-v1"] = (
        "corporate-valuation-history-rule-request-v1"
    )
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    start_date: date
    end_date: date
    basis: Literal["latest_annual"] = "latest_annual"
    rule: CorporateValuationHistoryRule

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_range(self) -> "CorporateValuationHistoryRuleRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class CorporateValuationHistoryRuleCoverage(ContractModel):
    """Exact, sparse coverage used to decide whether the rule is evaluable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_results: int = Field(ge=0)
    superseded_revisions: int = Field(ge=0)
    prior_points: int = Field(ge=0)
    required_prior_points: int = Field(ge=3, le=250)
    reference_start_date: date | None = None
    reference_end_date: date | None = None


class CorporateValuationHistoryRuleEvaluation(ContractModel):
    """One transparent outcome, never a score, candidate, or recommendation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["corporate-valuation-history-rule-evaluation-v1"] = (
        "corporate-valuation-history-rule-evaluation-v1"
    )
    request: CorporateValuationHistoryRuleRequest
    status: RuleStatus
    result_id: UUID
    formula: Literal["empirical_midrank_percentile_decimal34"] = (
        "empirical_midrank_percentile_decimal34"
    )
    current_point: CorporateValuationHistoryPoint | None = None
    reference_points: tuple[CorporateValuationHistoryPoint, ...] = ()
    empirical_percentile: Decimal | None = None
    lower_count: int = Field(ge=0)
    equal_count: int = Field(ge=0)
    greater_count: int = Field(ge=0)
    coverage: CorporateValuationHistoryRuleCoverage
    limitations: tuple[NonEmptyStr, ...]
