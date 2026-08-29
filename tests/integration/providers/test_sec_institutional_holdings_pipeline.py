import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import FILER_REVISION_SCHEMA_VERSION
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
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

_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>
<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"""


class _SubmissionsClient:
    def fetch(self, filer_cik):
        captured = datetime(2025, 2, 15, tzinfo=UTC)
        return RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{filer_cik}:submissions",
                retrieved_at=captured,
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


class _DocumentClient:
    def __init__(self, *, xml_count: int = 2) -> None:
        self._xml_count = xml_count
        self._retrieved_at = datetime(2025, 2, 15, tzinfo=UTC)

    def fetch_manifest(self, document):
        del document
        entries = {
            0: ("filing.htm",),
            1: ("primary_doc.xml",),
            2: ("primary_doc.xml", "infotable.xml"),
            3: ("primary_doc.xml", "infotable.xml", "extra.xml"),
        }[self._xml_count]
        return SecAccessionManifest(
            entries=entries,
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self._retrieved_at,
        )

    def fetch(self, document):
        if document.name == "xslForm13F_X02/primary_doc.xml":
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        else:
            content = _COVER if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self._retrieved_at,
        )


def test_pipeline_persists_two_revisions_report_and_positions_idempotently(
    tmp_path: Path,
) -> None:
    request = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
        filer_cik="1067983", forms=("13F-HR",)
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _SubmissionsClient(), _DocumentClient()
        )
        first = pipeline.run(request)
        second = pipeline.run(request)

        assert [report.report_id for report in first] == [report.report_id for report in second]
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION)
            == 3
        )
        assert storage.raw_records.count(schema_version=FILER_REVISION_SCHEMA_VERSION) == 2
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 1
        )
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION)
            == 1
        )
        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 0
        assert first[0].available_at == datetime(2025, 2, 14, 18, tzinfo=UTC)
        assert first[0].value_total_matches is True
        assert backup_module._scan_raw_records(storage) == storage.raw_records.count()


def test_pipeline_records_rejection_for_zero_one_or_three_xml(
    tmp_path: Path,
) -> None:
    for xml_count in (0, 1, 3):
        root = tmp_path / str(xml_count)
        with LocalStorage(StoragePaths.from_root(root)) as storage:
            result = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
                storage, _SubmissionsClient(), _DocumentClient(xml_count=xml_count)
            ).run(
                sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                    filer_cik="1067983", forms=("13F-HR",)
                )
            )

            assert result == ()
            assert (
                storage.raw_records.count(
                    schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION
                )
                == 1
            )
            assert storage.raw_records.count(schema_version=FILER_REVISION_SCHEMA_VERSION) == 0
