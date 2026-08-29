"""Append-only RawRecord codec for institutional holdings evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_SOURCE_ID,
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
    InstitutionalHoldingsResolutionOutcome,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class InstitutionalHoldingsRepositoryError(StorageError):
    """A persisted institutional-holdings record cannot be trusted."""


def outcome_to_raw_record(outcome: InstitutionalHoldingsResolutionOutcome) -> RawRecord:
    return RawRecord(
        record_id=outcome.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=INSTITUTIONAL_HOLDINGS_SOURCE_ID,
            record_key=json.dumps({"outcome_id": str(outcome.outcome_id)}, sort_keys=True),
            retrieved_at=outcome.retrieved_at,
            raw_uri=outcome.resource_url,
            checksum_sha256=outcome.content_sha256,
        ),
        event_time=outcome.filing.accepted_at,
        available_at=outcome.available_at,
        received_at=outcome.retrieved_at,
        payload={
            "kind": "sec_institutional_holdings_outcome",
            "outcome": outcome.model_dump(mode="json"),
        },
        schema_version=outcome.schema_version,
    )


def outcome_from_raw_record(record: RawRecord) -> InstitutionalHoldingsResolutionOutcome:
    outcome = _decode(
        record,
        schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
        kind="sec_institutional_holdings_outcome",
        key="outcome",
        model=InstitutionalHoldingsResolutionOutcome,
    )
    expected_key = json.dumps({"outcome_id": str(outcome.outcome_id)}, sort_keys=True)
    if (
        record.record_id != outcome.raw_record_id
        or record.event_time != outcome.filing.accepted_at
        or record.available_at != outcome.available_at
        or record.received_at != outcome.retrieved_at
        or record.source.record_key != expected_key
        or record.source.retrieved_at != outcome.retrieved_at
        or record.source.raw_uri != outcome.resource_url
        or record.source.checksum_sha256 != outcome.content_sha256
    ):
        raise InstitutionalHoldingsRepositoryError("institutional outcome RawRecord conflicts")
    return outcome


def report_to_raw_record(report: InstitutionalHoldingsReport) -> RawRecord:
    revision = report.cover_revision
    return RawRecord(
        record_id=report.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=INSTITUTIONAL_HOLDINGS_SOURCE_ID,
            record_key=json.dumps({"report_id": str(report.report_id)}, sort_keys=True),
            retrieved_at=report.parsed_at,
            raw_uri=revision.source_url,
            checksum_sha256=revision.content_sha256,
        ),
        event_time=revision.document.filing.accepted_at,
        available_at=report.available_at,
        received_at=report.parsed_at,
        payload={
            "kind": "sec_institutional_holdings_report",
            "report": report.model_dump(mode="json"),
        },
        schema_version=report.schema_version,
    )


def report_from_raw_record(record: RawRecord) -> InstitutionalHoldingsReport:
    report = _decode(
        record,
        schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
        kind="sec_institutional_holdings_report",
        key="report",
        model=InstitutionalHoldingsReport,
    )
    revision = report.cover_revision
    expected_key = json.dumps({"report_id": str(report.report_id)}, sort_keys=True)
    if (
        record.record_id != report.raw_record_id
        or record.event_time != revision.document.filing.accepted_at
        or record.available_at != report.available_at
        or record.received_at != report.parsed_at
        or record.source.record_key != expected_key
        or record.source.retrieved_at != report.parsed_at
        or record.source.raw_uri != revision.source_url
        or record.source.checksum_sha256 != revision.content_sha256
    ):
        raise InstitutionalHoldingsRepositoryError("institutional report RawRecord conflicts")
    return report


def position_to_raw_record(position: InstitutionalHoldingPosition) -> RawRecord:
    revision = position.information_table_revision
    return RawRecord(
        record_id=position.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=INSTITUTIONAL_HOLDINGS_SOURCE_ID,
            record_key=json.dumps({"position_id": str(position.position_id)}, sort_keys=True),
            retrieved_at=position.parsed_at,
            raw_uri=revision.source_url,
            checksum_sha256=revision.content_sha256,
        ),
        event_time=revision.document.filing.accepted_at,
        available_at=position.available_at,
        received_at=position.parsed_at,
        payload={
            "kind": "sec_institutional_holding_position",
            "position": position.model_dump(mode="json"),
        },
        schema_version=position.schema_version,
    )


def position_from_raw_record(record: RawRecord) -> InstitutionalHoldingPosition:
    position = _decode(
        record,
        schema_version=INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION,
        kind="sec_institutional_holding_position",
        key="position",
        model=InstitutionalHoldingPosition,
    )
    revision = position.information_table_revision
    expected_key = json.dumps({"position_id": str(position.position_id)}, sort_keys=True)
    if (
        record.record_id != position.raw_record_id
        or record.event_time != revision.document.filing.accepted_at
        or record.available_at != position.available_at
        or record.received_at != position.parsed_at
        or record.source.record_key != expected_key
        or record.source.retrieved_at != position.parsed_at
        or record.source.raw_uri != revision.source_url
        or record.source.checksum_sha256 != revision.content_sha256
    ):
        raise InstitutionalHoldingsRepositoryError("institutional position RawRecord conflicts")
    return position


def _decode(record, *, schema_version: str, kind: str, key: str, model):
    if (
        record.asset_id is not None
        or record.source.source_id != INSTITUTIONAL_HOLDINGS_SOURCE_ID
        or record.schema_version != schema_version
        or not isinstance(record.payload, dict)
        or record.payload.get("kind") != kind
    ):
        raise InstitutionalHoldingsRepositoryError("institutional holdings RawRecord is malformed")
    try:
        return model.model_validate_json(json.dumps(record.payload[key]))
    except (KeyError, TypeError, ValueError) as error:
        raise InstitutionalHoldingsRepositoryError(
            "institutional holdings payload is malformed"
        ) from error


class InstitutionalHoldingsRepository:
    def __init__(self, raw_records) -> None:
        self._raw_records = raw_records

    def get_outcome(self, outcome_id: UUID) -> InstitutionalHoldingsResolutionOutcome | None:
        return self._get(
            InstitutionalHoldingsResolutionOutcome.expected_raw_record_id(outcome_id),
            outcome_from_raw_record,
        )

    def get_report(self, report_id: UUID) -> InstitutionalHoldingsReport | None:
        return self._get(
            InstitutionalHoldingsReport.expected_raw_record_id(report_id), report_from_raw_record
        )

    def get_position(self, position_id: UUID) -> InstitutionalHoldingPosition | None:
        return self._get(
            InstitutionalHoldingPosition.expected_raw_record_id(position_id),
            position_from_raw_record,
        )

    def _get(self, raw_record_id: UUID, decoder):
        try:
            return decoder(self._raw_records.get(raw_record_id))
        except RecordNotFoundError:
            return None

    def save_outcome(self, outcome: InstitutionalHoldingsResolutionOutcome):
        existing = self.get_outcome(outcome.outcome_id)
        if existing is not None and existing != outcome:
            raise InstitutionalHoldingsRepositoryError(
                "institutional holdings outcome identity conflicts"
            )
        self._raw_records.save(outcome_to_raw_record(outcome))
        return outcome

    def save_report(self, report: InstitutionalHoldingsReport):
        existing = self.get_report(report.report_id)
        if existing is not None and existing != report:
            raise InstitutionalHoldingsRepositoryError(
                "institutional holdings report identity conflicts"
            )
        self._raw_records.save(report_to_raw_record(report))
        return report

    def save_position(self, position: InstitutionalHoldingPosition):
        existing = self.get_position(position.position_id)
        if existing is not None and existing != position:
            raise InstitutionalHoldingsRepositoryError(
                "institutional holdings position identity conflicts"
            )
        self._raw_records.save(position_to_raw_record(position))
        return position

    def verify_outcome_lineage(self, outcome: InstitutionalHoldingsResolutionOutcome) -> None:
        try:
            discovery = self._raw_records.get(outcome.discovery_raw_record_id)
        except RecordNotFoundError as error:
            raise InstitutionalHoldingsRepositoryError(
                "institutional outcome has no submissions lineage"
            ) from error
        document = (
            discovery.payload.get("document") if isinstance(discovery.payload, dict) else None
        )
        discovered_cik = (
            str(document.get("cik", "")).zfill(10) if isinstance(document, dict) else ""
        )
        if (
            discovery.asset_id is not None
            or not discovery.source.source_id.endswith(":submissions")
            or discovery.received_at > outcome.retrieved_at
            or discovered_cik != outcome.filing.filer_cik
        ):
            raise InstitutionalHoldingsRepositoryError(
                "institutional outcome submissions lineage is invalid"
            )

    def list_reports(
        self, *, manager_cik: str, known_at: datetime
    ) -> list[InstitutionalHoldingsReport]:
        return sorted(
            (
                report
                for record in self._raw_records.list(
                    source_id=INSTITUTIONAL_HOLDINGS_SOURCE_ID,
                    schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
                    available_to=known_at,
                )
                if (report := report_from_raw_record(record)).manager_cik == manager_cik
            ),
            key=lambda item: (
                item.available_at,
                item.cover_revision.document.filing.accession,
                str(item.report_id),
            ),
        )

    def list_positions(
        self, *, report_ids: set[UUID], known_at: datetime
    ) -> list[InstitutionalHoldingPosition]:
        return sorted(
            (
                position
                for record in self._raw_records.list(
                    source_id=INSTITUTIONAL_HOLDINGS_SOURCE_ID,
                    schema_version=INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION,
                    available_to=known_at,
                )
                if (position := position_from_raw_record(record)).report_id in report_ids
            ),
            key=lambda item: (str(item.report_id), item.row_number),
        )


def verify_institutional_holding_records(
    records: Iterable[RawRecord], repository, filer_documents, content_store
) -> None:
    for record in records:
        if record.schema_version == INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION:
            outcome = outcome_from_raw_record(record)
            repository.verify_outcome_lineage(outcome)
            content_store.verify(outcome.content_sha256, size_bytes=outcome.content_size_bytes)
        elif record.schema_version == INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION:
            report = report_from_raw_record(record)
            filer_documents.verify_revision(report.cover_revision)
            filer_documents.verify_revision(report.information_table_revision)
        elif record.schema_version == INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION:
            position = position_from_raw_record(record)
            report = repository.get_report(position.report_id)
            if (
                report is None
                or report.information_table_revision != position.information_table_revision
            ):
                raise InstitutionalHoldingsRepositoryError(
                    "institutional position report lineage is invalid"
                )
            filer_documents.verify_revision(position.information_table_revision)
