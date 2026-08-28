"""Append-only Section 16 importer with manifest-first resource resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from investment_analyst.core.models import SourceDefinition, SourceType
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_to_raw_record,
)
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_FORMS,
    OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
    OWNERSHIP_SCHEMA_VERSION_V2,
    OWNERSHIP_SOURCE_ID,
    OwnershipResolutionOutcome,
    OwnershipStatement,
)
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.providers.fundamentals.sec_fact_models import SUBMISSIONS_SCHEMA_VERSION
from investment_analyst.providers.ownership.sec_ownership_index import ownership_filings
from investment_analyst.providers.ownership.sec_ownership_parser import (
    classify_ownership_resource,
    parse_ownership_statement,
)
from investment_analyst.storage import StorageError


class SecOwnershipPipelineError(StorageError):
    """A bounded ownership import cannot preserve its evidence contract."""


@dataclass(frozen=True, slots=True)
class SecOwnershipImportRequest:
    forms: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    limit_per_form: int = 1

    def __post_init__(self) -> None:
        if bool(self.forms) == bool(self.accessions) or self.limit_per_form < 1:
            raise SecOwnershipPipelineError("invalid ownership selection")
        if (
            len(set(self.forms)) != len(self.forms)
            or len(set(self.accessions)) != len(self.accessions)
            or any(form not in OWNERSHIP_FORMS for form in self.forms)
        ):
            raise SecOwnershipPipelineError("invalid ownership selection")


class SecOwnershipPipeline:
    """Complete each accession before continuing to the next one."""

    def __init__(self, storage, client, *, configuration) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration

    def run(self, request: SecOwnershipImportRequest) -> tuple[OwnershipStatement, ...]:
        self._storage.require_open()
        submissions = self._latest_submissions()
        selected = self._select(ownership_filings(submissions, self._configuration), request)
        documents = SecDocumentRepository(self._storage.raw_records, self._storage.documents)
        ownership = OwnershipRepository(self._storage.raw_records)
        self._storage.sources.upsert(
            SourceDefinition(
                source_id=OWNERSHIP_SOURCE_ID,
                provider_name="U.S. Securities and Exchange Commission",
                dataset_name="EDGAR Section 16 ownership filings",
                source_type=SourceType.DOCUMENTS,
                base_url="https://www.sec.gov",
                is_official=True,
                coverage_notes="Forms 3, 4, and 5 resolved from official accession manifests.",
            )
        )
        statements: list[OwnershipStatement] = []
        for metadata in selected:
            filing = SecFiling(
                filing_id=SecFiling.expected_id(self._configuration.cik, metadata.accession),
                filer_cik=self._configuration.cik,
                accession=metadata.accession,
                form=metadata.form,
                filing_date=metadata.filing_date,
                report_date=metadata.report_date,
                accepted_at=metadata.accepted_at,
                is_amendment=metadata.form.endswith("/A"),
            )
            locator = SecLogicalDocument(
                document_id=SecLogicalDocument.expected_id(
                    filing.filing_id, metadata.primary_document
                ),
                filing=filing,
                name=metadata.primary_document,
            )
            resolved = self._client.resolve_ownership_document(locator)
            self._save_outcome(
                ownership,
                filing,
                submissions.record_id,
                locator.name,
                locator.name,
                resolved.locator,
                resolved.manifest,
            )
            classification = classify_ownership_resource(resolved.semantic.content)
            semantic_name = resolved.semantic.url.rsplit("/", 1)[-1]
            self._save_outcome(
                ownership,
                filing,
                submissions.record_id,
                locator.name,
                semantic_name,
                resolved.semantic,
                resolved.manifest,
                status=classification.status,
                reason_code=classification.reason_code,
            )
            if classification.status != "accepted":
                continue
            semantic_document = SecLogicalDocument(
                document_id=SecLogicalDocument.expected_id(filing.filing_id, semantic_name),
                filing=filing,
                name=semantic_name,
            )
            revision_id = SecDocumentRevision.expected_id(
                semantic_document.document_id,
                resolved.semantic.sha256,
                REVISION_SCHEMA_VERSION_V2,
            )
            revision = documents.get_revision(revision_id)
            if revision is None:
                receipt = self._storage.documents.put(resolved.semantic.content)
                revision = SecDocumentRevision(
                    revision_id=revision_id,
                    asset_id=self._configuration.asset_id,
                    document=semantic_document,
                    raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
                    discovery_raw_record_id=submissions.record_id,
                    content_sha256=receipt.sha256,
                    content_size_bytes=receipt.size_bytes,
                    available_at=filing.accepted_at,
                    retrieved_at=resolved.semantic.retrieved_at,
                    source_url=resolved.semantic.url,
                    revision_schema_version=REVISION_SCHEMA_VERSION_V2,
                )
                self._storage.raw_records.save(revision_to_raw_record(revision))
            statement_id = OwnershipStatement.expected_id(
                revision.revision_id, OWNERSHIP_SCHEMA_VERSION_V2
            )
            statement = ownership.get(statement_id)
            if statement is None:
                statement = parse_ownership_statement(
                    resolved.semantic.content,
                    asset_id=self._configuration.asset_id,
                    revision=revision,
                    parsed_at=datetime.now(UTC),
                )
                ownership.save(statement)
            statements.append(statement)
        return tuple(statements)

    def _latest_submissions(self):
        records = self._storage.raw_records.list(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.submissions_source_id,
            schema_version=SUBMISSIONS_SCHEMA_VERSION,
        )
        if not records:
            raise SecOwnershipPipelineError("no persisted SEC submissions snapshot is eligible")
        return max(
            records,
            key=lambda item: (item.available_at, item.received_at, str(item.record_id)),
        )

    @staticmethod
    def _select(filings, request: SecOwnershipImportRequest):
        by_accession = {item.accession: item for item in filings}
        if request.accessions:
            try:
                return tuple(
                    sorted(
                        (by_accession[item] for item in request.accessions),
                        key=lambda item: (item.accepted_at, item.accession),
                    )
                )
            except KeyError as error:
                raise SecOwnershipPipelineError(
                    "requested accession is absent or ineligible"
                ) from error
        selected = []
        for form in sorted(request.forms):
            candidates = [item for item in filings if item.form == form]
            selected.extend(candidates[-request.limit_per_form :])
        return tuple(sorted(selected, key=lambda item: (item.accepted_at, item.accession)))

    def _save_outcome(
        self,
        repository,
        filing,
        discovery_raw_record_id,
        declared_locator,
        resource_name,
        response,
        manifest,
        *,
        status=None,
        reason_code=None,
    ) -> OwnershipResolutionOutcome:
        classification = classify_ownership_resource(response.content)
        outcome_status = status or classification.status
        outcome_reason = reason_code or classification.reason_code
        outcome_id = OwnershipResolutionOutcome.expected_id(
            filing.accession,
            resource_name,
            response.sha256,
            outcome_status,
            OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
        )
        existing = repository.get_outcome(outcome_id)
        if existing is not None:
            return existing
        receipt = self._storage.documents.put(response.content)
        outcome = OwnershipResolutionOutcome(
            outcome_id=outcome_id,
            raw_record_id=OwnershipResolutionOutcome.expected_raw_record_id(outcome_id),
            asset_id=self._configuration.asset_id,
            filing=filing,
            discovery_raw_record_id=discovery_raw_record_id,
            declared_locator=declared_locator,
            resource_name=resource_name,
            resource_url=response.url,
            content_sha256=receipt.sha256,
            content_size_bytes=receipt.size_bytes,
            manifest_url=manifest.url,
            manifest_sha256=manifest.sha256,
            available_at=filing.accepted_at,
            retrieved_at=response.retrieved_at,
            status=outcome_status,
            reason_code=outcome_reason,
            resolver_version="sec-ownership-resolver-v2",
            schema_version=OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
        )
        return repository.save_outcome(outcome)
