"""Append-only importer for the isolated SEC Schedule 13D/13G vertical."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from investment_analyst.core.models import SourceDefinition, SourceType
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
    BeneficialOwnershipResolutionOutcome,
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_documents.models import (
    BENEFICIAL_OWNERSHIP_FORMS,
    REVISION_SCHEMA_VERSION_V2,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_to_raw_record,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_index import (
    BeneficialOwnershipFiling,
    beneficial_ownership_filings,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_parser import (
    classify_beneficial_ownership_resource,
    parse_beneficial_ownership_statement,
)
from investment_analyst.providers.beneficial_ownership.sec_beneficial_ownership_resolver import (
    resolve_beneficial_ownership_document,
)
from investment_analyst.providers.fundamentals.sec_fact_models import SUBMISSIONS_SCHEMA_VERSION
from investment_analyst.storage import StorageError


class SecBeneficialOwnershipPipelineError(StorageError):
    """A bounded import cannot preserve the declared evidence contract."""


@dataclass(frozen=True, slots=True)
class SecBeneficialOwnershipImportRequest:
    forms: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    limit_per_form: int = 1

    def __post_init__(self) -> None:
        if bool(self.forms) == bool(self.accessions) or self.limit_per_form < 1:
            raise SecBeneficialOwnershipPipelineError("invalid beneficial ownership selection")
        if (
            len(set(self.forms)) != len(self.forms)
            or len(set(self.accessions)) != len(self.accessions)
            or any(form not in BENEFICIAL_OWNERSHIP_FORMS for form in self.forms)
        ):
            raise SecBeneficialOwnershipPipelineError("invalid beneficial ownership selection")


class SecBeneficialOwnershipPipeline:
    def __init__(self, storage, client, *, configuration) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration

    def run(
        self, request: SecBeneficialOwnershipImportRequest
    ) -> tuple[BeneficialOwnershipStatement, ...]:
        self._storage.require_open()
        submissions = self._latest_submissions()
        selected = self._select(
            beneficial_ownership_filings(submissions, self._configuration), request
        )
        documents = SecDocumentRepository(self._storage.raw_records, self._storage.documents)
        ownership = BeneficialOwnershipRepository(self._storage.raw_records)
        self._storage.sources.upsert(
            SourceDefinition(
                source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
                provider_name="U.S. Securities and Exchange Commission",
                dataset_name="EDGAR Schedule 13D/13G beneficial ownership filings",
                source_type=SourceType.DOCUMENTS,
                base_url="https://www.sec.gov",
                is_official=True,
                coverage_notes=(
                    "Schedules 13D and 13G from filings.recent; only one top-level XML is parsed."
                ),
            )
        )
        statements: list[BeneficialOwnershipStatement] = []
        for metadata in selected:
            statement = self._import_one(
                metadata=metadata,
                discovery_raw_record_id=submissions.record_id,
                documents=documents,
                ownership=ownership,
            )
            if statement is not None:
                statements.append(statement)
        return tuple(statements)

    def _import_one(
        self,
        *,
        metadata: BeneficialOwnershipFiling,
        discovery_raw_record_id,
        documents: SecDocumentRepository,
        ownership: BeneficialOwnershipRepository,
    ) -> BeneficialOwnershipStatement | None:
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
            document_id=SecLogicalDocument.expected_id(filing.filing_id, metadata.primary_document),
            filing=filing,
            name=metadata.primary_document,
        )
        resolved = resolve_beneficial_ownership_document(self._client, locator)
        if resolved.semantic is None:
            self._save_outcome(
                ownership,
                filing=filing,
                discovery_raw_record_id=discovery_raw_record_id,
                declared_locator=locator.name,
                resource_name=locator.name,
                response=resolved.locator,
                manifest=resolved.manifest,
                status="rejected",
                reason_code=resolved.rejection_reason or "no_unique_top_level_xml",
            )
            return None
        locator_classification = classify_beneficial_ownership_resource(resolved.locator.content)
        self._save_outcome(
            ownership,
            filing=filing,
            discovery_raw_record_id=discovery_raw_record_id,
            declared_locator=locator.name,
            resource_name=locator.name,
            response=resolved.locator,
            manifest=resolved.manifest,
            status=locator_classification.status,
            reason_code=locator_classification.reason_code,
        )
        semantic_name = resolved.semantic.url.rsplit("/", 1)[-1]
        semantic_classification = classify_beneficial_ownership_resource(resolved.semantic.content)
        self._save_outcome(
            ownership,
            filing=filing,
            discovery_raw_record_id=discovery_raw_record_id,
            declared_locator=locator.name,
            resource_name=semantic_name,
            response=resolved.semantic,
            manifest=resolved.manifest,
            status=semantic_classification.status,
            reason_code=semantic_classification.reason_code,
        )
        if semantic_classification.status != "accepted":
            return None
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
                discovery_raw_record_id=discovery_raw_record_id,
                content_sha256=receipt.sha256,
                content_size_bytes=receipt.size_bytes,
                available_at=filing.accepted_at,
                retrieved_at=resolved.semantic.retrieved_at,
                source_url=resolved.semantic.url,
                revision_schema_version=REVISION_SCHEMA_VERSION_V2,
            )
            self._storage.raw_records.save(revision_to_raw_record(revision))
        statement_id = BeneficialOwnershipStatement.expected_id(revision.revision_id)
        existing = ownership.get(statement_id)
        if existing is not None:
            return existing
        statement = parse_beneficial_ownership_statement(
            resolved.semantic.content,
            asset_id=self._configuration.asset_id,
            revision=revision,
            parsed_at=datetime.now(UTC),
        )
        return ownership.save(statement)

    def _latest_submissions(self):
        records = self._storage.raw_records.list(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.submissions_source_id,
            schema_version=SUBMISSIONS_SCHEMA_VERSION,
        )
        if not records:
            raise SecBeneficialOwnershipPipelineError(
                "no persisted SEC submissions snapshot is eligible"
            )
        return max(
            records,
            key=lambda item: (item.available_at, item.received_at, str(item.record_id)),
        )

    @staticmethod
    def _select(
        filings: tuple[BeneficialOwnershipFiling, ...], request: SecBeneficialOwnershipImportRequest
    ) -> tuple[BeneficialOwnershipFiling, ...]:
        by_accession = {item.accession: item for item in filings}
        if request.accessions:
            try:
                return tuple(
                    sorted(
                        (by_accession[accession] for accession in request.accessions),
                        key=lambda item: (item.accepted_at, item.accession),
                    )
                )
            except KeyError as error:
                raise SecBeneficialOwnershipPipelineError(
                    "requested accession is absent or ineligible"
                ) from error
        selected: list[BeneficialOwnershipFiling] = []
        for form in sorted(request.forms):
            selected.extend(
                [item for item in filings if item.form == form][-request.limit_per_form :]
            )
        return tuple(sorted(selected, key=lambda item: (item.accepted_at, item.accession)))

    def _save_outcome(
        self,
        repository: BeneficialOwnershipRepository,
        *,
        filing: SecFiling,
        discovery_raw_record_id,
        declared_locator: str,
        resource_name: str,
        response,
        manifest,
        status: str,
        reason_code: str,
    ) -> BeneficialOwnershipResolutionOutcome:
        outcome_id = BeneficialOwnershipResolutionOutcome.expected_id(
            filing.accession, resource_name, response.sha256, status
        )
        existing = repository.get_outcome(outcome_id)
        if existing is not None:
            return existing
        receipt = self._storage.documents.put(response.content)
        return repository.save_outcome(
            BeneficialOwnershipResolutionOutcome(
                outcome_id=outcome_id,
                raw_record_id=BeneficialOwnershipResolutionOutcome.expected_raw_record_id(
                    outcome_id
                ),
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
                status=status,
                reason_code=reason_code,
            )
        )
