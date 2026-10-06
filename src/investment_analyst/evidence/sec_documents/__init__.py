"""SEC primary-document corpus contracts."""

from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SCHEMA_VERSION_V2,
    SEC_DOCUMENT_SCHEMA_VERSION_V3,
    SEC_DOCUMENT_SCHEMA_VERSION_V4,
    SEC_DOCUMENT_SOURCE_ID,
    SecDocumentAcquisitionRevision,
    SecDocumentMetadataRevision,
    SecDocumentQuery,
    SecDocumentReplay,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
    SecTerminalScriptDifference,
)

__all__ = [
    "SEC_DOCUMENT_SCHEMA_VERSION",
    "SEC_DOCUMENT_SCHEMA_VERSION_V2",
    "SEC_DOCUMENT_SCHEMA_VERSION_V3",
    "SEC_DOCUMENT_SCHEMA_VERSION_V4",
    "SEC_DOCUMENT_SOURCE_ID",
    "SecDocumentQuery",
    "SecDocumentAcquisitionRevision",
    "SecDocumentMetadataRevision",
    "SecDocumentReplay",
    "SecDocumentRevision",
    "SecFiling",
    "SecLogicalDocument",
    "SecTerminalScriptDifference",
]
