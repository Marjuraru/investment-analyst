import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_weight_pipeline import (
    InstitutionalWeightPipeline,
)
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
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

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
            ),  # noqa: E501
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
            },  # noqa: E501
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
        )  # noqa: E501

    def fetch(self, document: object) -> SecPrimaryDocumentResponse:
        content = _COVER if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self.retrieved_at,
        )  # noqa: E501


def test_pipeline_persists_one_effective_close_weight_idempotently(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
        report = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _Submissions(), _Documents()
        ).run(
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                filer_cik="1067983", forms=("13F-HR",)
            )
        )[0]  # noqa: E501
        InstitutionalHoldingsSemanticsService(storage, clock=lambda: _NOW).enrich(
            InstitutionalSemanticsEnrichRequest(
                manager_cik="1067983", report_ids=(report.report_id,), known_at=_NOW
            )
        )  # noqa: E501
        correspondence = InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip="037833100",
            title_of_class="COM",
            effective_from=date(2020, 1, 1),
            effective_to=None,
            available_at=_NOW,
            recorded_at=_NOW,
        )  # noqa: E501
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            correspondence, catalog_version=1, declared_by="test"
        )  # noqa: E501
    location = StorageLocationRequest(legacy_root=tmp_path)
    CazatiburonesInstitutionalObservationsApplication.create_default().normalize(
        InstitutionalObservationRequest(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            report_ids=(report.report_id,),
            known_at=_NOW,
        ),
        location=location,
    )  # noqa: E501
    with LocalStorage(paths) as storage:
        pipeline = InstitutionalWeightPipeline(storage, clock=lambda: _NOW + timedelta(seconds=1))
        first = pipeline.compute(asset_id="equity:us:aapl", manager_cik="1067983", known_at=_NOW)
        second = pipeline.compute(asset_id="equity:us:aapl", manager_cik="1067983", known_at=_NOW)
        results = storage.metric_results.list(asset_id="equity:us:aapl")
    assert first.metrics_created == 1
    assert second.metrics_reused == 1
    assert results[0].value == 1
    assert results[0].parameters["effective_accession"] == "0000950123-25-000001"


def test_pipeline_requires_writable_storage(tmp_path) -> None:
    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths):
        pass
    with LocalStorage(paths, read_only=True) as storage:
        pipeline = InstitutionalWeightPipeline(storage)
        try:
            pipeline.compute(asset_id="equity:us:aapl", manager_cik="1067983", known_at=_NOW)
        except StorageError:
            pass
        else:
            raise AssertionError("read-only storage must be rejected")


def test_pipeline_omits_asset_absent_from_a_resolved_close(tmp_path) -> None:
    """A close with zero rows for the requested asset is a typed omission, never a zero weight."""
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
        # No InstrumentCorrespondence and no institutional observations are normalized for
        # this asset: the resolved close exists, but the requested asset has no row in it.
    with LocalStorage(paths) as storage:
        pipeline = InstitutionalWeightPipeline(storage, clock=lambda: _NOW + timedelta(seconds=1))
        summary = pipeline.compute(asset_id="equity:us:aapl", manager_cik="1067983", known_at=_NOW)
        results = storage.metric_results.list(asset_id="equity:us:aapl")
    assert summary.metrics_created == 0
    assert summary.skipped_by_reason == {"missing_position": 2}
    assert results == []
