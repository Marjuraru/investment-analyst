"""Integration coverage for backup verification of row-scoped correspondence lineage.

A backup must rebuild ``snapshot → candidate → report → artifact → row`` from the persisted
repositories. Corruption, a missing parent, an incompatible hash or a diverging row invalidates the
backup instead of being silently accepted.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
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
from investment_analyst.workspace.backup import (
    WorkspaceBackupError,
    WorkspaceBackupService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_NOW = datetime(2026, 8, 1, tzinfo=UTC)
_SHA = "a" * 64
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


def _services(tmp_path: Path) -> tuple[WorkspaceService, WorkspaceBackupService, Path]:
    workspace_service = WorkspaceService(environ={}, home=tmp_path / "home", clock=lambda: _NOW)
    workspace = tmp_path / "source"
    workspace_service.initialize(workspace)
    return (
        workspace_service,
        WorkspaceBackupService(workspace_service, clock=lambda: _NOW.replace(hour=1)),
        workspace,
    )


def _seed_claim(
    workspace_service: WorkspaceService, source: Path
) -> SecInstitutionalRowCorrespondence:
    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
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
        artifact = InstitutionalSemanticsRepository(storage.raw_records).get_for_parent(report)
        row = artifact.rows[0]
        revision = Sec13FDataSetRevision.create(
            dataset_url=(
                "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
                "01dec2024-28feb2025_form13f.zip"
            ),
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
                cusip=row.cusip,
                manager_cik=report.manager_cik,
                accession=report.cover_revision.document.filing.accession,
                form=report.cover_revision.document.filing.form,
                report_period=report.report_period,
            ),
            dataset_revision_id=revision.revision_id,
            asset_id="equity:us:aapl",
            cusip=row.cusip,
            manager_cik=report.manager_cik,
            manager_name="BERKSHIRE HATHAWAY INC",
            accession=report.cover_revision.document.filing.accession,
            form=report.cover_revision.document.filing.form,
            filing_date=report.cover_revision.document.filing.filing_date,
            report_period=report.report_period,
            value_as_filed=report.parsed_value_total,
            is_selected=True,
            selection_rank=1,
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
            selected_manager_count=1,
            unselected_manager_count=0,
            coverage_complete=True,
            covered_cusips=(row.cusip,),
            candidates=(candidate,),
        )
        universe = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
        universe.save_dataset_revision(revision)
        universe.save_snapshot(snapshot)
        claim = SecInstitutionalRowCorrespondence.claim(
            asset_id=candidate.asset_id,
            cusip=row.cusip,
            title_of_class=row.title_of_class,
            report_period=report.report_period,
            manager_cik=report.manager_cik,
            report_id=report.report_id,
            artifact_id=artifact.artifact_id,
            row_id=row.row_id,
            universe_snapshot_id=snapshot.snapshot_id,
            dataset_revision_id=revision.revision_id,
            candidate_id=candidate.candidate_id,
            available_at=max(snapshot.available_at, artifact.available_at),
            recorded_at=_NOW,
        )
        SecInstitutionalRowCorrespondenceRepository(storage.raw_records).save(claim)
        return claim


def test_backup_and_restore_verify_the_complete_row_correspondence_lineage(
    tmp_path: Path,
) -> None:
    workspace_service, backup_service, source = _services(tmp_path)
    claim = _seed_claim(workspace_service, source)

    manifest = backup_service.create(source, tmp_path / "backup")

    assert manifest.counts.raw_records >= 1
    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        assert storage.raw_records.count(schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION) == 1
        assert (
            SecInstitutionalRowCorrespondenceRepository(storage.raw_records).get(
                claim.correspondence_id
            )
            == claim
        )

    restored = backup_service.restore(tmp_path / "backup", tmp_path / "restored")
    assert restored.status == "ready"
    with workspace_service.open_storage(
        workspace_service.resolve(tmp_path / "restored"), WorkspaceAccessMode.READ_ONLY
    ) as storage:
        assert (
            SecInstitutionalRowCorrespondenceRepository(storage.raw_records).get(
                claim.correspondence_id
            )
            == claim
        )


def _append_claim(
    workspace_service: WorkspaceService, source: Path, claim: SecInstitutionalRowCorrespondence
) -> None:
    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        SecInstitutionalRowCorrespondenceRepository(storage.raw_records).save(claim)


def test_backup_rejects_a_claim_without_its_persisted_parents(tmp_path: Path) -> None:
    workspace_service, backup_service, source = _services(tmp_path)
    claim = _seed_claim(workspace_service, source)
    orphan = SecInstitutionalRowCorrespondence.claim(
        asset_id=claim.asset_id,
        cusip=claim.cusip,
        title_of_class=claim.title_of_class,
        report_period=claim.report_period,
        manager_cik=claim.manager_cik,
        report_id=uuid4(),
        artifact_id=uuid4(),
        row_id=uuid4(),
        universe_snapshot_id=uuid4(),
        dataset_revision_id=uuid4(),
        candidate_id=uuid4(),
        available_at=claim.available_at,
        recorded_at=claim.recorded_at,
    )
    _append_claim(workspace_service, source, orphan)

    with pytest.raises(WorkspaceBackupError, match="traceability") as error:
        backup_service.create(source, tmp_path / "backup")
    assert "row correspondence" in str(error.value.__cause__)
    assert not (tmp_path / "backup").exists()


def test_backup_rejects_availability_that_is_not_the_exact_parent_maximum(
    tmp_path: Path,
) -> None:
    workspace_service, backup_service, source = _services(tmp_path)
    original = _seed_claim(workspace_service, source)
    diverging = SecInstitutionalRowCorrespondence.claim(
        asset_id=original.asset_id,
        cusip=original.cusip,
        title_of_class=original.title_of_class,
        report_period=original.report_period,
        manager_cik=original.manager_cik,
        report_id=original.report_id,
        artifact_id=original.artifact_id,
        row_id=original.row_id,
        universe_snapshot_id=original.universe_snapshot_id,
        dataset_revision_id=original.dataset_revision_id,
        candidate_id=original.candidate_id,
        available_at=original.available_at - timedelta(hours=1),
        recorded_at=original.recorded_at,
    )
    assert diverging.correspondence_id != original.correspondence_id
    _append_claim(workspace_service, source, diverging)

    with pytest.raises(WorkspaceBackupError, match="traceability") as error:
        backup_service.create(source, tmp_path / "backup")
    assert "availability" in str(error.value.__cause__)
    assert not (tmp_path / "backup").exists()


def test_backup_rejects_a_corrupted_row_correspondence_record(tmp_path: Path) -> None:
    workspace_service, backup_service, source = _services(tmp_path)
    _seed_claim(workspace_service, source)
    with workspace_service.open_storage(
        workspace_service.resolve(source), WorkspaceAccessMode.READ_WRITE
    ) as storage:
        records = storage.raw_records.list(schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION)
        assert len(records) == 1
        storage.raw_records.save(records[0].model_copy(update={"record_id": uuid4()}))

    with pytest.raises(WorkspaceBackupError, match="traceability") as error:
        backup_service.create(source, tmp_path / "backup")
    assert "row correspondence" in str(error.value.__cause__)
    assert not (tmp_path / "backup").exists()
