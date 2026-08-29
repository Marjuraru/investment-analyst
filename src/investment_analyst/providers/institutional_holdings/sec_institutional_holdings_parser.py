"""Pure, bounded parser for structured SEC Form 13F XML."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from investment_analyst.evidence.sec_documents.models import (
    INSTITUTIONAL_HOLDINGS_FORMS,
    SecFilerDocumentRevision,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
)
from investment_analyst.storage import StorageError

_FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)\b", re.I)


class SecInstitutionalHoldingsParserError(StorageError):
    """Structured bytes do not satisfy the Form 13F evidence contract."""


@dataclass(frozen=True, slots=True)
class InstitutionalHoldingsResourceClassification:
    status: str
    reason_code: str


def classify_institutional_holdings_resource(
    content: bytes, *, role: str
) -> InstitutionalHoldingsResourceClassification:
    if _FORBIDDEN.search(content):
        return InstitutionalHoldingsResourceClassification("rejected", "forbidden_declaration")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return InstitutionalHoldingsResourceClassification("rejected", "not_xml")
    expected = "edgarSubmission" if role == "cover" else "informationTable"
    if role not in {"cover", "information_table"} or _name(root) != expected:
        return InstitutionalHoldingsResourceClassification("rejected", "unexpected_root")
    return InstitutionalHoldingsResourceClassification("accepted", "structured_sec_xml")


def parse_institutional_holdings(
    cover_content: bytes,
    information_table_content: bytes,
    *,
    cover_revision: SecFilerDocumentRevision,
    information_table_revision: SecFilerDocumentRevision,
    parsed_at: datetime,
) -> tuple[InstitutionalHoldingsReport, tuple[InstitutionalHoldingPosition, ...]]:
    for content, role in (
        (cover_content, "cover"),
        (information_table_content, "information_table"),
    ):
        classification = classify_institutional_holdings_resource(content, role=role)
        if classification.status != "accepted":
            raise SecInstitutionalHoldingsParserError(
                f"institutional holdings resource rejected: {classification.reason_code}"
            )
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise SecInstitutionalHoldingsParserError("parsed_at must be timezone-aware")
    if cover_revision.document.filing != information_table_revision.document.filing:
        raise SecInstitutionalHoldingsParserError("13F revisions identify different filings")
    parsed_at = parsed_at.astimezone(UTC)
    filing = cover_revision.document.filing
    cover = ElementTree.fromstring(cover_content)
    table = ElementTree.fromstring(information_table_content)
    form = _text(cover, "submissionType", required=True)
    if form not in INSTITUTIONAL_HOLDINGS_FORMS or form != filing.form:
        raise SecInstitutionalHoldingsParserError("cover form conflicts with filing")
    manager = _descendant(cover, "filingManager", required=True)
    manager_name = _text(manager, "name", required=True)
    cover_period = _date(_text(cover, "reportCalendarOrQuarter"))
    if filing.report_date is not None and cover_period != filing.report_date:
        raise SecInstitutionalHoldingsParserError("cover report period conflicts with filing")
    declared_entry_total = _integer(_text(cover, "tableEntryTotal"))
    declared_value_total = _decimal(_text(cover, "tableValueTotal"))
    report_id = InstitutionalHoldingsReport.expected_id(
        cover_revision.revision_id, information_table_revision.revision_id
    )
    positions: list[InstitutionalHoldingPosition] = []
    for row_number, row in enumerate(_descendants(table, "infoTable"), start=1):
        position_id = InstitutionalHoldingPosition.expected_id(report_id, row_number)
        positions.append(
            InstitutionalHoldingPosition(
                position_id=position_id,
                raw_record_id=InstitutionalHoldingPosition.expected_raw_record_id(position_id),
                report_id=report_id,
                information_table_revision=information_table_revision,
                row_number=row_number,
                issuer_name=_text(row, "nameOfIssuer", required=True),
                title_of_class=_text(row, "titleOfClass", required=True),
                cusip=_text(row, "cusip", required=True),
                value=_decimal(_text(row, "value")),
                quantity=_decimal(_text(row, "sshPrnamt")),
                quantity_type=_text(row, "sshPrnamtType"),
                investment_discretion=_text(row, "investmentDiscretion"),
                voting_sole=_decimal(_text(row, "Sole")),
                voting_shared=_decimal(_text(row, "Shared")),
                voting_none=_decimal(_text(row, "None")),
                available_at=filing.accepted_at,
                parsed_at=parsed_at,
            )
        )
    values_complete = all(position.value is not None for position in positions)
    parsed_value_total = sum(
        (position.value for position in positions if position.value is not None), Decimal("0")
    )
    report = InstitutionalHoldingsReport(
        report_id=report_id,
        raw_record_id=InstitutionalHoldingsReport.expected_raw_record_id(report_id),
        manager_cik=filing.filer_cik,
        manager_name=manager_name,
        file_number=_text(cover, "form13FFileNumber"),
        report_period=filing.report_date,
        cover_revision=cover_revision,
        information_table_revision=information_table_revision,
        declared_entry_total=declared_entry_total,
        declared_value_total=declared_value_total,
        parsed_entry_total=len(positions),
        parsed_value_total=parsed_value_total,
        position_values_complete=values_complete,
        entry_total_matches=(
            None if declared_entry_total is None else declared_entry_total == len(positions)
        ),
        value_total_matches=(
            None
            if declared_value_total is None or not values_complete
            else declared_value_total == parsed_value_total
        ),
        available_at=filing.accepted_at,
        parsed_at=parsed_at,
    )
    return report, tuple(positions)


def _name(node: ElementTree.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _descendants(node: ElementTree.Element, name: str) -> tuple[ElementTree.Element, ...]:
    return tuple(item for item in node.iter() if item is not node and _name(item) == name)


def _descendant(
    node: ElementTree.Element, name: str, *, required: bool = False
) -> ElementTree.Element | None:
    found = _descendants(node, name)
    if len(found) > 1:
        raise SecInstitutionalHoldingsParserError(f"ambiguous XML {name}")
    if not found and required:
        raise SecInstitutionalHoldingsParserError(f"missing XML {name}")
    return found[0] if found else None


def _text(node: ElementTree.Element | None, name: str, *, required: bool = False) -> str | None:
    if node is None:
        if required:
            raise SecInstitutionalHoldingsParserError(f"missing XML {name}")
        return None
    child = _descendant(node, name, required=required)
    value = None if child is None else (child.text or "").strip()
    if required and not value:
        raise SecInstitutionalHoldingsParserError(f"missing XML {name}")
    return value or None


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%m-%d-%Y").date()
        except ValueError as error:
            raise SecInstitutionalHoldingsParserError("invalid XML date") from error


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise SecInstitutionalHoldingsParserError("invalid XML decimal") from error
    if not parsed.is_finite():
        raise SecInstitutionalHoldingsParserError("invalid XML decimal")
    return parsed


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdecimal():
        raise SecInstitutionalHoldingsParserError("invalid XML integer")
    return int(value)
