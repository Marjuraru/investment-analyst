"""Manifest-first, exact-byte resolution for Schedule 13D/13G representations."""

from __future__ import annotations

from dataclasses import dataclass

from investment_analyst.evidence.sec_documents.models import SecLogicalDocument
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecDocumentClient,
    SecPrimaryDocumentResponse,
)


@dataclass(frozen=True, slots=True)
class ResolvedBeneficialOwnershipDocument:
    manifest: SecAccessionManifest
    locator: SecPrimaryDocumentResponse
    semantic: SecPrimaryDocumentResponse | None
    rejection_reason: str | None


def resolve_beneficial_ownership_document(
    client: SecDocumentClient, document: SecLogicalDocument
) -> ResolvedBeneficialOwnershipDocument:
    """Resolve one top-level XML, retaining the declared locator in every outcome path."""
    manifest = client.fetch_manifest(document)
    locator = client.fetch(document)
    candidates = tuple(
        name for name in manifest.entries if "/" not in name and name.lower().endswith(".xml")
    )
    if len(candidates) != 1:
        return ResolvedBeneficialOwnershipDocument(
            manifest=manifest,
            locator=locator,
            semantic=None,
            rejection_reason="no_unique_top_level_xml",
        )
    semantic_name = candidates[0]
    semantic_document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(document.filing.filing_id, semantic_name),
        filing=document.filing,
        name=semantic_name,
    )
    semantic = (
        locator if semantic_document.name == document.name else client.fetch(semantic_document)
    )
    return ResolvedBeneficialOwnershipDocument(
        manifest=manifest,
        locator=locator,
        semantic=semantic,
        rejection_reason=None,
    )
