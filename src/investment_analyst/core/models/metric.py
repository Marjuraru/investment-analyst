"""Metric definitions and calculated metric results."""

from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataQuality, MetricCategory


class MetricDefinition(ContractModel):
    """Versioned and human-readable contract for a deterministic metric."""

    metric_key: NonEmptyStr
    display_name: NonEmptyStr
    category: MetricCategory
    description: NonEmptyStr
    formula: NonEmptyStr
    unit: NonEmptyStr
    default_parameters: dict[NonEmptyStr, JsonValue] = Field(default_factory=dict)
    limitations: list[NonEmptyStr] = Field(default_factory=list)
    references: list[NonEmptyStr] = Field(default_factory=list)
    definition_version: NonEmptyStr


class MetricResult(ContractModel):
    """Calculated metric value with its inputs, parameters, and algorithm version."""

    result_id: UUID = Field(default_factory=uuid4)
    asset_id: NonEmptyStr
    metric_key: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    computed_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue] = Field(default_factory=dict)
    input_observation_ids: list[UUID]
    algorithm_version: NonEmptyStr
    quality: DataQuality

    @model_validator(mode="after")
    def validate_result_traceability(self) -> "MetricResult":
        """Ensure the result is temporally valid and has source observations."""
        if self.available_at > self.computed_at:
            raise ValueError("available_at must not be later than computed_at")
        if not self.input_observation_ids:
            raise ValueError("at least one input_observation_id is required")
        return self
