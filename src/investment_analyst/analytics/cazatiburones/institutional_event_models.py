"""Strict domain models for persisted institutional 13F events and candidates."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class _Strict(ContractModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )


class InstitutionalEventRule(_Strict):
    rule_id: NonEmptyStr
    metric_key: NonEmptyStr
    direction: Literal["increased", "reduced"]
    unit: NonEmptyStr
    definition_version: NonEmptyStr


class InstitutionalEvaluation(_Strict):
    rule_id: NonEmptyStr
    metric_result_id: UUID
    status: Literal["met", "not_met", "not_evaluable"]
    value: Decimal | None = None
    reason: NonEmptyStr | None = None
    unit: NonEmptyStr

    @model_validator(mode="after")
    def validate_shape(self) -> "InstitutionalEvaluation":
        if self.status == "met" and self.value is None:
            raise ValueError("met evaluation requires a value")
        if self.status != "met" and self.value is not None:
            raise ValueError("non-met evaluation must not carry a value")
        return self


class InstitutionalEvent(_Strict):
    event_id: UUID
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_period: NonEmptyStr
    prior_report_period: NonEmptyStr
    cusip: NonEmptyStr
    title_of_class: NonEmptyStr
    put_call: str | None = None
    rule_id: NonEmptyStr
    metric_result_id: UUID
    metric_key: NonEmptyStr
    algorithm_version: NonEmptyStr
    unit: NonEmptyStr
    value: Decimal
    available_at: UTCDateTime
    input_observation_ids: tuple[UUID, ...]
    parameters: dict[NonEmptyStr, str | None]


class InstitutionalCandidate(_Strict):
    candidate_id: UUID
    event_id: UUID
    status: Literal["eligible", "suppressed"]
    cooldown_until: UTCDateTime | None = None
    suppressed_by_event_id: UUID | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "InstitutionalCandidate":
        if self.status == "suppressed" and (
            self.cooldown_until is None or self.suppressed_by_event_id is None
        ):
            raise ValueError("suppressed candidate requires cooldown evidence")
        return self


class InstitutionalEventSnapshot(_Strict):
    snapshot_id: UUID
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    known_at: UTCDateTime
    recorded_at: UTCDateTime
    policy_version: NonEmptyStr
    evaluations: tuple[InstitutionalEvaluation, ...]
    events: tuple[InstitutionalEvent, ...]
    candidates: tuple[InstitutionalCandidate, ...]
    omissions: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_identities(self) -> "InstitutionalEventSnapshot":
        event_ids = [item.event_id for item in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("snapshot events must be unique")
        candidate_event_ids = [item.event_id for item in self.candidates]
        if set(candidate_event_ids) != set(event_ids) or len(candidate_event_ids) != len(
            self.candidates
        ):
            raise ValueError("snapshot candidates must exactly cover events")
        return self


class InstitutionalEventMaterializationSummary(_Strict):
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    known_at: UTCDateTime
    snapshot_id: UUID
    created: bool
    events: int = Field(ge=0)
    candidates: int = Field(ge=0)
