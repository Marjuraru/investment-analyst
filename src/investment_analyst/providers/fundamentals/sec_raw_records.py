"""Apple SEC source definitions and immutable raw snapshot conversion."""

import json
from uuid import UUID, uuid5

from investment_analyst.core.models import (
    Asset,
    AssetClass,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    APPLE_CIK,
    APPLE_ENTITY_NAME,
    APPLE_TICKER,
    OFFICIAL_BASE_URL,
    SecDocumentType,
    SecEdgarDocument,
)

ASSET_ID = "equity:us:aapl"
SUBMISSIONS_SOURCE_ID = "sec-edgar:aapl:submissions"
COMPANY_FACTS_SOURCE_ID = "sec-edgar:aapl:companyfacts"
_SUBMISSIONS_SCHEMA_VERSION = "sec-edgar-submissions-snapshot-v1"
_COMPANY_FACTS_SCHEMA_VERSION = "sec-edgar-companyfacts-snapshot-v1"
_RAW_RECORD_NAMESPACE = UUID("7d324554-a32e-5c56-9dcf-ae8e72ebf3dc")


class SecRawRecordError(ValueError):
    """Invalid SEC source, asset, or raw-record conversion input."""


def create_sec_aapl_asset(existing: Asset | None = None) -> Asset:
    """Create the canonical AAPL asset while preserving existing provider symbols."""
    if existing is not None and existing.asset_id != ASSET_ID:
        raise SecRawRecordError("existing asset must use the stable AAPL asset_id")
    provider_symbols = dict(existing.provider_symbols) if existing is not None else {}
    provider_symbols["sec_cik"] = APPLE_CIK
    return Asset(
        asset_id=ASSET_ID,
        symbol=APPLE_TICKER,
        name=APPLE_ENTITY_NAME,
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
        provider_symbols=provider_symbols,
        is_active=existing.is_active if existing is not None else True,
    )


def create_sec_submissions_source() -> SourceDefinition:
    """Return the official Apple EDGAR submissions source contract."""
    return SourceDefinition(
        source_id=SUBMISSIONS_SOURCE_ID,
        provider_name="U.S. Securities and Exchange Commission",
        dataset_name="Apple EDGAR Submissions",
        source_type=SourceType.FUNDAMENTALS,
        base_url=OFFICIAL_BASE_URL,
        is_official=True,
        coverage_notes=(
            "Official SEC EDGAR snapshot containing filing history and metadata. "
            "Periods, revisions, and accounting concepts are not resolved yet; snapshots may "
            "change after new filings or corrections and do not constitute financial advice."
        ),
    )


def create_sec_company_facts_source() -> SourceDefinition:
    """Return the official Apple EDGAR XBRL company-facts source contract."""
    return SourceDefinition(
        source_id=COMPANY_FACTS_SOURCE_ID,
        provider_name="U.S. Securities and Exchange Commission",
        dataset_name="Apple EDGAR XBRL Company Facts",
        source_type=SourceType.FUNDAMENTALS,
        base_url=OFFICIAL_BASE_URL,
        is_official=True,
        coverage_notes=(
            "Official SEC EDGAR snapshot aggregating XBRL concepts from multiple filings. "
            "Periods, revisions, taxonomies, units, and accounting concepts are not resolved yet; "
            "snapshots may change after new filings or corrections and do not constitute financial "
            "advice."
        ),
    )


def get_sec_source_definitions() -> tuple[SourceDefinition, SourceDefinition]:
    """Return both fixed source definitions in deterministic order."""
    return create_sec_submissions_source(), create_sec_company_facts_source()


def sec_document_to_raw_record(document: SecEdgarDocument) -> RawRecord:
    """Convert one validated SEC document into a deterministic raw snapshot record."""
    source_id, schema_version = _source_and_schema(document.document_type)
    identity = json.dumps(
        {
            "source_id": source_id,
            "document_type": document.document_type.value,
            "cik": document.cik,
            "body_sha256": document.body_sha256,
            "schema_version": schema_version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    record_id = uuid5(_RAW_RECORD_NAMESPACE, identity)
    return RawRecord(
        record_id=record_id,
        asset_id=ASSET_ID,
        source=SourceReference(
            source_id=source_id,
            record_key=(
                f"CIK{document.cik}:{document.document_type.value}:{document.body_sha256[:16]}"
            ),
            retrieved_at=document.retrieved_at,
            raw_uri=document.request_url,
            checksum_sha256=document.body_sha256,
        ),
        event_time=document.retrieved_at,
        available_at=document.retrieved_at,
        received_at=document.retrieved_at,
        payload={
            "document_type": document.document_type.value,
            "cik": document.cik,
            "entity_name": document.entity_name,
            "body_sha256": document.body_sha256,
            "content_length": document.content_length,
            "document": document.body,
        },
        schema_version=schema_version,
    )


def expected_record_id(document: SecEdgarDocument) -> UUID:
    """Return the deterministic identifier expected for a validated SEC document."""
    return sec_document_to_raw_record(document).record_id


def _source_and_schema(document_type: SecDocumentType) -> tuple[str, str]:
    if document_type is SecDocumentType.SUBMISSIONS:
        return SUBMISSIONS_SOURCE_ID, _SUBMISSIONS_SCHEMA_VERSION
    if document_type is SecDocumentType.COMPANY_FACTS:
        return COMPANY_FACTS_SOURCE_ID, _COMPANY_FACTS_SCHEMA_VERSION
    raise SecRawRecordError(f"unsupported SEC document type: {document_type}")
