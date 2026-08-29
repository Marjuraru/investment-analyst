from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

INSTRUMENT_CORRESPONDENCE_SOURCE_ID = "catalog-declaration:instrument-correspondence:v1"
INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION = "instrument-correspondence-v1"
_IDS = uuid5(NAMESPACE_URL, "investment-analyst:instrument-correspondence:v1")
_RAW = uuid5(NAMESPACE_URL, "investment-analyst:instrument-correspondence-raw:v1")
_CUSIP = re.compile(r"^[0-9A-Z*@#]{9}$")


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstrumentCorrespondence(_Strict):
    correspondence_id: UUID
    raw_record_id: UUID
    asset_id: NonEmptyStr
    cusip: NonEmptyStr
    title_of_class: NonEmptyStr
    effective_from: date
    effective_to: date | None = None
    available_at: UTCDateTime
    recorded_at: UTCDateTime
    schema_version: Literal["instrument-correspondence-v1"] = (
        INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION
    )

    @field_validator("cusip")
    @classmethod
    def valid_cusip(cls, value: str) -> str:
        if not _CUSIP.fullmatch(value):
            raise ValueError("CUSIP must contain exactly nine declared characters")
        return value

    @model_validator(mode="after")
    def valid(self) -> InstrumentCorrespondence:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        if self.available_at > self.recorded_at:
            raise ValueError("available_at must not be later than recorded_at")
        if self.correspondence_id != self.expected_id(
            self.cusip,
            self.title_of_class,
            self.asset_id,
            self.effective_from,
            self.effective_to,
            self.available_at,
        ):
            raise ValueError("instrument correspondence identity is invalid")
        if self.raw_record_id != self.expected_raw_record_id(self.correspondence_id):
            raise ValueError("instrument correspondence raw identity is invalid")
        return self

    @staticmethod
    def expected_id(
        cusip: str,
        title: str,
        asset_id: str,
        effective_from: date,
        effective_to: date | None,
        available_at: datetime,
    ) -> UUID:
        effective_to_value = "" if effective_to is None else effective_to.isoformat()
        return uuid5(
            _IDS,
            "|".join(
                (
                    cusip,
                    title,
                    asset_id,
                    effective_from.isoformat(),
                    effective_to_value,
                    available_at.isoformat(),
                    INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION,
                )
            ),
        )

    @staticmethod
    def expected_raw_record_id(correspondence_id: UUID) -> UUID:
        return uuid5(_RAW, f"{correspondence_id}|raw-record")

    @classmethod
    def declare(
        cls,
        *,
        asset_id: str,
        cusip: str,
        title_of_class: str,
        effective_from: date,
        effective_to: date | None,
        available_at: datetime,
        recorded_at: datetime,
    ) -> InstrumentCorrespondence:
        correspondence_id = cls.expected_id(
            cusip, title_of_class, asset_id, effective_from, effective_to, available_at
        )
        return cls(
            correspondence_id=correspondence_id,
            raw_record_id=cls.expected_raw_record_id(correspondence_id),
            asset_id=asset_id,
            cusip=cusip,
            title_of_class=title_of_class,
            effective_from=effective_from,
            effective_to=effective_to,
            available_at=available_at,
            recorded_at=recorded_at,
        )

    @property
    def event_time(self) -> datetime:
        return datetime.combine(self.effective_from, time.min, tzinfo=UTC)

    def is_effective_on(self, report_period: date | None) -> bool:
        return (
            report_period is not None
            and self.effective_from <= report_period
            and (self.effective_to is None or report_period < self.effective_to)
        )
