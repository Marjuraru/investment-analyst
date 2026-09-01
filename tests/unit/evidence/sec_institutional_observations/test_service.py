import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
    InstitutionalObservationRequest,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationLineageError,
    InstitutionalObservationService,
)
from investment_analyst.evidence.sec_institutional_semantics.service import (
    InstitutionalHoldingsSemanticsService,
    InstitutionalSemanticsEnrichRequest,
)
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_NOW = datetime(2025, 2, 16, tzinfo=UTC)
_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
</edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>037833100</cusip><value>60</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt>
<sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable></informationTable>"""


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


def _seed(storage: LocalStorage, *, enrich: bool = True, title_of_class: str = "COM"):
    report = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
        storage, _Submissions(), _Documents()
    ).run(
        sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
            filer_cik="1067983", forms=("13F-HR",)
        )
    )[0]
    correspondence = InstrumentCorrespondence.declare(
        asset_id="equity:us:aapl",
        cusip="037833100",
        title_of_class=title_of_class,
        effective_from=report.report_period - timedelta(days=365),
        effective_to=None,
        available_at=_NOW,
        recorded_at=_NOW,
    )
    InstrumentCorrespondenceRepository(storage.raw_records).save(
        correspondence, catalog_version=1, declared_by="test"
    )
    if enrich:
        InstitutionalHoldingsSemanticsService(storage, clock=lambda: _NOW).enrich(
            InstitutionalSemanticsEnrichRequest(
                manager_cik="1067983", report_ids=(report.report_id,), known_at=_NOW
            )
        )
    return report, correspondence


def test_normalization_counts_and_idempotence(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        report, _ = _seed(storage)
        first = InstitutionalObservationService(
            storage, clock=lambda: _NOW + timedelta(hours=1)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=_NOW,
            )
        )
        second = InstitutionalObservationService(
            storage, clock=lambda: _NOW + timedelta(days=1)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=_NOW,
            )
        )

    assert first.reports_examined == 1
    assert first.reports_missing == first.reports_not_enriched == 0
    assert first.rows_examined == first.rows_linked == first.values_examined == 2
    assert first.rows_unlinked == 0
    assert first.observations_generated == first.observations_created == 4
    assert second.observations_created == 0
    assert second.observations_reused == 4


def test_normalization_class_mismatch_is_unlinked(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        report, _ = _seed(storage, title_of_class="PREF")
        summary = InstitutionalObservationService(
            storage, clock=lambda: _NOW + timedelta(hours=1)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=_NOW,
            )
        )

    assert summary.rows_examined == summary.rows_unlinked == 2
    assert summary.rows_linked == summary.values_examined == 0
    assert summary.observations_generated == 0
    assert summary.skipped_by_reason == {"class_mismatch": 2}


def test_summary_distinguishes_missing_and_not_enriched_reports(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        report, _ = _seed(storage, enrich=False)
        summary = InstitutionalObservationService(
            storage, clock=lambda: _NOW + timedelta(hours=1)
        ).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id, uuid4()),
                known_at=_NOW,
            )
        )

    assert summary.reports_examined == 2
    assert summary.reports_missing == summary.reports_not_enriched == 1
    assert summary.rows_examined == summary.values_examined == 0
    assert summary.skipped_by_reason == {"missing_report": 1, "not_enriched": 1}


def test_read_only_query_returns_verified_views_and_rejects_corruption(tmp_path: Path) -> None:
    root = StoragePaths.from_root(tmp_path)
    with LocalStorage(root) as storage:
        report, _ = _seed(storage)
        InstitutionalObservationService(storage, clock=lambda: _NOW + timedelta(hours=1)).normalize(
            InstitutionalObservationRequest(
                asset_id="equity:us:aapl",
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=_NOW,
            )
        )

    query = InstitutionalObservationQuery(
        asset_id="equity:us:aapl",
        known_at=_NOW + timedelta(days=1),
        manager_cik="1067983",
        report_id=report.report_id,
        cusip="037833100",
        limit=1,
    )
    with LocalStorage(root, read_only=True) as storage:
        service = InstitutionalObservationService(storage)
        result = service.query(query)
        all_fields = {
            view.observation.field_name
            for view in service.query(
                query.model_copy(update={"limit": 10, "field_name": None})
            ).observations
        }

    assert result.total_matching == 4
    assert result.truncated is True
    assert result.observations[0].report.report_id == report.report_id
    assert result.observations[0].row.cusip == "037833100"
    assert result.observations[0].correspondence.asset_id == "equity:us:aapl"
    assert all_fields == {
        "institutional_reported_fair_value",
        "institutional_reported_principal_amount",
        "institutional_reported_shares",
    }

    with LocalStorage(root) as storage:
        corrupted = storage.observations.list(asset_id="equity:us:aapl")[0].model_copy(
            update={"observation_id": uuid4()}
        )
        storage.observations.save(corrupted)
    with (
        LocalStorage(root, read_only=True) as storage,
        pytest.raises(InstitutionalObservationLineageError, match="content conflicts"),
    ):
        InstitutionalObservationService(storage).query(query)
