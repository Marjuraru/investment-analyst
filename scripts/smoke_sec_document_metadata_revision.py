#!/usr/bin/env python3
"""Verify five-issuer SEC metadata corrections and backup recovery in scratch workspaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_submissions_refresh import (
    SecSubmissionsRefreshService,
)
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.core.models import RawRecord, SourceDefinition, SourceType
from investment_analyst.evidence.sec_documents.models import (
    REVISION_SCHEMA_VERSION,
    REVISION_SCHEMA_VERSION_V2,
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SCHEMA_VERSION_V2,
    SEC_DOCUMENT_SCHEMA_VERSION_V3,
    SEC_DOCUMENT_SCHEMA_VERSION_V4,
    SEC_DOCUMENT_SOURCE_ID,
    SecAssetDocumentRevision,
    SecDocumentAcquisitionRevision,
    SecDocumentMetadataRevision,
    SecDocumentQuery,
    SecDocumentRevision,
    SecLogicalDocument,
    same_document_metadata_except_accepted_at,
    verify_sec_terminal_script_difference,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_from_raw_record,
    revision_to_raw_record,
)
from investment_analyst.evidence.sec_documents.service import SecDocumentCorpusService
from investment_analyst.evidence.sec_documents.timeline_models import (
    SecDocumentTimelineQuery,
)
from investment_analyst.evidence.sec_documents.timeline_service import (
    SecDocumentTimelineService,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.fundamentals.sec_document_pipeline import (
    SecDocumentImportRequest,
    SecDocumentImportSummary,
    SecDocumentPipeline,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    SUBMISSIONS_SCHEMA_VERSION,
)
from investment_analyst.providers.fundamentals.sec_filing_index import SecFilingIndex
from investment_analyst.providers.fundamentals.sec_raw_records import (
    create_sec_asset,
    create_sec_submissions_source,
)
from investment_analyst.providers.http import HttpResponse, HttpTransport, UrlLibHttpTransport
from investment_analyst.storage import LocalStorage
from investment_analyst.workspace.backup import WorkspaceBackupService
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_EXPECTED_OLD_ACCEPTED_AT: dict[str, dict[str, str]] = {
    "equity:us:mstr": {
        "0001050446-26-000020": "2026-02-19T22:18:45Z",
        "0001050446-26-000044": "2026-08-03T20:49:56Z",
    },
    "equity:us:aapl": {
        "0000320193-25-000079": "2025-10-31T10:01:26Z",
        "0000320193-26-000020": "2026-07-31T10:01:02Z",
    },
    "equity:us:amzn": {
        "0001018724-26-000004": "2026-02-05T23:44:31Z",
        "0001018724-26-000026": "2026-07-30T22:11:13Z",
    },
    "equity:us:cvx": {
        "0000093410-26-000078": "2026-02-24T20:03:12Z",
        "0000093410-26-000167": "2026-08-06T15:12:47Z",
        "0000093410-22-000028": "2022-05-04T21:21:50Z",
    },
    "equity:us:pltr": {
        "0001321655-26-000011": "2026-02-17T11:14:19Z",
        "0001321655-26-000041": "2026-08-03T22:06:38Z",
    },
}
_RECENT_COLUMNS = (
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "form",
    "primaryDocument",
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class _DocumentSeed:
    accession: str
    prior_revision: SecDocumentRevision
    prior_raw_record: RawRecord
    prior_submissions: RawRecord
    legacy_v1_raw_record: RawRecord


@dataclass(frozen=True, slots=True)
class _IssuerSeed:
    configuration: SecAssetConfiguration
    documents: tuple[_DocumentSeed, ...]


class _OfficialSecSmokeTransport:
    """Count and constrain the authorized Submissions and selected Archives requests."""

    def __init__(self, inner: HttpTransport) -> None:
        self._inner = inner
        self._configuration: SecAssetConfiguration | None = None
        self._allowed_archives: frozenset[str] = frozenset()
        self._archives_enabled = False
        self._next_request_at = 0.0
        self._pace_lock = threading.Lock()
        self.submissions_by_asset: Counter[str] = Counter()
        self.archive_paths: Counter[str] = Counter()

    def set_scope(
        self,
        configuration: SecAssetConfiguration,
        documents: tuple[SecLogicalDocument, ...],
        *,
        allow_archives: bool,
    ) -> None:
        self._configuration = configuration
        self._allowed_archives = frozenset(_archive_path(document) for document in documents)
        self._archives_enabled = allow_archives

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        configuration = self._configuration
        if configuration is None:
            raise RuntimeError("SEC smoke request scope is not configured")
        if not headers.get("User-Agent", "").strip():
            raise RuntimeError("SEC request omitted the configured User-Agent")
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise RuntimeError("SEC smoke permits HTTPS only")
        if parsed.hostname == "data.sec.gov":
            expected_path = f"/submissions/CIK{configuration.cik}.json"
            if parsed.path != expected_path or parsed.query or parsed.fragment:
                raise RuntimeError("SEC smoke blocked an out-of-scope Submissions request")
            self.submissions_by_asset[configuration.asset_id] += 1
        elif parsed.hostname == "www.sec.gov":
            if (
                not self._archives_enabled
                or parsed.path not in self._allowed_archives
                or parsed.query
                or parsed.fragment
            ):
                raise RuntimeError("SEC smoke blocked an out-of-scope Archives request")
            self.archive_paths[parsed.path] += 1
        else:
            raise RuntimeError("SEC smoke blocked a request to an unapproved host")
        self._pace_request()
        return self._inner.get(
            url,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )

    def _pace_request(self) -> None:
        with self._pace_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request_at = time.monotonic() + 0.12


def _archive_path(document: SecLogicalDocument) -> str:
    cik = str(int(document.filing.filer_cik))
    accession = document.filing.accession.replace("-", "")
    return f"/Archives/edgar/data/{cik}/{accession}/{document.name}"


def _utc_literal(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("SEC timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _submission_row(record: RawRecord, accession: str) -> tuple[dict[str, str], str, str]:
    payload = record.payload
    if not isinstance(payload, dict):
        raise ValueError("SEC Submissions payload is not an object")
    document = payload.get("document")
    if not isinstance(document, dict):
        raise ValueError("SEC Submissions document is missing")
    filings = document.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise ValueError("SEC Submissions recent filings are missing")
    accessions = recent.get("accessionNumber")
    if not isinstance(accessions, list):
        raise ValueError("SEC Submissions accession list is malformed")
    matches = [index for index, value in enumerate(accessions) if value == accession]
    if len(matches) != 1:
        raise ValueError("SEC Submissions does not uniquely contain the selected accession")
    index = matches[0]
    row: dict[str, str] = {}
    for column in _RECENT_COLUMNS:
        values = recent.get(column)
        if not isinstance(values, list) or index >= len(values):
            raise ValueError("SEC Submissions metadata columns are incomplete")
        value = values[index]
        if not isinstance(value, str):
            raise ValueError("SEC Submissions filing metadata is not text")
        row[column] = value
    entity_name = payload.get("entity_name")
    cik = payload.get("cik")
    if not isinstance(entity_name, str) or not isinstance(cik, str):
        raise ValueError("SEC Submissions issuer identity is malformed")
    return row, entity_name, cik


def _metadata_without_acceptance(
    row: dict[str, str], entity_name: str, cik: str
) -> tuple[tuple[str, str], ...]:
    entries = [("entity_name", entity_name), ("cik", cik)]
    entries.extend((key, value) for key, value in row.items() if key != "acceptanceDateTime")
    return tuple(sorted(entries))


def _document_source() -> SourceDefinition:
    return SourceDefinition(
        source_id=SEC_DOCUMENT_SOURCE_ID,
        provider_name="U.S. Securities and Exchange Commission",
        dataset_name="EDGAR primary filing documents",
        source_type=SourceType.DOCUMENTS,
        base_url="https://www.sec.gov",
        is_official=True,
        coverage_notes="Selected primary 10-K, 10-Q, 20-F, and 40-F filings only.",
    )


def _load_source_seeds(
    runtime: ApplicationRuntime,
    source_workspace: Path,
) -> tuple[_IssuerSeed, ...]:
    configurations = tuple(
        resolve_sec_configuration(runtime.provider_resolver, asset_id=asset_id)
        for asset_id in _EXPECTED_OLD_ACCEPTED_AT
    )
    location = StorageLocationRequest(workspace=source_workspace)
    seeds: list[_IssuerSeed] = []
    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_ONLY) as storage:
        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        for configuration in configurations:
            expected = _EXPECTED_OLD_ACCEPTED_AT[configuration.asset_id]
            records = storage.raw_records.list(
                asset_id=configuration.asset_id,
                source_id=SEC_DOCUMENT_SOURCE_ID,
                schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V2,
            )
            by_accession: dict[str, list[SecDocumentRevision]] = {
                accession: [] for accession in expected
            }
            for record in records:
                revision = revision_from_raw_record(record)
                if (
                    isinstance(revision, SecDocumentRevision)
                    and revision.revision_schema_version == REVISION_SCHEMA_VERSION_V2
                    and revision.document.filing.accession in by_accession
                ):
                    by_accession[revision.document.filing.accession].append(revision)
            document_seeds: list[_DocumentSeed] = []
            prior_submissions_ids: set[UUID] = set()
            for accession, expected_literal in expected.items():
                candidates = by_accession[accession]
                unique = {item.revision_id: item for item in candidates}
                if len(unique) != 1:
                    raise ValueError(
                        f"source workspace must contain exactly one v2 revision for {accession}"
                    )
                prior = next(iter(unique.values()))
                if prior.document.filing.accepted_at != _utc_literal(expected_literal):
                    raise ValueError(
                        f"source accepted_at differs from the declared prior for {accession}"
                    )
                prior_record = storage.raw_records.get(prior.raw_record_id)
                submissions = storage.raw_records.get(prior.discovery_raw_record_id)
                if submissions.schema_version != SUBMISSIONS_SCHEMA_VERSION:
                    raise ValueError("prior document does not reference a Submissions snapshot")
                old_index = SecFilingIndex.from_raw_record(submissions, configuration)
                old_metadata = old_index.get(accession)
                if (
                    old_metadata is None
                    or old_metadata.acceptance_at != prior.document.filing.accepted_at
                    or old_metadata.primary_document != prior.document.name
                    or old_metadata.form != prior.document.filing.form
                    or old_metadata.filing_date != prior.document.filing.filing_date
                    or old_metadata.report_date != prior.document.filing.report_date
                ):
                    raise ValueError("prior Submissions metadata does not match its document")
                repository.verify_revision(prior)
                prior_submissions_ids.add(submissions.record_id)
                legacy_revision = SecDocumentRevision(
                    revision_id=SecDocumentRevision.expected_id(
                        prior.document.document_id,
                        prior.content_sha256,
                        REVISION_SCHEMA_VERSION,
                    ),
                    asset_id=prior.asset_id,
                    document=prior.document,
                    raw_record_id=SecDocumentRevision.expected_raw_record_id(
                        SecDocumentRevision.expected_id(
                            prior.document.document_id,
                            prior.content_sha256,
                            REVISION_SCHEMA_VERSION,
                        )
                    ),
                    discovery_raw_record_id=prior.discovery_raw_record_id,
                    content_sha256=prior.content_sha256,
                    content_size_bytes=prior.content_size_bytes,
                    available_at=prior.retrieved_at,
                    retrieved_at=prior.retrieved_at,
                    source_url=prior.source_url,
                    revision_schema_version=REVISION_SCHEMA_VERSION,
                )
                document_seeds.append(
                    _DocumentSeed(
                        accession=accession,
                        prior_revision=prior,
                        prior_raw_record=prior_record,
                        prior_submissions=submissions,
                        legacy_v1_raw_record=revision_to_raw_record(legacy_revision),
                    )
                )
            if len(prior_submissions_ids) != 1:
                raise ValueError(
                    f"source workspace must expose one prior Submissions snapshot for "
                    f"{configuration.asset_id}"
                )
            seeds.append(
                _IssuerSeed(
                    configuration=configuration,
                    documents=tuple(document_seeds),
                )
            )
    if sum(len(issuer.documents) for issuer in seeds) != 11:
        raise ValueError("the selected SEC source inventory must contain exactly 11 documents")
    return tuple(seeds)


def _seed_original_documents(
    storage: LocalStorage,
    source_storage: LocalStorage,
    seeds: tuple[_IssuerSeed, ...],
) -> None:
    saved_submissions: set[UUID] = set()
    for issuer in seeds:
        storage.assets.upsert(create_sec_asset(issuer.configuration))
        storage.sources.upsert(create_sec_submissions_source(issuer.configuration))
        for document in issuer.documents:
            if document.prior_submissions.record_id not in saved_submissions:
                storage.raw_records.save(document.prior_submissions)
                saved_submissions.add(document.prior_submissions.record_id)
            if document.prior_revision.content_sha256:
                body = source_storage.documents.read(document.prior_revision.content_sha256)
                if len(body) != document.prior_revision.content_size_bytes:
                    raise ValueError("verified source blob has an unexpected size")
                receipt = storage.documents.put(body)
                if (
                    receipt.sha256 != document.prior_revision.content_sha256
                    or receipt.size_bytes != document.prior_revision.content_size_bytes
                ):
                    raise ValueError("scratch blob copy changed the source document")
            storage.raw_records.save(document.legacy_v1_raw_record)
            storage.raw_records.save(document.prior_raw_record)


def _raw_records_bounded(storage: LocalStorage) -> tuple[RawRecord, ...]:
    records: list[RawRecord] = []
    cursor_at: datetime | None = None
    cursor_id: UUID | None = None
    while True:
        page = storage.raw_records.list_import_page(
            limit=256,
            after_received_at=cursor_at,
            after_record_id=cursor_id,
        )
        if not page:
            break
        if len(page) > 256:
            raise ValueError("raw evidence page exceeds the authorized 256-record bound")
        resolved = storage.raw_records.get_many(page)
        if len(resolved) != len(page):
            raise ValueError("raw evidence page did not resolve exactly")
        records.extend(resolved[item] for item in page)
        final = resolved[page[-1]]
        cursor_at, cursor_id = final.received_at, final.record_id
    return tuple(records)


def _refresh_selected(
    storage: LocalStorage,
    issuer: _IssuerSeed,
    *,
    identity: SecEdgarIdentity,
    transport: _OfficialSecSmokeTransport,
    allow_archives: bool,
    retrieved_at: datetime,
) -> tuple[RawRecord, SecDocumentImportSummary]:
    configuration = issuer.configuration
    old_documents = tuple(item.prior_revision.document for item in issuer.documents)
    transport.set_scope(configuration, old_documents, allow_archives=allow_archives)
    submissions_client = SecEdgarClient(
        transport,
        identity,
        cik=configuration.cik,
        ticker=configuration.ticker,
        clock=lambda: retrieved_at,
    )
    snapshot = SecSubmissionsRefreshService(
        storage,
        configuration=configuration,
        issuer_client=submissions_client,
    ).persist_fresh_snapshot()
    document_client = SecDocumentClient(
        transport,
        identity,
        clock=lambda: retrieved_at + timedelta(seconds=1),
    )
    summary = SecDocumentPipeline(
        storage,
        document_client,
        configuration=configuration,
    ).run(SecDocumentImportRequest(accessions=tuple(item.accession for item in issuer.documents)))
    return snapshot.record, summary


def _counts_by_document_schema(storage: LocalStorage) -> dict[str, int]:
    return {
        schema: storage.raw_records.count(
            source_id=SEC_DOCUMENT_SOURCE_ID,
            schema_version=schema,
        )
        for schema in (
            SEC_DOCUMENT_SCHEMA_VERSION,
            SEC_DOCUMENT_SCHEMA_VERSION_V2,
            SEC_DOCUMENT_SCHEMA_VERSION_V3,
            SEC_DOCUMENT_SCHEMA_VERSION_V4,
        )
    }


def _verify_point_in_time(
    runtime: ApplicationRuntime,
    workspace: Path,
    seeds: tuple[_IssuerSeed, ...],
    corrected: Mapping[tuple[str, str], SecAssetDocumentRevision],
) -> tuple[dict[str, object], ...]:
    evidence: list[dict[str, object]] = []
    location = StorageLocationRequest(workspace=workspace)
    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_ONLY) as storage:
        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        timeline = SecDocumentTimelineService(storage)
        for issuer in seeds:
            configuration = issuer.configuration
            corpus = SecDocumentCorpusService(storage, configuration=configuration)
            for source in issuer.documents:
                revision = corrected[(configuration.asset_id, source.accession)]
                before_cut = revision.available_at - timedelta(microseconds=1)
                after_cut = revision.available_at
                before = repository.replay(
                    asset_id=configuration.asset_id,
                    known_at=before_cut,
                    accession=source.accession,
                )
                after = repository.replay(
                    asset_id=configuration.asset_id,
                    known_at=after_cut,
                    accession=source.accession,
                )
                if before.revision != source.prior_revision or after.revision != revision:
                    raise AssertionError("SEC replay selected a revision outside its PIT cut")
                reader_before = corpus.replay(
                    SecDocumentQuery(
                        asset_id=configuration.asset_id,
                        known_at=before_cut,
                        accession=source.accession,
                    )
                )
                reader_after = corpus.replay(
                    SecDocumentQuery(
                        asset_id=configuration.asset_id,
                        known_at=after_cut,
                        accession=source.accession,
                    )
                )
                if (
                    reader_before.revision != source.prior_revision
                    or reader_after.revision != revision
                ):
                    raise AssertionError("SEC corpus reader did not preserve the PIT correction")
                before_timeline = timeline.query(
                    SecDocumentTimelineQuery(
                        known_at=before_cut,
                        asset_ids=(configuration.asset_id,),
                        accession=source.accession,
                    )
                )
                after_timeline = timeline.query(
                    SecDocumentTimelineQuery(
                        known_at=after_cut,
                        asset_ids=(configuration.asset_id,),
                        accession=source.accession,
                    )
                )
                if tuple(item.revision_id for item in before_timeline.entries) != (
                    source.prior_revision.revision_id,
                ) or {item.revision_id for item in after_timeline.entries} != {
                    source.prior_revision.revision_id,
                    revision.revision_id,
                }:
                    raise AssertionError("SEC timeline changed its pre/post correction history")
                repository.verify_revision(revision)
                evidence.append(
                    {
                        "asset_id": configuration.asset_id,
                        "accession": source.accession,
                        "before_cut": before_cut.isoformat(),
                        "before_revision_id": str(before.revision.revision_id),
                        "after_cut": after_cut.isoformat(),
                        "after_revision_id": str(after.revision.revision_id),
                        "timeline_revision_count_after": after_timeline.matched_count,
                        "content_sha256_unchanged": (
                            before.revision.content_sha256 == after.revision.content_sha256
                        ),
                    }
                )
    return tuple(evidence)


def _fingerprint_raw_records(records: tuple[RawRecord, ...]) -> dict[str, str]:
    return {
        str(record.record_id): hashlib.sha256(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        for record in records
    }


def _make_v2_workspace(
    runtime: ApplicationRuntime,
    workspace_service: WorkspaceService,
    source_workspace: Path,
    destination_workspace: Path,
    seeds: tuple[_IssuerSeed, ...],
) -> None:
    workspace_service.initialize(destination_workspace, format_version=2)
    source_location = StorageLocationRequest(workspace=source_workspace)
    target_location = StorageLocationRequest(workspace=destination_workspace)
    with (
        runtime.open_storage(source_location, access_mode=WorkspaceAccessMode.READ_ONLY) as source,
        runtime.open_storage(
            target_location, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as destination,
    ):
        for issuer in seeds:
            destination.assets.upsert(create_sec_asset(issuer.configuration))
            destination.sources.upsert(create_sec_submissions_source(issuer.configuration))
        destination.sources.upsert(_document_source())
        records = _raw_records_bounded(source)
        if len(records) > 256:
            raise ValueError("scratch raw evidence copy exceeds one bounded page")
        for record in records:
            destination.raw_records.save(record)
        content_hashes = {
            revision.content_sha256
            for issuer in seeds
            for item in issuer.documents
            for revision in (item.prior_revision,)
        }
        for revision in source.raw_records.list(
            source_id=SEC_DOCUMENT_SOURCE_ID,
            schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V3,
        ):
            document_revision = revision_from_raw_record(revision)
            content_hashes.add(document_revision.content_sha256)
        for revision in source.raw_records.list(
            source_id=SEC_DOCUMENT_SOURCE_ID,
            schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V4,
        ):
            document_revision = revision_from_raw_record(revision)
            if not isinstance(document_revision, SecDocumentAcquisitionRevision):
                raise ValueError("v4 SEC record decoded to another revision family")
            content_hashes.add(document_revision.content_sha256)
            content_hashes.add(document_revision.prior_content_sha256)
        for checksum in sorted(content_hashes):
            destination.documents.put(source.documents.read(checksum))


def _verify_restored_workspace(
    runtime: ApplicationRuntime,
    workspace: Path,
    seeds: tuple[_IssuerSeed, ...],
    corrected: Mapping[tuple[str, str], SecAssetDocumentRevision],
) -> dict[str, object]:
    location = StorageLocationRequest(workspace=workspace)
    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_ONLY) as storage:
        counts = _counts_by_document_schema(storage)
        expected_v3 = sum(
            isinstance(revision, SecDocumentMetadataRevision) for revision in corrected.values()
        )
        expected_v4 = sum(
            isinstance(revision, SecDocumentAcquisitionRevision) for revision in corrected.values()
        )
        if counts != {
            SEC_DOCUMENT_SCHEMA_VERSION: 11,
            SEC_DOCUMENT_SCHEMA_VERSION_V2: 11,
            SEC_DOCUMENT_SCHEMA_VERSION_V3: expected_v3,
            SEC_DOCUMENT_SCHEMA_VERSION_V4: expected_v4,
        }:
            raise AssertionError("restored backup did not preserve all document schemas")
        repository = SecDocumentRepository(storage.raw_records, storage.documents)
        prior_ids = set()
        metadata_ids = set()
        legacy_raw_ids = set()
        preserved_submission_ids: set[UUID] = set()
        submission_counts: dict[str, int] = {}
        for issuer in seeds:
            config = issuer.configuration
            submission_count = storage.raw_records.count(
                asset_id=config.asset_id,
                source_id=config.submissions_source_id,
                schema_version=SUBMISSIONS_SCHEMA_VERSION,
            )
            if submission_count < 2:
                raise AssertionError("restored backup lost its prior/current Submissions records")
            submission_counts[config.asset_id] = submission_count
            for source in issuer.documents:
                prior_ids.add(source.prior_revision.revision_id)
                prior_raw = storage.raw_records.get(source.prior_raw_record.record_id)
                if prior_raw != source.prior_raw_record:
                    raise AssertionError("restored backup changed a prior v2 document RawRecord")
                prior_revision = repository.get_revision(source.prior_revision.revision_id)
                if prior_revision != source.prior_revision:
                    raise AssertionError("restored backup changed the prior v2 revision")
                repository.verify_revision(prior_revision)
                legacy_raw = storage.raw_records.get(source.legacy_v1_raw_record.record_id)
                if legacy_raw != source.legacy_v1_raw_record:
                    raise AssertionError("restored backup changed a legacy v1 document RawRecord")
                legacy_revision = revision_from_raw_record(legacy_raw)
                if not isinstance(legacy_revision, SecDocumentRevision):
                    raise AssertionError("restored legacy v1 revision decoded to another family")
                repository.verify_revision(legacy_revision)
                legacy_raw_ids.add(legacy_raw.record_id)
                prior_submissions = storage.raw_records.get(source.prior_submissions.record_id)
                if prior_submissions != source.prior_submissions:
                    raise AssertionError("restored backup changed the prior Submissions snapshot")
                preserved_submission_ids.add(prior_submissions.record_id)
                current = corrected[(config.asset_id, source.accession)]
                metadata_ids.add(current.revision_id)
                decoded = repository.get_revision(current.revision_id)
                if decoded != current:
                    raise AssertionError("restored metadata revision JSON changed")
                repository.verify_revision(decoded)
                if isinstance(decoded, SecDocumentAcquisitionRevision):
                    prior_body = storage.documents.read(decoded.prior_content_sha256)
                    current_body = storage.documents.read(decoded.content_sha256)
                    if len(prior_body) == 0 or len(current_body) != decoded.content_size_bytes:
                        raise AssertionError("restored backup lost a full v4 SEC response blob")
                    verify_sec_terminal_script_difference(
                        prior_body,
                        current_body,
                        document_name=decoded.document.name,
                        proof=decoded.terminal_script_difference,
                    )
                current_submissions = storage.raw_records.get(current.discovery_raw_record_id)
                if current_submissions.schema_version != SUBMISSIONS_SCHEMA_VERSION:
                    raise AssertionError("restored backup lost the current Submissions snapshot")
                prior_row, prior_name, prior_cik = _submission_row(
                    prior_submissions, source.accession
                )
                current_row, current_name, current_cik = _submission_row(
                    current_submissions, source.accession
                )
                if (
                    _metadata_without_acceptance(prior_row, prior_name, prior_cik)
                    != _metadata_without_acceptance(current_row, current_name, current_cik)
                    or prior_row["acceptanceDateTime"] == current_row["acceptanceDateTime"]
                ):
                    raise AssertionError("restored old/current Submissions lineage is inconsistent")
        if (
            len(prior_ids) != 11
            or len(metadata_ids) != 11
            or len(legacy_raw_ids) != 11
            or len(preserved_submission_ids) != 5
        ):
            raise AssertionError("restored revision identities are incomplete")
    return {
        "format_version": runtime.workspace_service.inspect(workspace).format_version,
        "document_record_counts": counts,
        "prior_revision_count": len(prior_ids),
        "legacy_revision_count": len(legacy_raw_ids),
        "metadata_revision_count": len(metadata_ids),
        "acquisition_revision_count": expected_v4,
        "full_v4_response_blobs_verified": 2 * expected_v4,
        "preserved_prior_submissions_count": len(preserved_submission_ids),
        "submissions_records_by_asset": submission_counts,
        "lineage_and_blobs_verified": True,
    }


def _restore_case(
    runtime: ApplicationRuntime,
    workspace_service: WorkspaceService,
    backup_source: Path,
    destination: Path,
    backup_path: Path,
    seeds: tuple[_IssuerSeed, ...],
    corrected: Mapping[tuple[str, str], SecAssetDocumentRevision],
    *,
    identity: SecEdgarIdentity,
    transport: _OfficialSecSmokeTransport,
    run_time: datetime,
) -> dict[str, object]:
    manifest = WorkspaceBackupService(workspace_service).create(backup_source, backup_path)
    inspection = WorkspaceBackupService(workspace_service).restore(backup_path, destination)
    if inspection.status != "ready":
        raise AssertionError("restored SEC scratch workspace is not ready")
    before = _verify_restored_workspace(runtime, destination, seeds, corrected)
    pit_before_repeat = _verify_point_in_time(runtime, destination, seeds, corrected)
    raw_count_before = inspection.raw_record_count
    for offset, issuer in enumerate(seeds):
        with runtime.open_storage(
            StorageLocationRequest(workspace=destination),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            _, summary = _refresh_selected(
                storage,
                issuer,
                identity=identity,
                transport=transport,
                allow_archives=False,
                retrieved_at=run_time + timedelta(minutes=offset),
            )
            if (
                summary.document_fetch_calls != 0
                or summary.revisions_created != 0
                or summary.revisions_reused != len(issuer.documents)
            ):
                raise AssertionError(
                    "restored SEC correction repeat fetched or duplicated a document"
                )
    after = _verify_restored_workspace(runtime, destination, seeds, corrected)
    pit_after_repeat = _verify_point_in_time(runtime, destination, seeds, corrected)
    if before["document_record_counts"] != after["document_record_counts"]:
        raise AssertionError("repeated correction changed restored document record counts")
    if pit_before_repeat != pit_after_repeat:
        raise AssertionError("repeated correction changed restored point-in-time replay")
    return {
        "backup_manifest_schema": manifest.schema_version,
        "backup_id": str(manifest.backup_id),
        "backup_raw_record_count": manifest.counts.raw_records,
        "restored_raw_record_count": inspection.raw_record_count,
        "restored": before,
        "repeat_refresh": {
            "submissions_requests": len(seeds),
            "archives_requests": 0,
            "document_revisions_created": 0,
            "equivalent_revision_ids_created": 0,
            "raw_record_count_before": raw_count_before,
            "raw_record_count_after": workspace_service.inspect(destination).raw_record_count,
        },
        "point_in_time_checks": len(pit_after_repeat),
        "queries_read_only": True,
        "writes_scratch_only": True,
    }


def run_smoke(source_workspace: Path) -> dict[str, object]:
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent.strip():
        raise SystemExit("SEC_USER_AGENT is required and was not provided")
    identity = SecEdgarIdentity(user_agent)
    with tempfile.TemporaryDirectory(prefix="data-chassis-41-sec-") as temporary:
        root = Path(temporary)
        workspace_service = WorkspaceService(environ={}, home=root / "home")
        runtime = ApplicationRuntime.create_default(workspace_service=workspace_service)
        seeds = _load_source_seeds(runtime, source_workspace.expanduser().resolve())
        original_metadata: dict[str, str] = {}
        corrected: dict[tuple[str, str], SecAssetDocumentRevision] = {}
        correction_rows: list[dict[str, object]] = []
        byte_proof_rows: list[dict[str, object]] = []
        observed_at = datetime.now(UTC)
        transport = _OfficialSecSmokeTransport(UrlLibHttpTransport())
        metadata_workspace = root / "metadata-format-1"
        workspace_service.initialize(metadata_workspace, format_version=1)
        with (
            runtime.open_storage(
                StorageLocationRequest(workspace=metadata_workspace),
                access_mode=WorkspaceAccessMode.READ_WRITE,
            ) as storage,
            runtime.open_storage(
                StorageLocationRequest(workspace=source_workspace.expanduser().resolve()),
                access_mode=WorkspaceAccessMode.READ_ONLY,
            ) as source_storage,
        ):
            _seed_original_documents(storage, source_storage, seeds)
            original_metadata = _fingerprint_raw_records(_raw_records_bounded(storage))
            _verify_pre_correction(storage, seeds)
            submissions_ids: dict[str, str] = {}
            initial_summaries: list[SecDocumentImportSummary] = []
            for offset, issuer in enumerate(seeds):
                record, summary = _refresh_selected(
                    storage,
                    issuer,
                    identity=identity,
                    transport=transport,
                    allow_archives=True,
                    retrieved_at=observed_at + timedelta(minutes=offset),
                )
                submissions_ids[issuer.configuration.asset_id] = str(record.record_id)
                initial_summaries.append(summary)
                if (
                    summary.revisions_created != len(issuer.documents)
                    or summary.revisions_reused != 0
                    or summary.document_fetch_calls != len(issuer.documents)
                ):
                    raise AssertionError("initial SEC correction did not verify every response")
                for revision in summary.revisions:
                    if not isinstance(
                        revision,
                        (SecDocumentMetadataRevision, SecDocumentAcquisitionRevision),
                    ):
                        raise AssertionError("SEC correction did not create a v3/v4 revision")
                    corrected[
                        (issuer.configuration.asset_id, revision.document.filing.accession)
                    ] = revision
            if len(corrected) != 11:
                raise AssertionError("SEC refresh did not produce all 11 metadata corrections")
            first_archive_start = sum(transport.archive_paths.values())
            initial_archives_gets = first_archive_start
            if first_archive_start != 11:
                raise AssertionError("initial correction did not make exactly 11 Archives requests")
            v3_count = sum(
                isinstance(revision, SecDocumentMetadataRevision) for revision in corrected.values()
            )
            v4_count = sum(
                isinstance(revision, SecDocumentAcquisitionRevision)
                for revision in corrected.values()
            )
            if v3_count != 0 or v4_count != 11:
                raise AssertionError(
                    "live SEC responses did not produce the declared 11 exact v4 revisions"
                )
            if sum(summary.blobs_created for summary in initial_summaries) != v4_count:
                raise AssertionError("new v4 responses were not persisted as full content blobs")
            for issuer in seeds:
                configuration = issuer.configuration
                fresh_submissions = storage.raw_records.get(
                    UUID(submissions_ids[configuration.asset_id])
                )
                fresh_index = SecFilingIndex.from_raw_record(fresh_submissions, configuration)
                for source in issuer.documents:
                    revision = corrected[(configuration.asset_id, source.accession)]
                    old_row, old_name, old_cik = _submission_row(
                        source.prior_submissions, source.accession
                    )
                    current_row, current_name, current_cik = _submission_row(
                        fresh_submissions, source.accession
                    )
                    current_metadata = fresh_index.get(source.accession)
                    if (
                        current_metadata is None
                        or current_metadata.acceptance_at != revision.document.filing.accepted_at
                        or current_row["acceptanceDateTime"] == old_row["acceptanceDateTime"]
                        or _metadata_without_acceptance(old_row, old_name, old_cik)
                        != _metadata_without_acceptance(current_row, current_name, current_cik)
                        or not same_document_metadata_except_accepted_at(
                            source.prior_revision.document, revision.document
                        )
                        or revision.prior_revision_id != source.prior_revision.revision_id
                    ):
                        raise AssertionError(
                            f"SEC correction changed metadata beyond accepted_at for "
                            f"{source.accession}"
                        )
                    if revision.metadata_observed_at != fresh_submissions.received_at:
                        raise AssertionError("revision lost Submissions observation lineage")
                    expected_available_at = max(
                        revision.document.filing.accepted_at,
                        revision.metadata_observed_at,
                        revision.retrieved_at,
                    )
                    if isinstance(revision, SecDocumentMetadataRevision):
                        if (
                            revision.content_sha256 != source.prior_revision.content_sha256
                            or revision.content_size_bytes
                            != source.prior_revision.content_size_bytes
                        ):
                            raise AssertionError("v3 changed its strict byte-identical contract")
                    elif isinstance(revision, SecDocumentAcquisitionRevision):
                        if revision.prior_content_sha256 != source.prior_revision.content_sha256:
                            raise AssertionError("v4 did not point to its prior full content blob")
                        expected_available_at = max(
                            expected_available_at,
                            source.prior_revision.available_at,
                        )
                        prior_body = storage.documents.read(source.prior_revision.content_sha256)
                        current_body = storage.documents.read(revision.content_sha256)
                        if (
                            hashlib.sha256(prior_body).hexdigest()
                            != source.prior_revision.content_sha256
                            or len(prior_body) != source.prior_revision.content_size_bytes
                            or hashlib.sha256(current_body).hexdigest() != revision.content_sha256
                            or len(current_body) != revision.content_size_bytes
                        ):
                            raise AssertionError("v4 full SEC response bytes fail their hashes")
                        verify_sec_terminal_script_difference(
                            prior_body,
                            current_body,
                            document_name=revision.document.name,
                            proof=revision.terminal_script_difference,
                        )
                        proof = revision.terminal_script_difference
                        byte_proof_rows.append(
                            {
                                "asset_id": configuration.asset_id,
                                "accession": source.accession,
                                "prior_sha256": source.prior_revision.content_sha256,
                                "prior_size_bytes": source.prior_revision.content_size_bytes,
                                "current_sha256": revision.content_sha256,
                                "current_size_bytes": revision.content_size_bytes,
                                "old_script": proof.old_script,
                                "new_script": proof.new_script,
                                "insertion_offset": proof.insertion_offset,
                                "core_sha256": proof.core_sha256,
                                "core_size_bytes": proof.core_size_bytes,
                                "core_bytes_equal": True,
                            }
                        )
                    if revision.available_at != expected_available_at:
                        raise AssertionError("revision availability is not point-in-time safe")
                    correction_rows.append(
                        {
                            "asset_id": configuration.asset_id,
                            "accession": source.accession,
                            "prior_revision_id": str(source.prior_revision.revision_id),
                            "corrected_revision_id": str(revision.revision_id),
                            "revision_schema_version": revision.revision_schema_version,
                            "prior_raw_record_id": str(source.prior_raw_record.record_id),
                            "corrected_raw_record_id": str(revision.raw_record_id),
                            "prior_content_sha256": source.prior_revision.content_sha256,
                            "prior_content_size_bytes": source.prior_revision.content_size_bytes,
                            "current_content_sha256": revision.content_sha256,
                            "current_content_size_bytes": revision.content_size_bytes,
                            "metadata_sha256": revision.metadata_sha256,
                            "prior_acceptanceDateTime_literal": old_row["acceptanceDateTime"],
                            "current_acceptanceDateTime_literal": current_row["acceptanceDateTime"],
                            "metadata_fields_equal_except_acceptance": True,
                            "availability": revision.available_at.isoformat(),
                        }
                    )
            original_records = storage.raw_records.get_many(
                tuple(UUID(record_id) for record_id in original_metadata)
            )
            if _fingerprint_raw_records(tuple(original_records.values())) != original_metadata:
                raise AssertionError("source v1/v2 raw evidence was rewritten in scratch")
            document_counts_after_first = _counts_by_document_schema(storage)
            if document_counts_after_first != {
                SEC_DOCUMENT_SCHEMA_VERSION: 11,
                SEC_DOCUMENT_SCHEMA_VERSION_V2: 11,
                SEC_DOCUMENT_SCHEMA_VERSION_V3: v3_count,
                SEC_DOCUMENT_SCHEMA_VERSION_V4: v4_count,
            }:
                raise AssertionError("scratch document inventory lost its v1-v4 revisions")
            raw_count_before_repeat = storage.raw_records.count()
            current_document_counts_before_repeat = _counts_by_document_schema(storage)
            repeat_archive_start = sum(transport.archive_paths.values())
            repeated_submissions: dict[str, str] = {}
            for offset, issuer in enumerate(seeds):
                record, summary = _refresh_selected(
                    storage,
                    issuer,
                    identity=identity,
                    transport=transport,
                    allow_archives=False,
                    retrieved_at=observed_at + timedelta(hours=1, minutes=offset),
                )
                repeated_submissions[issuer.configuration.asset_id] = str(record.record_id)
                if (
                    summary.document_fetch_calls != 0
                    or summary.revisions_created != 0
                    or summary.revisions_reused != len(issuer.documents)
                ):
                    raise AssertionError("second SEC refresh did not reuse the current revisions")
            if (
                sum(transport.archive_paths.values()) != repeat_archive_start
                or _counts_by_document_schema(storage) != current_document_counts_before_repeat
            ):
                raise AssertionError(
                    "second SEC refresh duplicated a revision or made Archives calls"
                )
            if raw_count_before_repeat > storage.raw_records.count():
                raise AssertionError("second SEC refresh removed append-only raw evidence")
        pit_evidence = _verify_point_in_time(runtime, metadata_workspace, seeds, corrected)
        pit_after_repeat = _verify_point_in_time(runtime, metadata_workspace, seeds, corrected)
        if pit_evidence != pit_after_repeat:
            raise AssertionError("second SEC refresh changed the historical PIT results")

        v2_workspace = root / "metadata-format-2"
        _make_v2_workspace(
            runtime,
            workspace_service,
            metadata_workspace,
            v2_workspace,
            seeds,
        )
        v1_restore = _restore_case(
            runtime,
            workspace_service,
            metadata_workspace,
            root / "restored-format-1",
            root / "backup-format-1",
            seeds,
            corrected,
            identity=identity,
            transport=transport,
            run_time=observed_at + timedelta(hours=2),
        )
        v2_restore = _restore_case(
            runtime,
            workspace_service,
            v2_workspace,
            root / "restored-format-2",
            root / "backup-format-2",
            seeds,
            corrected,
            identity=identity,
            transport=transport,
            run_time=observed_at + timedelta(hours=3),
        )
        if sum(transport.archive_paths.values()) != 11:
            raise AssertionError("restore repeats unexpectedly fetched SEC Archives")
        utc_date_crossings = sum(
            _utc_literal(row["current_acceptanceDateTime_literal"]).date()
            != _utc_literal(row["prior_acceptanceDateTime_literal"]).date()
            for row in correction_rows
        )
        if utc_date_crossings < 1:
            raise AssertionError("SEC accepted_at corrections did not prove a UTC date crossing")
        return {
            "schema_version": "sec-document-response-revision-smoke-v2",
            "status": "pass",
            "captured_at": datetime.now(UTC).isoformat(),
            "code_sha": _git("rev-parse", "HEAD"),
            "working_tree_clean": not bool(_git("status", "--short")),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.system(),
                "workspace_source_access": "read-only selected SEC records and blobs",
                "scratch_only_writes": True,
                "metrics_or_database_copy": False,
                "user_agent_value_emitted": False,
            },
            "deliverables": {
                "sec_response_five_issuers": {
                    "issuer_count": len(seeds),
                    "document_correction_count": len(corrected),
                    "submissions_gets_initial": len(seeds),
                    "archives_gets_initial": initial_archives_gets,
                    "submissions_gets_repeat": len(seeds),
                    "archives_gets_repeat": 0,
                    "v3_revisions": v3_count,
                    "v4_revisions": v4_count,
                    "current_revisions_reused_on_repeat": len(corrected),
                    "utc_date_crossings": utc_date_crossings,
                    "prior_and_current_submissions_ids": {
                        issuer.configuration.asset_id: {
                            "prior": str(issuer.documents[0].prior_submissions.record_id),
                            "current": submissions_ids[issuer.configuration.asset_id],
                            "repeat": repeated_submissions[issuer.configuration.asset_id],
                        }
                        for issuer in seeds
                    },
                    "document_schema_counts": document_counts_after_first,
                    "source_original_raw_sha256_unchanged": True,
                    "point_in_time_checks": len(pit_evidence),
                    "corrections": correction_rows,
                    "pit_evidence": pit_evidence,
                },
                "sec_response_restore": {
                    "format_1": v1_restore,
                    "format_2": v2_restore,
                    "archive_gets_during_restore_repeats": 0,
                    "restore_destinations_were_empty": True,
                    "point_in_time_checks_after_repeat": len(pit_evidence),
                },
                "sec_response_byte_proof": {
                    "proof_count": len(byte_proof_rows),
                    "v4_full_response_blobs_per_revision": 2,
                    "all_core_bytes_equal": all(
                        row["core_bytes_equal"] is True for row in byte_proof_rows
                    ),
                    "proofs": byte_proof_rows,
                },
            },
        }


def _verify_pre_correction(storage: LocalStorage, seeds: tuple[_IssuerSeed, ...]) -> None:
    repository = SecDocumentRepository(storage.raw_records, storage.documents)
    for issuer in seeds:
        for item in issuer.documents:
            replay = repository.replay(
                asset_id=issuer.configuration.asset_id,
                known_at=item.prior_revision.available_at,
                accession=item.accession,
            )
            if replay.revision != item.prior_revision:
                raise AssertionError("scratch pre-correction replay differs from the prior v2")


def _git(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    result = run_smoke(arguments.source_workspace)
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
