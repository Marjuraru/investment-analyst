"""Pure, bounded parser for structured SEC Schedule 13D/13G XML."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipStatement
from investment_analyst.evidence.sec_documents.models import (
    BENEFICIAL_OWNERSHIP_FORMS,
    SecDocumentRevision,
    normalize_cik,
)
from investment_analyst.storage import StorageError

_FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)\b", re.I)
_STRUCTURED_ROOT = "edgarSubmission"


class SecBeneficialOwnershipParserError(StorageError):
    """Structured bytes do not satisfy the beneficial-ownership evidence contract."""


@dataclass(frozen=True, slots=True)
class BeneficialOwnershipResourceClassification:
    status: str
    reason_code: str


def classify_beneficial_ownership_resource(
    content: bytes,
) -> BeneficialOwnershipResourceClassification:
    """Classify exact bytes before any semantic extraction or normalization."""
    if _FORBIDDEN.search(content):
        return BeneficialOwnershipResourceClassification("rejected", "forbidden_declaration")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return BeneficialOwnershipResourceClassification("rejected", "not_xml")
    if _name(root) != _STRUCTURED_ROOT:
        return BeneficialOwnershipResourceClassification("rejected", "unexpected_root")
    return BeneficialOwnershipResourceClassification("accepted", "structured_sec_xml")


def parse_beneficial_ownership_statement(
    content: bytes, *, asset_id: str, revision: SecDocumentRevision, parsed_at: datetime
) -> BeneficialOwnershipStatement:
    classification = classify_beneficial_ownership_resource(content)
    if classification.status != "accepted":
        raise SecBeneficialOwnershipParserError(
            f"beneficial ownership resource rejected: {classification.reason_code}"
        )
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise SecBeneficialOwnershipParserError("parsed_at must be timezone-aware")
    root = ElementTree.fromstring(content)
    form = _text(root, "submissionType", required=True)
    if form not in BENEFICIAL_OWNERSHIP_FORMS or form != revision.document.filing.form:
        raise SecBeneficialOwnershipParserError(
            "structured filing form conflicts with document revision"
        )
    subject = _descendant(root, "subjectCompany", required=True)
    subject_cik = normalize_cik(_text(subject, "cik", required=True))
    subject_name = _text(subject, "name", required=True)
    owner = _descendant(root, "reportingOwner")
    if owner is None:
        owner = _descendant(root, "filingPerson")
    statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
    return BeneficialOwnershipStatement(
        statement_id=statement_id,
        raw_record_id=BeneficialOwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=asset_id,
        document_revision=revision,
        form=form,
        subject_cik=subject_cik,
        subject_name=subject_name,
        reporting_person_cik=_text(owner, "cik") if owner is not None else None,
        reporting_person_name=_text(owner, "name") if owner is not None else None,
        event_date=_date(_text(root, "eventDate")),
        shares_beneficially_owned=_decimal(
            _first_text(root, "aggregateAmountBeneficiallyOwned", "numberOfSharesBeneficiallyOwned")
        ),
        percent_of_class=_decimal(
            _first_text(root, "percentOfClass", "percentOfClassRepresentedByAmount")
        ),
        available_at=revision.available_at,
        parsed_at=parsed_at.astimezone(UTC),
    )


def _name(node: ElementTree.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def _descendant(
    node: ElementTree.Element, name: str, *, required: bool = False
) -> ElementTree.Element | None:
    found = tuple(item for item in node.iter() if item is not node and _name(item) == name)
    if len(found) > 1:
        raise SecBeneficialOwnershipParserError(f"ambiguous XML {name}")
    if not found and required:
        raise SecBeneficialOwnershipParserError(f"missing XML {name}")
    return found[0] if found else None


def _text(node: ElementTree.Element | None, name: str, *, required: bool = False) -> str | None:
    if node is None:
        if required:
            raise SecBeneficialOwnershipParserError(f"missing XML {name}")
        return None
    child = _descendant(node, name, required=required)
    value = None if child is None else (child.text or "").strip()
    if required and not value:
        raise SecBeneficialOwnershipParserError(f"missing XML {name}")
    return value or None


def _first_text(node: ElementTree.Element, *names: str) -> str | None:
    values = tuple(value for name in names if (value := _text(node, name)) is not None)
    if len(values) > 1:
        raise SecBeneficialOwnershipParserError("ambiguous beneficial ownership amount")
    return values[0] if values else None


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise SecBeneficialOwnershipParserError("invalid XML date") from error


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise SecBeneficialOwnershipParserError("invalid XML decimal") from error
    if not parsed.is_finite():
        raise SecBeneficialOwnershipParserError("invalid XML decimal")
    return parsed
