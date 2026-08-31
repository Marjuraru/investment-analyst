"""Explicit enrichment and read-only PIT query services for 13F semantic bundles."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemanticsQuery,
    InstitutionalSemanticsHeader,
    InstitutionalSemanticsRow,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
    InstitutionalSemanticsRepositoryError,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_semantics_parser import (
    SecInstitutionalSemanticsParserError,
    parse_institutional_semantics,
)
from investment_analyst.storage import StorageError


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InstitutionalSemanticsEnrichRequest(_Strict):
    manager_cik: NonEmptyStr
    report_ids: tuple[UUID, ...]
    known_at: UTCDateTime

    @field_validator("manager_cik")
    @classmethod
    def normalize_manager_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def validate_ids(self) -> InstitutionalSemanticsEnrichRequest:
        if (
            not self.report_ids
            or len(set(self.report_ids)) != len(self.report_ids)
            or len(self.report_ids) > 20
        ):
            raise ValueError("report_ids must contain one to twenty unique values")
        return self


class InstitutionalSemanticsEnrichOutcome(_Strict):
    report_id: UUID
    state: Literal["created", "reused", "not_visible", "rejected"]
    reason_code: NonEmptyStr | None = None
    artifact_id: UUID | None = None


class InstitutionalSemanticsEnrichResult(_Strict):
    examined: int = Field(ge=0)
    created: int = Field(ge=0)
    reused: int = Field(ge=0)
    not_visible: int = Field(ge=0)
    rejected: int = Field(ge=0)
    outcomes: tuple[InstitutionalSemanticsEnrichOutcome, ...]


class InstitutionalSemanticsQueryReport(_Strict):
    report_id: UUID
    state: Literal["found", "missing", "not_enriched"]
    header: InstitutionalSemanticsHeader | None = None
    total_rows: int = Field(ge=0)
    matching_rows: int = Field(ge=0)
    truncated: bool
    rows: tuple[InstitutionalSemanticsRow, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> InstitutionalSemanticsQueryReport:
        if self.state != "found" and (self.header is not None or self.rows):
            raise ValueError("missing semantic query cannot contain a header or rows")
        if self.state == "found" and self.header is None:
            raise ValueError("found semantic query requires a header")
        if self.matching_rows < len(self.rows) or self.total_rows < self.matching_rows:
            raise ValueError("semantic query counts are invalid")
        if self.truncated != (self.matching_rows > len(self.rows)):
            raise ValueError("semantic query truncation is invalid")
        return self


class InstitutionalSemanticsQueryResult(_Strict):
    reports: tuple[InstitutionalSemanticsQueryReport, ...]


class InstitutionalHoldingsSemanticsService:
    def __init__(self, storage, *, clock=lambda: datetime.now(UTC)) -> None:
        self._storage = storage
        self._clock = clock

    def enrich(
        self, request: InstitutionalSemanticsEnrichRequest
    ) -> InstitutionalSemanticsEnrichResult:
        if self._storage.read_only:
            raise StorageError("institutional semantics enrichment requires writable storage")
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        repository = InstitutionalSemanticsRepository(self._storage.raw_records)
        outcomes: list[InstitutionalSemanticsEnrichOutcome] = []
        for report_id in sorted(request.report_ids, key=str):
            parent = holdings.get_report(report_id)
            if (
                parent is None
                or parent.manager_cik != request.manager_cik
                or parent.available_at > request.known_at
            ):
                outcomes.append(
                    InstitutionalSemanticsEnrichOutcome(report_id=report_id, state="not_visible")
                )
                continue
            existing = repository.get_for_parent(parent)
            if existing is not None:
                outcomes.append(
                    InstitutionalSemanticsEnrichOutcome(
                        report_id=report_id, state="reused", artifact_id=existing.artifact_id
                    )
                )
                continue
            try:
                item = parse_institutional_semantics(
                    self._storage.documents.read(parent.cover_revision.content_sha256),
                    self._storage.documents.read(parent.information_table_revision.content_sha256),
                    parent_report_id=parent.report_id,
                    cover_revision=parent.cover_revision,
                    information_table_revision=parent.information_table_revision,
                    parsed_at=self._now(),
                )
                saved = repository.save(item)
            except (SecInstitutionalSemanticsParserError, ValueError) as error:
                outcomes.append(
                    InstitutionalSemanticsEnrichOutcome(
                        report_id=report_id, state="rejected", reason_code=_reason(error)
                    )
                )
                continue
            state = "created" if saved.parsed_at == item.parsed_at else "reused"
            outcomes.append(
                InstitutionalSemanticsEnrichOutcome(
                    report_id=report_id,
                    state=state,
                    artifact_id=saved.artifact_id,
                )
            )
        return InstitutionalSemanticsEnrichResult(
            examined=len(outcomes),
            created=sum(item.state == "created" for item in outcomes),
            reused=sum(item.state == "reused" for item in outcomes),
            not_visible=sum(item.state == "not_visible" for item in outcomes),
            rejected=sum(item.state == "rejected" for item in outcomes),
            outcomes=tuple(outcomes),
        )

    def query(
        self, query: InstitutionalHoldingsSemanticsQuery
    ) -> InstitutionalSemanticsQueryResult:
        if not self._storage.read_only:
            raise StorageError("institutional semantics query requires read-only storage")
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        repository = InstitutionalSemanticsRepository(self._storage.raw_records)
        reports: list[InstitutionalSemanticsQueryReport] = []
        for report_id in query.report_ids:
            parent = holdings.get_report(report_id)
            if (
                parent is None
                or parent.manager_cik != query.manager_cik
                or parent.available_at > query.known_at
            ):
                reports.append(
                    InstitutionalSemanticsQueryReport(
                        report_id=report_id,
                        state="missing",
                        total_rows=0,
                        matching_rows=0,
                        truncated=False,
                    )
                )
                continue
            item = repository.get_for_parent(parent)
            if item is None:
                reports.append(
                    InstitutionalSemanticsQueryReport(
                        report_id=report_id,
                        state="not_enriched",
                        total_rows=0,
                        matching_rows=0,
                        truncated=False,
                    )
                )
                continue
            matching = tuple(
                row for row in item.rows if query.cusip is None or row.cusip == query.cusip
            )
            page = matching[query.offset : query.offset + query.limit]
            reports.append(
                InstitutionalSemanticsQueryReport(
                    report_id=report_id,
                    state="found",
                    header=InstitutionalSemanticsHeader.from_artifact(item),
                    total_rows=len(item.rows),
                    matching_rows=len(matching),
                    truncated=query.offset + len(page) < len(matching),
                    rows=page,
                )
            )
        return InstitutionalSemanticsQueryResult(reports=tuple(reports))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise InstitutionalSemanticsRepositoryError(
                "institutional semantics clock must be timezone-aware"
            )
        return value.astimezone(UTC)


def _reason(error: BaseException) -> str:
    del error
    return "integrity_or_parser_rejection"
