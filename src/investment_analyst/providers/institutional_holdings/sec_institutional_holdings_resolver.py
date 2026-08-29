"""Manifest-first resolution of the two structured Form 13F XML resources."""

from __future__ import annotations

from dataclasses import dataclass

from investment_analyst.evidence.sec_documents.models import SecLogicalDocument
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecDocumentClient,
    SecPrimaryDocumentResponse,
)


@dataclass(frozen=True, slots=True)
class ResolvedInstitutionalHoldingsDocuments:
    manifest: SecAccessionManifest
    locator: SecPrimaryDocumentResponse
    cover: SecPrimaryDocumentResponse | None
    information_table: SecPrimaryDocumentResponse | None
    rejection_reason: str | None


def resolve_institutional_holdings_documents(
    client: SecDocumentClient, document: SecLogicalDocument
) -> ResolvedInstitutionalHoldingsDocuments:
    manifest = client.fetch_manifest(document)
    locator = client.fetch(document)
    candidates = tuple(
        name for name in manifest.entries if "/" not in name and name.lower().endswith(".xml")
    )
    if len(candidates) != 2:
        return ResolvedInstitutionalHoldingsDocuments(
            manifest, locator, None, None, "not_exactly_two_top_level_xml"
        )
    declared_name = document.name.rsplit("/", 1)[-1]
    if declared_name not in candidates:
        return ResolvedInstitutionalHoldingsDocuments(
            manifest, locator, None, None, "declared_cover_is_not_top_level_xml"
        )
    cover_document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(document.filing.filing_id, declared_name),
        filing=document.filing,
        name=declared_name,
    )
    cover = locator if document.name == declared_name else client.fetch(cover_document)
    table_name = next(name for name in candidates if name != declared_name)
    table_document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(document.filing.filing_id, table_name),
        filing=document.filing,
        name=table_name,
    )
    return ResolvedInstitutionalHoldingsDocuments(
        manifest=manifest,
        locator=locator,
        cover=cover,
        information_table=client.fetch(table_document),
        rejection_reason=None,
    )
