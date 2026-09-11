"""Unit tests for row-correspondence lineage verification and deterministic resolution."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_correspondence.service import (
    SecInstitutionalRowCorrespondenceError,
    SecInstitutionalRowCorrespondenceService,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalHoldingsSemanticsService,
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.evidence.sec_institutional_universe.identity import candidate_id
from investment_analyst.evidence.sec_institutional_universe.models import (
    Sec13FDataSetRevision,
    Sec13FManagerCandidate,
    Sec13FManagerUniverseSnapshot,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_NOW = datetime(2025, 2, 16, 12, 0, tzinfo=UTC)
_SHA = "a" * 64
_CUSIP = "037833100"
_ARTIFACT_AVAILABLE = datetime(2025, 2, 14, 18, tzinfo=UTC)
_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
</edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable></informationTable>"""


class _Submissions:
    def fetch(self, filer_cik):
        from investment_analyst.core.models import RawRecord, SourceReference

        accepted = datetime(2025, 2, 14, 18, tzinfo=UTC)
        return RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{filer_cik}:submissions", retrieved_at=accepted
            ),
            event_time=accepted,
            available_at=accepted,
            received_at=accepted,
            payload={
                "document": {
                    "cik": filer_cik,
                    "name": "Manager LLC",
                    "filings": {
                        "recent": {
                            "accessionNumber": ["0000950123-25-000001"],
                            "filingDate": ["2025-02-14"],
                            "reportDate": ["2024-12-31"],
                            "acceptanceDateTime": ["2025-02-14T18:00:00Z"],
                            "form": ["13F-HR"],
                            "primaryDocument": ["primary_doc.xml"],
                        }
                    },
                }
            },
            schema_version="sec-manager-submissions-snapshot-v1",
        )


class _Documents:
    retrieved_at = datetime(2025, 2, 15, tzinfo=UTC)

    def fetch_manifest(self, document):
        del document
        return SecAccessionManifest(
            entries=("primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self.retrieved_at,
        )

    def fetch(self, document):
        content = _COVER if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self.retrieved_at,
        )


def _build_chain(storage, *, selected: bool = True):
    """Build the complete persisted evidence chain used by a row-scoped claim."""
    report = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
        storage, _Submissions(), _Documents()
    ).run(
        sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik="1067983", forms=("13F-HR",)
        )
    )[0]
    InstitutionalHoldingsSemanticsService(storage, clock=lambda: _NOW).enrich(
        InstitutionalSemanticsEnrichRequest(
            manager_cik="1067983", report_ids=(report.report_id,), known_at=_NOW
        )
    )
    from investment_analyst.evidence.sec_institutional_semantics.repository import (
        InstitutionalSemanticsRepository,
    )

    artifact = InstitutionalSemanticsRepository(storage.raw_records).get_for_parent(report)
    revision = Sec13FDataSetRevision.create(
        dataset_url="https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
        "01dec2024-28feb2025_form13f.zip",
        period_start=date(2024, 12, 1),
        period_end=date(2025, 2, 28),
        content_sha256=_SHA,
        size_bytes=10,
        retrieved_at=_NOW,
    )
    candidate = Sec13FManagerCandidate(
        candidate_id=candidate_id(
            dataset_sha256=_SHA,
            asset_id="equity:us:aapl",
            cusip=_CUSIP,
            manager_cik=report.manager_cik,
            accession=report.cover_revision.document.filing.accession,
            form=report.cover_revision.document.filing.form,
            report_period=report.report_period,
        ),
        dataset_revision_id=revision.revision_id,
        asset_id="equity:us:aapl",
        cusip=_CUSIP,
        manager_cik=report.manager_cik,
        manager_name="BERKSHIRE HATHAWAY INC",
        accession=report.cover_revision.document.filing.accession,
        form=report.cover_revision.document.filing.form,
        filing_date=report.cover_revision.document.filing.filing_date,
        report_period=report.report_period,
        value_as_filed=report.parsed_value_total,
        is_selected=selected,
        selection_rank=1 if selected else None,
    )
    snapshot = Sec13FManagerUniverseSnapshot.create(
        dataset_revision_id=revision.revision_id,
        dataset_sha256=_SHA,
        catalog_version=1,
        period_start=revision.period_start,
        period_end=revision.period_end,
        retrieved_at=_NOW,
        event_time=datetime(2025, 2, 28, tzinfo=UTC),
        eligible_asset_count=1,
        matched_asset_count=1,
        candidate_manager_count=1,
        selected_manager_count=1 if selected else 0,
        unselected_manager_count=0 if selected else 1,
        coverage_complete=True,
        covered_cusips=(_CUSIP,),
        candidates=(candidate,),
    )
    repository = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
    repository.save_dataset_revision(revision)
    repository.save_snapshot(snapshot)
    return report, artifact, revision, snapshot, candidate


def _claim(chain, row, **overrides):
    report, artifact, revision, snapshot, candidate = chain
    values: dict[str, object] = {
        "asset_id": candidate.asset_id,
        "cusip": row.cusip,
        "title_of_class": row.title_of_class,
        "report_period": report.report_period,
        "manager_cik": report.manager_cik,
        "report_id": report.report_id,
        "artifact_id": artifact.artifact_id,
        "row_id": row.row_id,
        "universe_snapshot_id": snapshot.snapshot_id,
        "dataset_revision_id": revision.revision_id,
        "candidate_id": candidate.candidate_id,
        "available_at": max(snapshot.available_at, artifact.available_at),
        "recorded_at": _NOW,
    }
    values.update(overrides)
    return SecInstitutionalRowCorrespondence.claim(**values)


def test_verify_lineage_accepts_a_complete_chain(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        chain = _build_chain(storage)
        claim = _claim(chain, chain[1].rows[0])
        service = SecInstitutionalRowCorrespondenceService(storage)
        service.verify_lineage(claim)
        service.verify_catalog_binding(claim, catalog_cusips={_CUSIP: "equity:us:aapl"})
        assert claim.available_at == _NOW
        assert claim.available_at >= _ARTIFACT_AVAILABLE
        assert claim.report_period == date(2024, 12, 31)
        assert claim.effective_to == date(2025, 1, 1)


def test_verify_lineage_rejects_every_incomplete_or_conflicting_parent(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        chain = _build_chain(storage)
        report, artifact, revision, snapshot, candidate = chain
        row = artifact.rows[0]
        service = SecInstitutionalRowCorrespondenceService(storage)
        maximum = max(snapshot.available_at, artifact.available_at)
        cases = (
            {"universe_snapshot_id": uuid4()},
            {"dataset_revision_id": uuid4()},
            {"candidate_id": uuid4()},
            {"report_id": uuid4()},
            {"artifact_id": uuid4()},
            {"row_id": uuid4()},
            {"report_period": date(2025, 3, 31)},
            {"cusip": "594918104"},
            {"title_of_class": "COM CL A"},
            {
                "available_at": maximum + timedelta(seconds=1),
                "recorded_at": _NOW + timedelta(days=1),
            },
            {"available_at": artifact.available_at - timedelta(days=1)},
        )
        for overrides in cases:
            claim = _claim(chain, row, **overrides)
            with pytest.raises(SecInstitutionalRowCorrespondenceError):
                service.verify_lineage(claim)
        assert report.manager_cik == candidate.manager_cik
        assert revision.revision_id == snapshot.dataset_revision_id


def test_verify_lineage_rejects_an_unselected_candidate(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        chain = _build_chain(storage, selected=False)
        claim = _claim(chain, chain[1].rows[0])
        with pytest.raises(SecInstitutionalRowCorrespondenceError):
            SecInstitutionalRowCorrespondenceService(storage).verify_lineage(claim)


def test_verify_catalog_binding_requires_the_exact_live_binding(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        chain = _build_chain(storage)
        claim = _claim(chain, chain[1].rows[0])
        service = SecInstitutionalRowCorrespondenceService(storage)
        for catalog in ({}, {_CUSIP: "equity:us:msft"}, {"594918104": "equity:us:aapl"}):
            with pytest.raises(SecInstitutionalRowCorrespondenceError):
                service.verify_catalog_binding(claim, catalog_cusips=catalog)


def test_resolution_is_deterministic_and_keeps_conflicts_ambiguous(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        chain = _build_chain(storage)
        row = chain[1].rows[0]
        service = SecInstitutionalRowCorrespondenceService(storage)
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)

        assert (
            service.resolve(
                artifact_id=chain[1].artifact_id, row_id=row.row_id, known_at=_NOW
            ).state
            == "absent"
        )

        later = _claim(chain, row, available_at=_NOW)
        earlier = SecInstitutionalRowCorrespondence.claim(
            asset_id=chain[4].asset_id,
            cusip=row.cusip,
            title_of_class=row.title_of_class,
            report_period=chain[0].report_period,
            manager_cik=chain[0].manager_cik,
            report_id=chain[0].report_id,
            artifact_id=chain[1].artifact_id,
            row_id=row.row_id,
            universe_snapshot_id=chain[3].snapshot_id,
            dataset_revision_id=chain[2].revision_id,
            candidate_id=chain[4].candidate_id,
            available_at=chain[1].available_at,
            recorded_at=_NOW,
        )
        repository.save(later)
        repository.save(earlier)
        resolved = service.resolve(
            artifact_id=chain[1].artifact_id, row_id=row.row_id, known_at=_NOW
        )
        assert resolved.state == "resolved"
        assert resolved.correspondence == earlier
        assert resolved.considered == 2

        repository.save(
            _claim(chain, row, asset_id="equity:us:msft", title_of_class=row.title_of_class)
        )
        assert (
            service.resolve(
                artifact_id=chain[1].artifact_id, row_id=row.row_id, known_at=_NOW
            ).state
            == "ambiguous_asset"
        )


def test_resolution_reports_conflicting_content_for_one_asset(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        chain = _build_chain(storage)
        row = chain[1].rows[0]
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        repository.save(_claim(chain, row))
        repository.save(_claim(chain, row, title_of_class="COM CL A"))
        resolution = SecInstitutionalRowCorrespondenceService(storage).resolve(
            artifact_id=chain[1].artifact_id, row_id=row.row_id, known_at=_NOW
        )
        assert resolution.state == "conflicting_content"
        assert resolution.correspondence is None
        assert resolution.considered == 2
