"""Pure bounded parser for complete, as-filed Form 13F XML semantics."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from investment_analyst.evidence.sec_documents.models import SecFilerDocumentRevision
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
    InstitutionalOtherManager,
    InstitutionalSemanticsRow,
)
from investment_analyst.storage import StorageError

_MAX_BYTES = 50 * 1024 * 1024
_MAX_ROWS = 100_000
_FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)\b", re.I)


class SecInstitutionalSemanticsParserError(StorageError):
    """Persisted XML cannot safely form a complete semantic bundle."""


def parse_institutional_semantics(
    cover_content: bytes,
    information_table_content: bytes,
    *,
    parent_report_id,
    cover_revision: SecFilerDocumentRevision,
    information_table_revision: SecFilerDocumentRevision,
    parsed_at: datetime,
) -> InstitutionalHoldingsSemantics:
    _validate_bytes(cover_content, "cover")
    _validate_bytes(information_table_content, "information_table")
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise SecInstitutionalSemanticsParserError("parsed_at must be timezone-aware")
    if cover_revision.document.filing != information_table_revision.document.filing:
        raise SecInstitutionalSemanticsParserError("13F revisions identify different filings")
    filing = cover_revision.document.filing
    cover = _parse(cover_content)
    table = _parse(information_table_content)
    form = _text(cover, "submissionType", required=True)
    if form != filing.form:
        raise SecInstitutionalSemanticsParserError("cover form conflicts with filing")
    manager = _descendant(cover, "filingManager", required=True)
    manager_name = _text(manager, "name", required=True)
    artifact_id = InstitutionalHoldingsSemantics.expected_id(
        parent_report_id, cover_revision.revision_id, information_table_revision.revision_id
    )
    other_managers_included = _other_managers(cover)
    declared_sequences = {
        value.sequence_number
        for value in other_managers_included
        if value.sequence_number is not None
    }
    rows: list[InstitutionalSemanticsRow] = []
    for number, element in enumerate(_descendants(table, "infoTable"), start=1):
        if number > _MAX_ROWS:
            raise SecInstitutionalSemanticsParserError("institutional semantics row limit exceeded")
        quantity_type = _text(element, "sshPrnamtType")
        put_call = _text(element, "putCall")
        manager_reference = _text(element, "otherManager")
        limitations: list[str] = []
        if quantity_type is None or _text(element, "investmentDiscretion") is None:
            limitations.append("optional_not_reported")
        if quantity_type is not None and quantity_type not in {"SH", "PRN"}:
            limitations.append("unsupported_code")
        refs = _references(manager_reference)
        if refs and any(reference not in declared_sequences for reference in refs):
            limitations.append("unresolved_manager_reference")
        row_id = InstitutionalHoldingsSemantics.expected_row_id(artifact_id, number)
        rows.append(
            InstitutionalSemanticsRow(
                row_id=row_id,
                row_number=number,
                issuer_name=_text(element, "nameOfIssuer", required=True),
                title_of_class=_text(element, "titleOfClass", required=True),
                cusip=_text(element, "cusip", required=True),
                figi=_text(element, "figi"),
                value_as_reported=_decimal(_text(element, "value", required=True)),
                quantity=_decimal(_text(element, "sshPrnamt", required=True)),
                quantity_type=quantity_type,
                put_call=put_call,
                investment_discretion=_text(element, "investmentDiscretion"),
                other_manager=manager_reference,
                other_manager_sequence_references=refs,
                voting_sole=_decimal(_text(element, "Sole")),
                voting_shared=_decimal(_text(element, "Shared")),
                voting_none=_decimal(_text(element, "None")),
                limitations=tuple(sorted(set(limitations))),
            )
        )
    amendment_value = _boolean(_text(cover, "isAmendment"))
    amendment_no = _text(cover, "amendmentNo")
    amendment_type = _text(cover, "amendmentType")
    return InstitutionalHoldingsSemantics(
        artifact_id=artifact_id,
        raw_record_id=InstitutionalHoldingsSemantics.expected_raw_record_id(artifact_id),
        parent_report_id=parent_report_id,
        manager_cik=filing.filer_cik,
        manager_name=manager_name,
        cover_revision=cover_revision,
        information_table_revision=information_table_revision,
        accession=filing.accession,
        form=filing.form,
        report_period=filing.report_date,
        xml_schema_version=cover.attrib.get("schemaVersion") or _text(cover, "schemaVersion"),
        report_type=_text(cover, "reportType"),
        is_amendment=filing.is_amendment or amendment_value is True,
        amendment_number=amendment_no,
        amendment_type=amendment_type,
        confidential_omitted=_boolean(_text(cover, "confidentialOmitted")),
        declared_entry_total=_integer(_text(cover, "tableEntryTotal")),
        declared_value_total=_decimal(_text(cover, "tableValueTotal")),
        other_managers_included=other_managers_included,
        reporting_managers=_reporting_managers(cover, manager_name, filing.filer_cik),
        rows=tuple(rows),
        available_at=filing.accepted_at,
        parsed_at=parsed_at.astimezone(UTC),
    )


def _validate_bytes(content: bytes, role: str) -> None:
    if not isinstance(content, bytes) or not content or len(content) > _MAX_BYTES:
        raise SecInstitutionalSemanticsParserError("institutional semantics XML size is invalid")
    if _FORBIDDEN.search(content):
        raise SecInstitutionalSemanticsParserError(
            "institutional semantics XML declaration is forbidden"
        )
    root = _parse(content)
    expected = "edgarSubmission" if role == "cover" else "informationTable"
    if _name(root) != expected:
        raise SecInstitutionalSemanticsParserError("institutional semantics XML root is invalid")


def _parse(content: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise SecInstitutionalSemanticsParserError(
            "institutional semantics XML is malformed"
        ) from error


def _name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _descendants(element: ElementTree.Element, name: str) -> tuple[ElementTree.Element, ...]:
    return tuple(item for item in element.iter() if item is not element and _name(item) == name)


def _descendant(
    element: ElementTree.Element | None, name: str, *, required: bool = False
) -> ElementTree.Element | None:
    if element is None:
        if required:
            raise SecInstitutionalSemanticsParserError(f"missing XML {name}")
        return None
    found = _descendants(element, name)
    if len(found) > 1:
        raise SecInstitutionalSemanticsParserError(f"ambiguous XML {name}")
    if required and not found:
        raise SecInstitutionalSemanticsParserError(f"missing XML {name}")
    return found[0] if found else None


def _text(element: ElementTree.Element | None, name: str, *, required: bool = False) -> str | None:
    child = _descendant(element, name, required=required)
    value = None if child is None else (child.text or "").strip()
    if required and not value:
        raise SecInstitutionalSemanticsParserError(f"missing XML {name}")
    return value or None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise SecInstitutionalSemanticsParserError("invalid XML decimal") from error
    if not parsed.is_finite() or parsed < 0:
        raise SecInstitutionalSemanticsParserError("invalid XML decimal")
    return parsed


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdecimal():
        raise SecInstitutionalSemanticsParserError("invalid XML integer")
    return int(value)


def _boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.casefold()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    raise SecInstitutionalSemanticsParserError("invalid XML boolean")


def _references(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _other_managers(cover: ElementTree.Element) -> tuple[InstitutionalOtherManager, ...]:
    values: list[InstitutionalOtherManager] = []
    for element in _descendants(cover, "otherManager2"):
        values.append(
            InstitutionalOtherManager(
                sequence_number=_text(element, "sequenceNumber"),
                name=_text(element, "name"),
                cik=_text(element, "cik"),
                file_number=_text(element, "form13FFileNumber"),
            )
        )
    return tuple(values)


def _reporting_managers(
    cover: ElementTree.Element, manager_name: str, manager_cik: str
) -> tuple[InstitutionalOtherManager, ...]:
    explicit = tuple(
        InstitutionalOtherManager(
            sequence_number=_text(element, "sequenceNumber"),
            name=_text(element, "name"),
            cik=_text(element, "cik"),
            file_number=_text(element, "form13FFileNumber"),
        )
        for element in _descendants(cover, "reportingManager")
    )
    return explicit or (InstitutionalOtherManager(name=manager_name, cik=manager_cik),)
