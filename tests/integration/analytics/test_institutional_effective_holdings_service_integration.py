"""Integration tests for institutional effective holdings application and CLI."""

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.application.cazatiburones_institutional_effective_holdings import (
    CazatiburonesInstitutionalEffectiveHoldingsApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.core.models import RawRecord, SourceReference
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
_COVER_ORIG = b"""<edgarSubmission><submissionType>13F-HR</submissionType>
<filingManager><name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>50.10</tableValueTotal>
</edgarSubmission>"""
_TABLE_ORIG = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50.10</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable></informationTable>"""

_COVER_AMEND = b"""<edgarSubmission><submissionType>13F-HR/A</submissionType>
<filingManager><name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
<isAmendment>true</isAmendment>
<amendmentNo>1</amendmentNo>
<amendmentInfo><amendmentType>NEW HOLDINGS</amendmentType></amendmentInfo>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>25.00</tableValueTotal>
</edgarSubmission>"""
_TABLE_AMEND = b"""<informationTable><infoTable><nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>594918104</cusip><value>25.00</value>
<shrsOrPrnAmt><sshPrnamt>5</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable></informationTable>"""


class _Submissions:
    def fetch(self, filer_cik: str) -> RawRecord:
        accepted_amend = datetime(2025, 2, 15, 18, tzinfo=UTC)
        return RawRecord(
            record_id=uuid4(),
            asset_id=None,
            source=SourceReference(
                source_id=f"sec-edgar:manager:{filer_cik}:submissions", retrieved_at=_NOW
            ),
            event_time=accepted_amend,
            available_at=accepted_amend,
            received_at=_NOW,
            payload={
                "document": {
                    "cik": filer_cik,
                    "name": "Manager LLC",
                    "filings": {
                        "recent": {
                            "accessionNumber": ["0000950123-25-000001", "0000950123-25-000002"],
                            "filingDate": ["2025-02-14", "2025-02-15"],
                            "reportDate": ["2024-12-31", "2024-12-31"],
                            "acceptanceDateTime": [
                                "2025-02-14T18:00:00Z",
                                "2025-02-15T18:00:00Z",
                            ],
                            "form": ["13F-HR", "13F-HR/A"],
                            "primaryDocument": [
                                "xslForm13F_X02/primary_doc.xml",
                                "xslForm13F_X02/primary_doc.xml",
                            ],
                        }
                    },
                }
            },
            schema_version="sec-manager-submissions-snapshot-v1",
        )


class _Documents:
    retrieved_at = _NOW

    def fetch_manifest(self, document: object) -> SecAccessionManifest:
        del document
        return SecAccessionManifest(
            entries=("xslForm13F_X02/primary_doc.xml", "primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self.retrieved_at,
        )

    def fetch(self, document: object) -> SecPrimaryDocumentResponse:
        name = getattr(document, "name", "")
        filing = getattr(document, "filing", None)
        acc = getattr(filing, "accession", "") if filing else ""
        if name == "xslForm13F_X02/primary_doc.xml":
            content = b"<!DOCTYPE html><html><body>locator</body></html>"
        elif name == "primary_doc.xml":
            content = _COVER_ORIG if "000001" in acc else _COVER_AMEND
        else:
            content = _TABLE_ORIG if "000001" in acc else _TABLE_AMEND
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{name}",
            retrieved_at=self.retrieved_at,
        )


def test_effective_holdings_application_and_cli_integration(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
        reports = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _Submissions(), _Documents()
        ).run(
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                filer_cik="1067983",
                forms=("13F-HR", "13F-HR/A"),
            )
        )
        semantics_service = InstitutionalHoldingsSemanticsService(storage, clock=lambda: _NOW)
        semantics_service.enrich(
            InstitutionalSemanticsEnrichRequest(
                manager_cik="1067983",
                report_ids=tuple(item.report_id for item in reports),
                known_at=_NOW,
            )
        )

    location = StorageLocationRequest(legacy_root=tmp_path)
    app = CazatiburonesInstitutionalEffectiveHoldingsApplication.create_default()

    # Before amendment available_at
    cut_before = datetime(2025, 2, 15, 12, tzinfo=UTC)
    res_before = app.query(
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        known_at=cut_before,
        offset=0,
        limit=1000,
        location=location,
    )
    assert res_before.status == "effective"
    assert res_before.total_rows == 1
    assert len(res_before.contributors) == 1

    # After amendment available_at
    cut_after = datetime(2025, 2, 16, 0, tzinfo=UTC)
    res_after = app.query(
        manager_cik="0001067983",
        report_period=date(2024, 12, 31),
        known_at=cut_after,
        offset=0,
        limit=1000,
        location=location,
    )
    assert res_after.status == "effective"
    assert res_after.total_rows == 2
    assert len(res_after.contributors) == 2

    # CLI call produces exact same result
    cli_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "query_sec_institutional_effective_holdings.py"
    )
    env = dict(os.environ)
    src_path = str(Path(__file__).resolve().parents[3] / "src")
    env["PYTHONPATH"] = (
        f"{src_path}:{env.get('PYTHONPATH', '')}" if "PYTHONPATH" in env else src_path
    )
    process = subprocess.run(
        [
            sys.executable,
            str(cli_path),
            "--root",
            str(tmp_path),
            "--manager-cik",
            "0001067983",
            "--report-period",
            "2024-12-31",
            "--known-at",
            "2025-02-16T00:00:00Z",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    cli_result = json.loads(process.stdout)
    assert cli_result == json.loads(res_after.model_dump_json())
