from datetime import UTC, datetime

import pytest

from investment_analyst.analytics.cazatiburones.institutional_metric_pipeline import (
    InstitutionalMetricPipeline,
)
from investment_analyst.storage import StorageError


class _Storage:
    read_only = True


def test_pipeline_requires_writable_storage() -> None:
    with pytest.raises(StorageError, match="writable"):
        InstitutionalMetricPipeline(_Storage()).compute(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            known_at=datetime(2025, 1, 1, tzinfo=UTC),
        )


def test_manager_scoped_inputs_match_full_history_at_two_cuts(tmp_path) -> None:
    import hashlib
    from datetime import date, timedelta

    from investment_analyst.application.cazatiburones_institutional_observations import (
        CazatiburonesInstitutionalObservationsApplication,
    )
    from investment_analyst.application.runtime import StorageLocationRequest
    from investment_analyst.evidence.instrument_correspondence.models import (
        InstrumentCorrespondence,
    )
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
    from investment_analyst.storage import LocalStorage, StoragePaths

    now = datetime(2025, 2, 16, tzinfo=UTC)
    cover = (
        b"<edgarSubmission><submissionType>13F-HR</submissionType>"
        b"<filingManager><name>Manager LLC</name></filingManager>"
        b"<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>"
        b"<tableEntryTotal>1</tableEntryTotal><tableValueTotal>50.10</tableValueTotal>"
        b"</edgarSubmission>"
    )
    table = (
        b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
        b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>50.10</value>"
        b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
        b"</shrsOrPrnAmt></infoTable></informationTable>"
    )

    class _Submissions:
        def fetch(self, filer_cik: str):
            from uuid import uuid4

            from investment_analyst.core.models import RawRecord, SourceReference

            accepted = datetime(2025, 2, 14, 18, tzinfo=UTC)
            return RawRecord(
                record_id=uuid4(),
                asset_id=None,
                source=SourceReference(
                    source_id=f"sec-edgar:manager:{filer_cik}:submissions",
                    retrieved_at=accepted,
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
            content = cover if document.name == "primary_doc.xml" else table
            return SecPrimaryDocumentResponse(
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                url=f"https://www.sec.gov/Archives/{document.name}",
                retrieved_at=self.retrieved_at,
            )

    paths = StoragePaths.from_root(tmp_path)
    with LocalStorage(paths) as storage:
        foreign_reports: list[object] = []
        for filer_cik in ("1067983", "0001234567"):
            reports = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
                storage, _Submissions(), _Documents()
            ).run(
                sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                    filer_cik=filer_cik, forms=("13F-HR",)
                )
            )
            manager = "0001067983" if filer_cik == "1067983" else "0001234567"
            InstitutionalHoldingsSemanticsService(storage, clock=lambda: now).enrich(
                InstitutionalSemanticsEnrichRequest(
                    manager_cik=manager,
                    report_ids=tuple(report.report_id for report in reports),
                    known_at=now,
                )
            )
            target = next(report for report in reports)
            if filer_cik != "1067983":
                foreign_reports.append(target)
        correspondence = InstrumentCorrespondence.declare(
            asset_id="equity:us:aapl",
            cusip="037833100",
            title_of_class="COM",
            effective_from=date(2020, 1, 1),
            effective_to=None,
            available_at=now,
            recorded_at=now,
        )
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            correspondence, catalog_version=1, declared_by="test"
        )
        foreign_correspondence = InstrumentCorrespondence.declare(
            asset_id="equity:us:msft",
            cusip="594918104",
            title_of_class="COM",
            effective_from=date(2020, 1, 1),
            effective_to=None,
            available_at=now,
            recorded_at=now,
        )
        InstrumentCorrespondenceRepository(storage.raw_records).save(
            foreign_correspondence, catalog_version=1, declared_by="test"
        )
    location = StorageLocationRequest(legacy_root=tmp_path)
    with LocalStorage(paths) as storage:
        from investment_analyst.evidence.sec_institutional_holdings.repository import (
            InstitutionalHoldingsRepository,
        )

        holdings = InstitutionalHoldingsRepository(storage.raw_records)
        target_reports = holdings.list_reports(manager_cik="0001067983", known_at=now)
    CazatiburonesInstitutionalObservationsApplication.create_default().normalize(
        InstitutionalObservationRequest(
            asset_id="equity:us:aapl",
            manager_cik="1067983",
            report_ids=tuple(report.report_id for report in target_reports),
            known_at=now,
        ),
        location=location,
    )
    with LocalStorage(paths) as storage:
        raw_lists = storage.raw_records.list
        observation_lists = storage.observations.list
        metric_lists = storage.metric_results.list

        def forbidden_raw_list(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("metric pipeline hydrated full raw history")

        def forbidden_observation_list(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("metric pipeline hydrated full observation history")

        def forbidden_metric_list(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("metric pipeline listed metric history")

        storage.raw_records.list = forbidden_raw_list  # type: ignore[method-assign]
        storage.observations.list = forbidden_observation_list  # type: ignore[method-assign]
        storage.metric_results.list = forbidden_metric_list  # type: ignore[method-assign]
        try:
            first = InstitutionalMetricPipeline(
                storage, clock=lambda: now + timedelta(seconds=1)
            ).compute(asset_id="equity:us:aapl", manager_cik="1067983", known_at=now)
            second = InstitutionalMetricPipeline(
                storage, clock=lambda: now + timedelta(seconds=2)
            ).compute(asset_id="equity:us:aapl", manager_cik="1067983", known_at=now)
        finally:
            storage.raw_records.list = raw_lists  # type: ignore[method-assign]
            storage.observations.list = observation_lists  # type: ignore[method-assign]
            storage.metric_results.list = metric_lists  # type: ignore[method-assign]
    assert foreign_reports
    assert first.metrics_created + first.metrics_reused >= 0
    assert second.metrics_reused >= first.metrics_reused or second.metrics_created == 0
    with LocalStorage(paths, read_only=True) as storage:
        results = storage.metric_results.list(asset_id="equity:us:aapl")
        assert all(result.parameters.get("manager_cik") == "0001067983" for result in results)
