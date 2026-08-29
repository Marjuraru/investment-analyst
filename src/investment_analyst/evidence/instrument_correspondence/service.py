from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.storage import StorageError
from investment_analyst.storage.local import LocalStorage


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstrumentCorrespondenceQuery(_Strict):
    """Read-only projection request; manager CIK belongs to the Form 13F lookup."""

    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    known_at: UTCDateTime
    period_from: date | None = None
    period_to: date | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("manager_cik")
    @classmethod
    def cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def period(self) -> "InstrumentCorrespondenceQuery":
        if (
            self.period_from is not None
            and self.period_to is not None
            and self.period_from > self.period_to
        ):
            raise ValueError("period_from must not be after period_to")
        return self


class LinkedInstitutionalHolding(_Strict):
    position: InstitutionalHoldingPosition
    correspondence: InstrumentCorrespondence


class UnlinkedInstitutionalHolding(_Strict):
    position: InstitutionalHoldingPosition
    reason: Literal[
        "missing_correspondence",
        "missing_report_period",
        "outside_effective_period",
        "ambiguous_correspondence",
    ]


class InstrumentCorrespondenceQueryResult(_Strict):
    reports: tuple[InstitutionalHoldingsReport, ...]
    linked_positions: tuple[LinkedInstitutionalHolding, ...]
    unlinked_positions: tuple[UnlinkedInstitutionalHolding, ...]
    total_matching: int = Field(ge=0)
    total_positions: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def shape(self) -> "InstrumentCorrespondenceQueryResult":
        if self.total_matching < len(self.reports) or self.truncated != (
            self.total_matching > len(self.reports)
        ):
            raise ValueError("query totals are invalid")
        if self.total_positions != len(self.linked_positions) + len(self.unlinked_positions):
            raise ValueError("total_positions must match resolved positions")
        return self


class InstrumentCorrespondenceService:
    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    def query(self, query: InstrumentCorrespondenceQuery) -> InstrumentCorrespondenceQueryResult:
        if not self._storage.read_only:
            raise StorageError("instrument correspondence query requires read-only storage")
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        matches = [
            r
            for r in holdings.list_reports(manager_cik=query.manager_cik, known_at=query.known_at)
            if (
                query.period_from is None
                or r.report_period is not None
                and r.report_period >= query.period_from
            )
            and (
                query.period_to is None
                or r.report_period is not None
                and r.report_period <= query.period_to
            )
        ]
        reports = tuple(reversed(matches))[: query.limit]
        positions = tuple(
            holdings.list_positions(
                report_ids={r.report_id for r in reports}, known_at=query.known_at
            )
        )
        candidates = [
            c
            for c in InstrumentCorrespondenceRepository(self._storage.raw_records).list(
                known_at=query.known_at, asset_id=query.asset_id
            )
        ]
        periods = {r.report_id: r.report_period for r in reports}
        linked: list[LinkedInstitutionalHolding] = []
        unlinked: list[UnlinkedInstitutionalHolding] = []
        for position in positions:
            effective = [
                c
                for c in candidates
                if c.cusip == position.cusip and c.is_effective_on(periods[position.report_id])
            ]
            if periods[position.report_id] is None:
                unlinked.append(
                    UnlinkedInstitutionalHolding(position=position, reason="missing_report_period")
                )
            elif len(effective) == 1:
                linked.append(
                    LinkedInstitutionalHolding(position=position, correspondence=effective[0])
                )
            else:
                unlinked.append(
                    UnlinkedInstitutionalHolding(
                        position=position,
                        reason="ambiguous_correspondence"
                        if len(effective) > 1
                        else "outside_effective_period"
                        if any(c.cusip == position.cusip for c in candidates)
                        else "missing_correspondence",
                    )
                )
        return InstrumentCorrespondenceQueryResult(
            reports=reports,
            linked_positions=tuple(linked),
            unlinked_positions=tuple(unlinked),
            total_matching=len(matches),
            total_positions=len(positions),
            truncated=len(matches) > len(reports),
        )
