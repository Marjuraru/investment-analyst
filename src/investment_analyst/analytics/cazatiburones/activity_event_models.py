"""Strict append-only artifacts for descriptive declared-activity events."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


EvaluationStatus = Literal["met", "not_met", "not_evaluable"]
CandidateStatus = Literal["eligible", "suppressed"]


class ActivityEventRule(_Strict):
    rule_id: NonEmptyStr
    metric_key: NonEmptyStr
    direction: Literal["increased", "reduced"]
    unit: NonEmptyStr
    definition_version: NonEmptyStr


class ActivityEvaluation(_Strict):
    rule_id: NonEmptyStr
    metric_result_id: UUID
    status: EvaluationStatus
    reason: NonEmptyStr | None = None
    value: Decimal | None = None
    unit: NonEmptyStr

    @model_validator(mode="after")
    def shape(self) -> "ActivityEvaluation":
        if self.status == "met" and self.value is None:
            raise ValueError("met evaluation requires a value")
        if self.status != "met" and self.value is not None:
            raise ValueError("non-met evaluation must not carry a value")
        return self


class ActivityEvent(_Strict):
    event_id: UUID
    asset_id: NonEmptyStr
    rule_id: NonEmptyStr
    metric_result_id: UUID
    metric_key: NonEmptyStr
    unit: NonEmptyStr
    value: Decimal
    available_at: UTCDateTime
    input_observation_ids: tuple[UUID, UUID]
    parameters: dict[NonEmptyStr, object]


class ActivityCandidate(_Strict):
    candidate_id: UUID
    event_id: UUID
    status: CandidateStatus
    cooldown_until: UTCDateTime | None = None
    suppressed_by_event_id: UUID | None = None

    @model_validator(mode="after")
    def shape(self) -> "ActivityCandidate":
        if self.status == "suppressed" and (
            self.cooldown_until is None or self.suppressed_by_event_id is None
        ):
            raise ValueError("suppressed candidate requires cooldown evidence")
        return self


class ActivityEventSnapshot(_Strict):
    snapshot_id: UUID
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    recorded_at: UTCDateTime
    policy_version: NonEmptyStr
    evaluations: tuple[ActivityEvaluation, ...]
    events: tuple[ActivityEvent, ...]
    candidates: tuple[ActivityCandidate, ...]
    omissions: tuple[NonEmptyStr, ...] = ()
    schema_version: Literal["cazatiburones-activity-event-snapshot-v1"] = (
        "cazatiburones-activity-event-snapshot-v1"
    )

    @model_validator(mode="after")
    def identities(self) -> "ActivityEventSnapshot":
        if len({item.event_id for item in self.events}) != len(self.events):
            raise ValueError("snapshot events must be unique")
        if {item.event_id for item in self.candidates} != {item.event_id for item in self.events}:
            raise ValueError("snapshot candidates must exactly cover events")
        return self


class ActivityEventMaterializationSummary(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    snapshot_id: UUID
    created: bool
    events: int = Field(ge=0)
    candidates: int = Field(ge=0)
