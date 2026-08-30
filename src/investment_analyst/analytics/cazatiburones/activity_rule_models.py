# ruff: noqa: E501
"""Strict ephemeral contracts for descriptive activity-rule evaluations.

No evaluation defined here is persisted, deduplicated, notified, or aggregated into a
score, ranking, verdict, or confidence. Each evaluation is a single declared fact about
one rule, one feature, and one participant/subject pair.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

ActivityRuleFamily = Literal["insider", "beneficial_ownership", "institutional_13f"]
ActivityRuleConditionType = Literal["decimal_compare", "boolean_equals"]
ActivityRuleComparator = Literal["gt", "gte", "lt", "lte", "eq"]
ActivityRuleFeatureStatus = Literal["available", "missing", "not_evaluable"]
ActivityRuleStatus = Literal["met", "not_met", "not_evaluable"]


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class ActivityRuleDefinition(_Strict):
    """One versioned, declared descriptive rule over a single already-integrated feature."""

    rule_id: NonEmptyStr
    family: ActivityRuleFamily
    forms: tuple[NonEmptyStr, ...]
    feature_key: NonEmptyStr
    condition_type: ActivityRuleConditionType
    comparator: ActivityRuleComparator | None = None
    threshold: Decimal | None = None
    expected_value: bool | None = None
    unit: NonEmptyStr
    label_es: NonEmptyStr
    limitations: NonEmptyStr
    minimum_sample: int | None = Field(default=None, ge=1)
    definition_version: NonEmptyStr

    @model_validator(mode="after")
    def valid_condition_shape(self) -> "ActivityRuleDefinition":
        if not self.forms:
            raise ValueError("a rule must declare at least one applicable form")
        if self.condition_type == "decimal_compare":
            if self.comparator is None or self.threshold is None:
                raise ValueError("decimal_compare requires both comparator and threshold")
            if self.expected_value is not None:
                raise ValueError("decimal_compare must not declare expected_value")
            if not self.threshold.is_finite():
                raise ValueError("threshold must be a finite Decimal")
        else:
            if self.expected_value is None:
                raise ValueError("boolean_equals requires expected_value")
            if self.comparator is not None or self.threshold is not None:
                raise ValueError("boolean_equals must not declare comparator or threshold")
        return self


class ActivityRuleEvaluation(_Strict):
    """One traceable evaluation of one declared rule against one declared feature."""

    rule_id: NonEmptyStr
    definition_version: NonEmptyStr
    family: ActivityRuleFamily
    form: NonEmptyStr
    participant_id: NonEmptyStr
    subject_id: NonEmptyStr
    feature_key: NonEmptyStr
    feature_status: ActivityRuleFeatureStatus
    value_observed: Decimal | bool | None = None
    threshold_applied: Decimal | bool | None = None
    status: ActivityRuleStatus
    reason: NonEmptyStr | None = None
    revision_ids: tuple[NonEmptyStr, ...] = ()
    event_date: date | None = None
    available_at: UTCDateTime

    @model_validator(mode="after")
    def valid_shape(self) -> "ActivityRuleEvaluation":
        if self.status == "not_evaluable":
            if self.reason is None:
                raise ValueError("not_evaluable requires a reason")
            if self.value_observed is not None or self.threshold_applied is not None:
                raise ValueError("not_evaluable must not carry an observed value or threshold")
        else:
            if self.reason is not None:
                raise ValueError("met/not_met must not carry a not_evaluable reason")
            if self.value_observed is None or self.threshold_applied is None:
                raise ValueError("met/not_met requires both the observed value and threshold")
            if self.feature_status != "available":
                raise ValueError("met/not_met requires an available feature")
        return self


class ActivityRuleEvaluationResult(_Strict):
    """Per-query evaluation result with explicit coverage counting, never a score."""

    evaluations: tuple[ActivityRuleEvaluation, ...]
    total_features_evaluated: int = Field(ge=0)
    truncated: bool = False

    @model_validator(mode="after")
    def valid_coverage(self) -> "ActivityRuleEvaluationResult":
        if self.total_features_evaluated < len(self.evaluations):
            raise ValueError("total_features_evaluated cannot be lower than returned evaluations")
        return self
