"""Strict ephemeral result contracts for declared SEC activity."""

from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field

from investment_analyst.analytics.cazatiburones.institutional_change_models import DescriptiveMetric
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class DeclaredActivityFeatureSet(_Strict):
    asset_id: NonEmptyStr
    family: NonEmptyStr
    participant_cik: NonEmptyStr
    form: NonEmptyStr
    declared_nature: NonEmptyStr | None = None
    security_title: NonEmptyStr | None = None
    table: Literal["non_derivative", "derivative"] | None = None
    event_date: date | None = None
    available_at: UTCDateTime
    revision_ids: tuple[NonEmptyStr, ...]
    comparison_status: Literal["available", "not_evaluable", "discontinuous"]
    metrics: tuple[DescriptiveMetric, ...]


class DeclaredActivityQueryResult(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    insider_features: tuple[DeclaredActivityFeatureSet, ...]
    beneficial_features: tuple[DeclaredActivityFeatureSet, ...]
    total_statements: int = Field(ge=0)
    truncated: bool = False
