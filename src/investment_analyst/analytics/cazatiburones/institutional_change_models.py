"""Strict ephemeral contracts for descriptive institutional changes."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalPosition(_Strict):
    cusip: NonEmptyStr
    title_of_class: NonEmptyStr
    quantity: Decimal | None = None
    value: Decimal | None = None


class InstitutionalClose(_Strict):
    manager_cik: NonEmptyStr
    report_period: date
    available_at: UTCDateTime
    declared_value_total: Decimal | None = Field(default=None, ge=0)
    positions: tuple[InstitutionalPosition, ...]


class DescriptiveMetric(_Strict):
    key: NonEmptyStr
    status: Literal["available", "missing", "not_evaluable"]
    value: Decimal | bool | None = None

    @model_validator(mode="after")
    def valid_value(self) -> "DescriptiveMetric":
        if self.status == "available" and self.value is None:
            raise ValueError("available metric requires value")
        if self.status != "available" and self.value is not None:
            raise ValueError("unavailable metric must not have value")
        return self


class InstitutionalChangeResult(_Strict):
    manager_cik: NonEmptyStr
    previous_period: date
    current_period: date
    available_at: UTCDateTime
    metrics: tuple[DescriptiveMetric, ...]
