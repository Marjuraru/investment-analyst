import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemanticsQuery,
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
from investment_analyst.workspace import backup as backup_module

_COVER = (
    b"<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>"
    b"<name>Manager LLC</name></filingManager>"
    b"<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>"
    b"<tableEntryTotal>2</tableEntryTotal><tableValueTotal>100</tableValueTotal>"
    b"</edgarSubmission>"
)
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>037833100</cusip><value>50</value><shrsOrPrnAmt><sshPrnamt>20</sshPrnamt>
<sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable></informationTable>"""


class _Submissions:
    def fetch(self, filer_cik: str) -> RawRecord:
        captured = datetime(2025, 2, 15, tzinfo=UTC)
        return RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{filer_cik}:submissions", retrieved_at=captured
            ),
            event_time=captured,
            available_at=captured,
            received_at=captured,
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
                            "primaryDocument": ["xslForm13F_X02/primary_doc.xml"],
                        }
                    },
                }
            },
            schema_version="sec-manager-submissions-snapshot-v1",
        )


class _Documents:
    _retrieved_at = datetime(2025, 2, 15, tzinfo=UTC)

    def fetch_manifest(self, document) -> SecAccessionManifest:
        del document
        return SecAccessionManifest(
            entries=("primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self._retrieved_at,
        )

    def fetch(self, document) -> SecPrimaryDocumentResponse:
        content = (
            b"<!DOCTYPE html><html><body>locator</body></html>"
            if document.name == "xslForm13F_X02/primary_doc.xml"
            else _COVER
            if document.name == "primary_doc.xml"
            else _TABLE
        )
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self._retrieved_at,
        )


def test_enrichment_reuses_complete_bundle_and_query_never_aggregates_rows(tmp_path: Path) -> None:
    root = StoragePaths.from_root(tmp_path)
    with LocalStorage(root) as storage:
        report = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _Submissions(), _Documents()
        ).run(
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                filer_cik="1067983", forms=("13F-HR",)
            )
        )[0]
        service = InstitutionalHoldingsSemanticsService(
            storage, clock=lambda: datetime(2025, 2, 16, tzinfo=UTC)
        )
        request = InstitutionalSemanticsEnrichRequest(
            manager_cik="1067983",
            report_ids=(report.report_id,),
            known_at=datetime(2025, 2, 16, tzinfo=UTC),
        )
        assert service.enrich(request).created == 1
        assert service.enrich(request).reused == 1
        assert backup_module._scan_raw_records(storage) == storage.raw_records.count()

    with LocalStorage(root, read_only=True) as storage:
        result = InstitutionalHoldingsSemanticsService(storage).query(
            InstitutionalHoldingsSemanticsQuery(
                manager_cik="1067983",
                report_ids=(report.report_id,),
                known_at=datetime(2025, 2, 16, tzinfo=UTC),
                cusip="037833100",
                limit=1,
            )
        )

    queried = result.reports[0]
    assert queried.state == "found"
    assert queried.total_rows == 2
    assert queried.matching_rows == 2
    assert queried.truncated is True
    assert len(queried.rows) == 1

    with LocalStorage(root, read_only=True) as storage:
        final_page = (
            InstitutionalHoldingsSemanticsService(storage)
            .query(
                InstitutionalHoldingsSemanticsQuery(
                    manager_cik="1067983",
                    report_ids=(report.report_id,),
                    known_at=datetime(2025, 2, 16, tzinfo=UTC),
                    cusip="037833100",
                    offset=1,
                    limit=1,
                )
            )
            .reports[0]
        )

    assert final_page.truncated is False
    assert tuple(row.row_number for row in final_page.rows) == (2,)
