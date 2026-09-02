import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.application.cazatiburones_institutional_composition import (
    CazatiburonesInstitutionalCompositionApplication,
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
_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType>
<filingManager><name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>50.10</tableValueTotal>
</edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50.10</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
</infoTable></informationTable>"""


class _Submissions:
    def fetch(self, filer_cik: str) -> RawRecord:
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

    def fetch_manifest(self, document: object) -> SecAccessionManifest:
        del document
        return SecAccessionManifest(
            entries=("primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self.retrieved_at,
        )

    def fetch(self, document: object) -> SecPrimaryDocumentResponse:
        name = document.name
        content = _COVER if name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{name}",
            retrieved_at=self.retrieved_at,
        )


def test_read_only_application_and_cli_return_the_same_pit_close(tmp_path: Path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
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
    with LocalStorage(paths, read_only=True) as storage:
        raw_before = storage.raw_records.count()

    location = StorageLocationRequest(legacy_root=tmp_path)
    result = CazatiburonesInstitutionalCompositionApplication.create_default().query(
        manager_cik="1067983", known_at=_NOW, location=location
    )

    assert len(result) == 1
    assert result[0].status == "original_complete"
    assert result[0].declared_value_total == result[0].observed_value_total
    with LocalStorage(paths, read_only=True) as storage:
        assert storage.raw_records.count() == raw_before

    script = Path(__file__).parents[3] / "scripts" / "query_sec_institutional_composition.py"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--root",
            str(tmp_path),
            "--manager-cik",
            "1067983",
            "--known-at",
            _NOW.isoformat(),
        ],
        capture_output=True,
        check=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )
    from_cli = json.loads(completed.stdout)
    assert from_cli[0]["status"] == "original_complete"
    assert from_cli[0]["policy_version"] == "sec-13f-composition-policy-v1"
