from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from investment_analyst.evidence.sec_institutional_semantics import artifact_reader
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)


class _RawRecords:
    def __init__(self, record_id: UUID) -> None:
        self._record_id = record_id
        self.list_calls: list[datetime] = []
        self.get_calls: list[UUID] = []

    def list_record_ids(
        self,
        *,
        source_id: str,
        schema_version: str,
        available_to: datetime,
    ) -> list[UUID]:
        assert source_id == "sec-edgar:institutional-holdings-semantics"
        assert schema_version == "sec-institutional-holdings-semantics-v2"
        self.list_calls.append(available_to)
        return [self._record_id]

    def get(self, record_id: UUID) -> object:
        self.get_calls.append(record_id)
        return object()


def test_reader_memoizes_validated_artifacts_per_repository_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_id = uuid4()
    raw_records = _RawRecords(record_id)
    artifact = SimpleNamespace(raw_record_id=record_id)
    parsed: list[object] = []
    monkeypatch.setattr(
        artifact_reader,
        "semantics_from_raw_record",
        lambda record: parsed.append(record) or artifact,
    )

    first = InstitutionalSemanticsArtifactReader(raw_records)  # type: ignore[arg-type]
    second = InstitutionalSemanticsArtifactReader(raw_records)  # type: ignore[arg-type]
    known_at = datetime(2025, 2, 14, tzinfo=UTC)

    assert first.list_visible(known_at=known_at) == (artifact,)
    assert second.list_visible(known_at=known_at) == (artifact,)
    assert raw_records.list_calls == [known_at, known_at]
    assert raw_records.get_calls == [record_id]
    assert len(parsed) == 1


def test_reader_keeps_caches_independent_between_storage_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_id = uuid4()
    first_records = _RawRecords(record_id)
    second_records = _RawRecords(record_id)
    monkeypatch.setattr(
        artifact_reader,
        "semantics_from_raw_record",
        lambda record: SimpleNamespace(raw_record_id=record_id, record=record),
    )
    known_at = datetime(2025, 2, 14, tzinfo=UTC)

    first = InstitutionalSemanticsArtifactReader(first_records)  # type: ignore[arg-type]
    second = InstitutionalSemanticsArtifactReader(second_records)  # type: ignore[arg-type]

    assert (
        first.list_visible(known_at=known_at)[0].record
        is not second.list_visible(known_at=known_at)[0].record
    )
    assert first_records.get_calls == [record_id]
    assert second_records.get_calls == [record_id]


def test_manager_scoped_artifacts_preserve_order_and_revisions(tmp_path) -> None:
    import hashlib

    from investment_analyst.evidence.sec_institutional_semantics.repository import (
        InstitutionalSemanticsRepository,
        semantics_to_raw_record,
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

    cover = (
        b"<edgarSubmission><submissionType>13F-HR</submissionType>"
        b"<filingManager><name>Manager LLC</name></filingManager>"
        b"<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>"
        b"<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal>"
        b"</edgarSubmission>"
    )
    table = (
        b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
        b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
        b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
        b"</shrsOrPrnAmt></infoTable></informationTable>"
    )

    class _Submissions:
        def fetch(self, filer_cik):
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
            content = cover if document.name == "primary_doc.xml" else table
            return SecPrimaryDocumentResponse(
                content=content,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                url=f"https://www.sec.gov/Archives/{document.name}",
                retrieved_at=self.retrieved_at,
            )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        for filer_cik in ("1067983", "0001234567"):
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
                storage, _Submissions(), _Documents()
            ).run(
                sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                    filer_cik=filer_cik, forms=("13F-HR",)
                )
            )
        known_at = datetime(2025, 2, 16, tzinfo=UTC)
        service = InstitutionalHoldingsSemanticsService(storage, clock=lambda: known_at)
        expected: list[object] = []
        from investment_analyst.evidence.sec_institutional_holdings.repository import (
            InstitutionalHoldingsRepository,
        )

        holdings = InstitutionalHoldingsRepository(storage.raw_records)
        for filer_cik in ("1067983", "0001234567"):
            manager = "0001067983" if filer_cik == "1067983" else "0001234567"
            reports = holdings.list_reports(manager_cik=manager, known_at=known_at)
            assert reports
            service.enrich(
                InstitutionalSemanticsEnrichRequest(
                    manager_cik=manager,
                    report_ids=tuple(report.report_id for report in reports),
                    known_at=known_at,
                )
            )
        repository = InstitutionalSemanticsRepository(storage.raw_records)
        target_reports = holdings.list_reports(manager_cik="0001067983", known_at=known_at)
        assert target_reports
        for report in target_reports:
            expected.append(repository.get_for_parent(report))
        reader = InstitutionalSemanticsArtifactReader(storage.raw_records)
        scoped = reader.list_for_manager(manager_cik="1067983", known_at=known_at)
        assert [item.artifact_id for item in scoped] == [
            item.artifact_id
            for item in expected  # type: ignore[union-attr]
        ]
        assert all(item.manager_cik == "0001067983" for item in scoped)
        selected = storage.raw_records.select_record_ids_by_json_field(
            field="semantics_manager",
            values=("0001067983",),
            source_id="sec-edgar:institutional-holdings-semantics",
            schema_version="sec-institutional-holdings-semantics-v2",
            available_to=known_at,
        )
        assert [semantics_to_raw_record(item).record_id for item in scoped] == selected
        assert (
            reader.list_for_manager(
                manager_cik="0001234567", known_at=datetime(2025, 2, 14, 17, tzinfo=UTC)
            )
            == ()
        )
        hydrated: list[object] = []
        original_get_many = storage.raw_records.get_many

        def spy_get_many(record_ids):  # type: ignore[no-untyped-def]
            hydrated.append(tuple(record_ids))
            return original_get_many(record_ids)

        storage.raw_records.get_many = spy_get_many  # type: ignore[method-assign]
        again = reader.list_for_manager(manager_cik="0001067983", known_at=known_at)
        assert [item.artifact_id for item in again] == [item.artifact_id for item in scoped]
        assert sum(len(batch) for batch in hydrated) == len(scoped)
