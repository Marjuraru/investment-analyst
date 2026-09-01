from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from investment_analyst.application.cazatiburones_institutional_observations import (
    CazatiburonesInstitutionalObservationsApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.instrument_correspondence.repository import (
    InstrumentCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
    InstitutionalObservationRequest,
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
</infoTable></informationTable>"""


class _Submissions:
    def fetch(self, filer_cik):
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
        import hashlib

        content = _COVER if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self.retrieved_at,
        )


def test_application_persists_and_queries_a_verified_pit_view(tmp_path: Path) -> None:
    root = StoragePaths.from_root(tmp_path)
    with LocalStorage(root) as storage:
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
        correspondence = InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip="037833100",
            title_of_class="COM",
            effective_from=report.report_period - timedelta(days=365),
            effective_to=None,
            available_at=_NOW,
            recorded_at=_NOW,
        )
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            correspondence, catalog_version=1, declared_by="test"
        )

    location = StorageLocationRequest(legacy_root=tmp_path)
    application = CazatiburonesInstitutionalObservationsApplication.create_default()
    summary = application.normalize(
        InstitutionalObservationRequest(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            report_ids=(report.report_id,),
            known_at=_NOW,
        ),
        location=location,
    )
    result = application.query(
        InstitutionalObservationQuery(
            asset_id="equity:us:aapl",
            known_at=_NOW + timedelta(days=1),
            field_name="institutional_reported_fair_value",
        ),
        location=location,
    )

    assert summary.observations_created == 2
    assert result.total_matching == 1
    assert result.observations[0].artifact.parent_report_id == report.report_id
