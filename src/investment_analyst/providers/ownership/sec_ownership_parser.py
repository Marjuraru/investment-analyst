"""Pure safe XML parser for Section 16 ownership documents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from investment_analyst.evidence.sec_documents.models import SecDocumentRevision, normalize_cik
from investment_analyst.evidence.sec_ownership.models import (
    OwnershipEntry,
    OwnershipStatement,
    ReportingOwner,
)
from investment_analyst.storage import StorageError

_FORBIDDEN = re.compile(rb"<!\s*(DOCTYPE|ENTITY)\b", re.I)


class SecOwnershipParserError(StorageError):
    pass


@dataclass(frozen=True, slots=True)
class OwnershipResourceClassification:
    status: str
    reason_code: str


def classify_ownership_resource(content: bytes) -> OwnershipResourceClassification:
    """Classify exact bytes before parsing; never repair or transform them."""
    if _FORBIDDEN.search(content):
        return OwnershipResourceClassification("rejected", "forbidden_declaration")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return OwnershipResourceClassification("rejected", "not_xml")
    if _name(root) != "ownershipDocument":
        return OwnershipResourceClassification("rejected", "incompatible_root")
    return OwnershipResourceClassification("accepted", "ownership_xml")


def parse_ownership_statement(
    content: bytes, *, asset_id: str, revision: SecDocumentRevision, parsed_at: datetime
) -> OwnershipStatement:
    classification = classify_ownership_resource(content)
    if classification.status != "accepted":
        raise SecOwnershipParserError(f"ownership resource rejected: {classification.reason_code}")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise SecOwnershipParserError("ownership document is not XML") from error
    if _name(root) != "ownershipDocument":
        raise SecOwnershipParserError("ownership XML root is invalid")
    form = _text(root, "documentType", required=True)
    if form != revision.document.filing.form:
        raise SecOwnershipParserError("ownership form conflicts with document revision")
    issuer = _child(root, "issuer", required=True)
    issuer_cik = normalize_cik(_text(issuer, "issuerCik", required=True))
    if issuer_cik != revision.document.filing.filer_cik:
        raise SecOwnershipParserError("ownership issuer conflicts with document revision")
    owners = tuple(_owner(node) for node in _children(root, "reportingOwner"))
    if not owners:
        raise SecOwnershipParserError("ownership document has no owner")
    footnotes = {
        node.attrib["id"]: " ".join("".join(node.itertext()).split())
        for node in root.iter()
        if _name(node) == "footnote" and node.attrib.get("id")
    }
    statement_id = OwnershipStatement.expected_id(revision.revision_id)
    entries = []
    ordinal = 0
    for table_name, table in (
        ("nonDerivativeTable", "non_derivative"),
        ("derivativeTable", "derivative"),
    ):
        container = _child(root, table_name)
        if container is None:
            continue
        for node in container:
            kind = (
                "holding"
                if _name(node).endswith("Holding")
                else "transaction"
                if _name(node).endswith("Transaction")
                else None
            )
            if kind is None:
                continue
            refs = tuple(
                child.attrib.get("id", "") for child in node.iter() if _name(child) == "footnoteId"
            )
            if not all(ref and ref in footnotes for ref in refs):
                raise SecOwnershipParserError("ownership entry has dangling footnote")
            for owner in owners:
                entries.append(
                    OwnershipEntry(
                        entry_id=OwnershipEntry.expected_id(statement_id, table, kind, ordinal),
                        table=table,
                        kind=kind,
                        ordinal=ordinal,
                        owner_cik=owner.cik,
                        security_title=_value(node, "securityTitle", required=True),
                        transaction_date=_date(_value(node, "transactionDate"))
                        if _value(node, "transactionDate")
                        else None,
                        transaction_code=_text(
                            _child(node, "transactionCoding"), "transactionCode"
                        ),
                        acquired_disposed=_value(
                            _child(node, "transactionAmounts"), "transactionAcquiredDisposedCode"
                        ),
                        shares=_decimal(
                            _value(_child(node, "transactionAmounts"), "transactionShares")
                        ),
                        price_per_share=_decimal(
                            _value(_child(node, "transactionAmounts"), "transactionPricePerShare")
                        ),
                        shares_owned_following=_decimal(
                            _value(
                                _child(node, "postTransactionAmounts"),
                                "sharesOwnedFollowingTransaction",
                            )
                        ),
                        ownership_nature=_value(
                            _child(node, "ownershipNature"), "directOrIndirectOwnership"
                        ),
                        footnote_ids=refs,
                    )
                )
                ordinal += 1
    if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
        raise SecOwnershipParserError("parsed_at must be timezone-aware")
    return OwnershipStatement(
        statement_id=statement_id,
        raw_record_id=OwnershipStatement.expected_raw_record_id(statement_id),
        asset_id=asset_id,
        document_revision=revision,
        form=form,
        period_of_report=_date(_text(root, "periodOfReport", required=True)),
        issuer_cik=issuer_cik,
        issuer_name=_text(issuer, "issuerName", required=True),
        issuer_trading_symbol=_text(issuer, "issuerTradingSymbol"),
        reporting_owners=owners,
        entries=tuple(entries),
        footnotes=footnotes,
        available_at=revision.available_at,
        parsed_at=parsed_at.astimezone(UTC),
    )


def _owner(node):
    identity = _child(node, "reportingOwnerId", required=True)
    relation = _child(node, "reportingOwnerRelationship", required=True)
    cik = normalize_cik(_text(identity, "rptOwnerCik", required=True))
    return ReportingOwner(
        reporting_owner_id=ReportingOwner.expected_id(cik),
        cik=cik,
        name=_text(identity, "rptOwnerName", required=True),
        is_director=_bool(_text(relation, "isDirector")),
        is_officer=_bool(_text(relation, "isOfficer")),
        is_ten_percent_owner=_bool(_text(relation, "isTenPercentOwner")),
        is_other=_bool(_text(relation, "isOther")),
        officer_title=_text(relation, "officerTitle"),
        other_text=_text(relation, "otherText"),
    )


def _name(node):
    return node.tag.rsplit("}", 1)[-1]


def _children(node, name):
    return () if node is None else tuple(child for child in node if _name(child) == name)


def _child(node, name, required=False):
    found = _children(node, name)
    if len(found) > 1:
        raise SecOwnershipParserError(f"ambiguous XML {name}")
    if not found and required:
        raise SecOwnershipParserError(f"missing XML {name}")
    return found[0] if found else None


def _text(node, name, required=False):
    child = _child(node, name, required)
    value = None if child is None else (child.text or "").strip()
    if required and not value:
        raise SecOwnershipParserError(f"missing XML {name}")
    return value or None


def _value(node, name, required=False):
    child = _child(node, name, required)
    return _text(child, "value", required) if child is not None else None


def _bool(value):
    if value is None:
        return False
    if value in {"0", "false"}:
        return False
    if value in {"1", "true"}:
        return True
    raise SecOwnershipParserError("invalid XML boolean")


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise SecOwnershipParserError("invalid XML date") from error


def _decimal(value):
    if value is None:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise SecOwnershipParserError("invalid XML decimal") from error
    if not result.is_finite():
        raise SecOwnershipParserError("invalid XML decimal")
    return result
