"""Original records and normalized observations."""

from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import Field, JsonValue, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.core.models.source import SourceReference


class RawRecord(ContractModel):
    """Uninterpreted JSON payload retained exactly as received from a source."""

    record_id: UUID = Field(default_factory=uuid4)
    asset_id: NonEmptyStr | None = None
    source: SourceReference
    event_time: UTCDateTime | None = None
    available_at: UTCDateTime
    received_at: UTCDateTime
    payload: JsonValue
    schema_version: NonEmptyStr

    @model_validator(mode="after")
    def validate_availability_order(self) -> "RawRecord":
        """Ensure the record was available no later than local receipt."""
        if self.available_at > self.received_at:
            raise ValueError("available_at must not be later than received_at")
        return self


class NormalizedObservation(ContractModel):
    """Typed value derived from one raw record with complete provenance."""

    observation_id: UUID = Field(default_factory=uuid4)
    raw_record_id: UUID
    asset_id: NonEmptyStr
    field_name: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    frequency: DataFrequency
    observed_at: UTCDateTime | None = None
    period_start: UTCDateTime | None = None
    period_end: UTCDateTime | None = None
    available_at: UTCDateTime
    normalized_at: UTCDateTime
    source: SourceReference
    quality: DataQuality
    transformation_version: NonEmptyStr

    @model_validator(mode="after")
    def validate_temporal_consistency(self) -> "NormalizedObservation":
        """Validate reporting periods and normalization availability."""
        if (
            self.period_start is not None
            and self.period_end is not None
            and self.period_start > self.period_end
        ):
            raise ValueError("period_start must not be later than period_end")
        if self.available_at > self.normalized_at:
            raise ValueError("available_at must not be later than normalized_at")
        if self.observed_at is None and self.period_end is None:
            raise ValueError("observed_at or period_end must be provided")
        return self
