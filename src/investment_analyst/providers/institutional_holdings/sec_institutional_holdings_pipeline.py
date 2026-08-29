"""Append-only importer for the isolated SEC Form 13F vertical."""

from __future__ import annotations

from dataclasses import dataclass

from investment_analyst.core.models import SourceDefinition, SourceType
from investment_analyst.evidence.sec_documents.models import (
    INSTITUTIONAL_HOLDINGS_FORMS,
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
    normalize_cik,
)
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    SEC_FILER_DOCUMENT_SOURCE_ID,
    SecFilerDocumentRepository,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_SOURCE_ID,
    InstitutionalHoldingsReport,
    InstitutionalHoldingsResolutionOutcome,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_resolver,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_index import (
    InstitutionalHoldingsFiling,
    institutional_holdings_filings,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    classify_institutional_holdings_resource,
    parse_institutional_holdings,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    manager_submissions_source_id,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class SecInstitutionalHoldingsPipelineError(StorageError):
    """A bounded Form 13F import cannot preserve its evidence contract."""


@dataclass(frozen=True, slots=True)
class SecInstitutionalHoldingsImportRequest:
    filer_cik: str
    forms: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    limit_per_form: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "filer_cik", normalize_cik(self.filer_cik))
        if bool(self.forms) == bool(self.accessions) or self.limit_per_form < 1:
            raise SecInstitutionalHoldingsPipelineError("invalid institutional selection")
        if (
            len(set(self.forms)) != len(self.forms)
            or len(set(self.accessions)) != len(self.accessions)
            or any(form not in INSTITUTIONAL_HOLDINGS_FORMS for form in self.forms)
        ):
            raise SecInstitutionalHoldingsPipelineError("invalid institutional selection")


class SecInstitutionalHoldingsPipeline:
    def __init__(self, storage, submissions_client, document_client) -> None:
        self._storage = storage
        self._submissions_client = submissions_client
        self._document_client = document_client

    def run(
        self, request: SecInstitutionalHoldingsImportRequest
    ) -> tuple[InstitutionalHoldingsReport, ...]:
        self._storage.require_open()
        self._upsert_sources(request.filer_cik)
        fetched_submissions = self._submissions_client.fetch(request.filer_cik)
        try:
            submissions = self._storage.raw_records.get(fetched_submissions.record_id)
        except RecordNotFoundError:
            submissions = self._storage.raw_records.save(fetched_submissions)
        filings = institutional_holdings_filings(submissions, request.filer_cik)
        selected = self._select(filings, request)
        documents = SecFilerDocumentRepository(self._storage.raw_records, self._storage.documents)
        holdings = InstitutionalHoldingsRepository(self._storage.raw_records)
        reports: list[InstitutionalHoldingsReport] = []
        for metadata in selected:
            report = self._import_one(
                metadata=metadata,
                filer_cik=request.filer_cik,
                discovery_raw_record_id=submissions.record_id,
                documents=documents,
                holdings=holdings,
            )
            if report is not None:
                reports.append(report)
        return tuple(reports)

    def _import_one(
        self,
        *,
        metadata: InstitutionalHoldingsFiling,
        filer_cik: str,
        discovery_raw_record_id,
        documents: SecFilerDocumentRepository,
        holdings: InstitutionalHoldingsRepository,
    ) -> InstitutionalHoldingsReport | None:
        filing = SecFiling(
            filing_id=SecFiling.expected_id(filer_cik, metadata.accession),
            filer_cik=filer_cik,
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
        resolved = sec_institutional_holdings_resolver.resolve_institutional_holdings_documents(
            self._document_client, locator
        )
        if resolved.cover is None or resolved.information_table is None:
            self._save_outcome(
                holdings,
                filing=filing,
                discovery_raw_record_id=discovery_raw_record_id,
                declared_locator=locator.name,
                resource_name=locator.name,
                response=resolved.locator,
                manifest=resolved.manifest,
                status="rejected",
                reason_code=resolved.rejection_reason or "not_exactly_two_top_level_xml",
            )
            return None
        if resolved.locator.url != resolved.cover.url:
            locator_classification = classify_institutional_holdings_resource(
                resolved.locator.content, role="cover"
            )
            self._save_outcome(
                holdings,
                filing=filing,
                discovery_raw_record_id=discovery_raw_record_id,
                declared_locator=locator.name,
                resource_name=locator.name,
                response=resolved.locator,
                manifest=resolved.manifest,
                status=locator_classification.status,
                reason_code=locator_classification.reason_code,
            )
        resources = (
            (resolved.cover, "cover"),
            (resolved.information_table, "information_table"),
        )
        classifications = []
        for response, role in resources:
            classification = classify_institutional_holdings_resource(response.content, role=role)
            classifications.append(classification)
            self._save_outcome(
                holdings,
                filing=filing,
                discovery_raw_record_id=discovery_raw_record_id,
                declared_locator=locator.name,
                resource_name=response.url.rsplit("/", 1)[-1],
                response=response,
                manifest=resolved.manifest,
                status=classification.status,
                reason_code=classification.reason_code,
            )
        if any(item.status != "accepted" for item in classifications):
            return None
        cover_revision = self._revision(
            response=resolved.cover,
            filing=filing,
            filer_cik=filer_cik,
            discovery_raw_record_id=discovery_raw_record_id,
            documents=documents,
        )
        table_revision = self._revision(
            response=resolved.information_table,
            filing=filing,
            filer_cik=filer_cik,
            discovery_raw_record_id=discovery_raw_record_id,
            documents=documents,
        )
        parsed_at = max(cover_revision.retrieved_at, table_revision.retrieved_at)
        report, positions = parse_institutional_holdings(
            resolved.cover.content,
            resolved.information_table.content,
            cover_revision=cover_revision,
            information_table_revision=table_revision,
            parsed_at=parsed_at,
        )
        report = holdings.save_report(report)
        for position in positions:
            holdings.save_position(position)
        return report

    def _revision(
        self,
        *,
        response,
        filing: SecFiling,
        filer_cik: str,
        discovery_raw_record_id,
        documents: SecFilerDocumentRepository,
    ) -> SecFilerDocumentRevision:
        name = response.url.rsplit("/", 1)[-1]
        document = SecLogicalDocument(
            document_id=SecLogicalDocument.expected_id(filing.filing_id, name),
            filing=filing,
            name=name,
        )
        revision_id = SecFilerDocumentRevision.expected_id(document.document_id, response.sha256)
        existing = documents.get_revision(revision_id)
        if existing is not None:
            return existing
        receipt = self._storage.documents.put(response.content)
        return documents.save(
            SecFilerDocumentRevision(
                revision_id=revision_id,
                filer_cik=filer_cik,
                document=document,
                raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(revision_id),
                discovery_raw_record_id=discovery_raw_record_id,
                content_sha256=receipt.sha256,
                content_size_bytes=receipt.size_bytes,
                available_at=filing.accepted_at,
                retrieved_at=response.retrieved_at,
                source_url=response.url,
            )
        )

    def _save_outcome(
        self,
        repository: InstitutionalHoldingsRepository,
        *,
        filing: SecFiling,
        discovery_raw_record_id,
        declared_locator: str,
        resource_name: str,
        response,
        manifest,
        status: str,
        reason_code: str,
    ) -> InstitutionalHoldingsResolutionOutcome:
        outcome_id = InstitutionalHoldingsResolutionOutcome.expected_id(
            filing.accession, resource_name, response.sha256, status
        )
        existing = repository.get_outcome(outcome_id)
        if existing is not None:
            return existing
        receipt = self._storage.documents.put(response.content)
        return repository.save_outcome(
            InstitutionalHoldingsResolutionOutcome(
                outcome_id=outcome_id,
                raw_record_id=InstitutionalHoldingsResolutionOutcome.expected_raw_record_id(
                    outcome_id
                ),
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

    @staticmethod
    def _select(
        filings: tuple[InstitutionalHoldingsFiling, ...],
        request: SecInstitutionalHoldingsImportRequest,
    ) -> tuple[InstitutionalHoldingsFiling, ...]:
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
                raise SecInstitutionalHoldingsPipelineError(
                    "requested accession is absent or ineligible"
                ) from error
        selected: list[InstitutionalHoldingsFiling] = []
        for form in sorted(request.forms):
            selected.extend(
                [item for item in filings if item.form == form][-request.limit_per_form :]
            )
        return tuple(sorted(selected, key=lambda item: (item.accepted_at, item.accession)))

    def _upsert_sources(self, filer_cik: str) -> None:
        sources = (
            SourceDefinition(
                source_id=manager_submissions_source_id(filer_cik),
                provider_name="U.S. Securities and Exchange Commission",
                dataset_name=f"EDGAR manager Submissions CIK {filer_cik}",
                source_type=SourceType.DOCUMENTS,
                base_url="https://data.sec.gov",
                is_official=True,
                coverage_notes="Manager filing metadata from filings.recent only.",
            ),
            SourceDefinition(
                source_id=SEC_FILER_DOCUMENT_SOURCE_ID,
                provider_name="U.S. Securities and Exchange Commission",
                dataset_name="EDGAR filer document revisions",
                source_type=SourceType.DOCUMENTS,
                base_url="https://www.sec.gov",
                is_official=True,
                coverage_notes="Exact filer document bytes without catalog asset linkage.",
            ),
            SourceDefinition(
                source_id=INSTITUTIONAL_HOLDINGS_SOURCE_ID,
                provider_name="U.S. Securities and Exchange Commission",
                dataset_name="EDGAR Form 13F institutional holdings",
                source_type=SourceType.DOCUMENTS,
                base_url="https://www.sec.gov",
                is_official=True,
                coverage_notes=(
                    "Structured Form 13F XML from filings.recent; positions are unlinked."
                ),
            ),
        )
        for source in sources:
            self._storage.sources.upsert(source)
