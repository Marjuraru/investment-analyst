import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.evidence.instrument_correspondence.models import InstrumentCorrespondence
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    SecInstitutionalRowCorrespondence,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import monetary_value
from investment_analyst.evidence.sec_institutional_observations.normalizer import normalize_row
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
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

_NOW = datetime(2025, 2, 16, 12, 0, tzinfo=UTC)
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


def _artifact(storage):
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
    return report, InstitutionalSemanticsRepository(storage.raw_records).get_for_parent(report)


def _row_claim(artifact, row) -> SecInstitutionalRowCorrespondence:
    return SecInstitutionalRowCorrespondence.claim(
        asset_id="equity:us:aapl",
        cusip=row.cusip,
        title_of_class=row.title_of_class,
        report_period=artifact.report_period,
        manager_cik=artifact.manager_cik,
        report_id=artifact.parent_report_id,
        artifact_id=artifact.artifact_id,
        row_id=row.row_id,
        universe_snapshot_id=uuid4(),
        dataset_revision_id=uuid4(),
        candidate_id=uuid4(),
        available_at=max(_NOW, artifact.available_at),
        recorded_at=_NOW,
    )


@pytest.mark.parametrize(
    ("accepted_at", "value", "quality"),
    [
        (datetime(2023, 1, 2, 23, 59, tzinfo=UTC), Decimal("1000"), "partial"),
        (datetime(2023, 1, 3, tzinfo=UTC), Decimal("1"), "valid"),
    ],
)
def test_normalizer_monetary_values_are_decimal_exact(
    accepted_at: datetime, value: Decimal, quality: str
) -> None:
    actual, actual_quality = monetary_value(Decimal("1"), accepted_at=accepted_at)

    assert actual == value
    assert actual_quality == quality


def test_normalize_row_accepts_a_row_scoped_claim_without_changing_values(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        report, artifact = _artifact(storage)
        row = artifact.rows[0]
        manual = InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip=row.cusip,
            title_of_class=row.title_of_class,
            effective_from=artifact.report_period,
            effective_to=date(2025, 1, 1),
            available_at=_NOW,
            recorded_at=_NOW,
        )
        scoped = _row_claim(artifact, row)
        normalized_at = _NOW
        manual_values = normalize_row(artifact, row, manual, normalized_at=normalized_at)
        scoped_values = normalize_row(artifact, row, scoped, normalized_at=normalized_at)

    assert [item.field_name for item in scoped_values] == [
        item.field_name for item in manual_values
    ]
    assert [item.value for item in scoped_values] == [item.value for item in manual_values]
    assert [item.unit for item in scoped_values] == [item.unit for item in manual_values]
    assert [item.quality for item in scoped_values] == [item.quality for item in manual_values]
    assert scoped_values
    for scoped_value, manual_value in zip(scoped_values, manual_values, strict=True):
        assert scoped_value.observation_id != manual_value.observation_id
        assert scoped_value.asset_id == manual_value.asset_id == "equity:us:aapl"
        assert scoped_value.transformation_version == manual_value.transformation_version
        assert str(scoped.correspondence_id) in scoped_value.source.record_key
        assert str(manual.correspondence_id) in manual_value.source.record_key
    assert scoped_values[0].available_at == max(scoped.available_at, artifact.available_at)
    assert scoped_values[0].period_end == datetime(2024, 12, 31, tzinfo=UTC)
    assert report.report_id == artifact.parent_report_id


def test_normalize_row_rejects_an_early_clock_and_missing_period(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        _, artifact = _artifact(storage)
        row = artifact.rows[0]
        scoped = _row_claim(artifact, row)
        with pytest.raises(ValueError, match="not precede available evidence"):
            normalize_row(artifact, row, scoped, normalized_at=datetime(2024, 1, 1, tzinfo=UTC))
        without_period = artifact.model_copy(update={"report_period": None})
        assert normalize_row(without_period, row, scoped, normalized_at=_NOW) == ()
