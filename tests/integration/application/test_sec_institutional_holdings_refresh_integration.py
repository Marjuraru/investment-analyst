"""Integration tests for the directed Form 13F refresh over the persisted manager universe."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_institutional_holdings_refresh import (
    SecInstitutionalHoldingsDirectedPeriodsRefreshRequest,
    SecInstitutionalHoldingsDirectedRefreshApplication,
)
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    SecInstitutionalHoldingsDirectedRefreshRequest,
)
from investment_analyst.application.sec_institutional_universe import (
    SecInstitutionalUniverseApplication,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseRefreshRequest,
    SecInstitutionalUniverseRefreshResult,
)
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_PERIOD = date(2026, 3, 31)
_REPORT_PERIOD_XML = "03-31-2026"
_PRIMARY_DOCUMENT = "xslForm13F_X02/primary_doc.xml"
_ZIP_URL = (
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    "01mar2026-31may2026_form13f.zip"
)
_IDENTITY = SecEdgarIdentity("Analyst user@example.com")
_MANAGERS = {
    "0001067983": ("BERKSHIRE HATHAWAY INC", "80000"),
    "0000000002": ("ALPHA ASSET MANAGEMENT", "50000"),
    "0000000003": ("BETA PARTNERS LP", "10000"),
}


def _universe_zip() -> bytes:
    submissions = ["ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"]
    coverpage = ["ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT"]
    infotable = ["ACCESSION_NUMBER\tCUSIP\tVALUE"]
    for cik, (name, value) in _MANAGERS.items():
        accession = f"{cik}-26-000010"
        submissions.append(f"{accession}\t15-APR-2026\t13F-HR\t{cik}\t31-MAR-2026")
        coverpage.append(f"{accession}\t{name}\tN")
        infotable.append(f"{accession}\t037833100\t{value}")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SUBMISSION.tsv", "\n".join(submissions) + "\n")
        archive.writestr("COVERPAGE.tsv", "\n".join(coverpage) + "\n")
        archive.writestr("INFOTABLE.tsv", "\n".join(infotable) + "\n")
    return stream.getvalue()


class _UniverseTransport(HttpTransport):
    def __init__(self, zip_bytes: bytes) -> None:
        self.zip_bytes = zip_bytes

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        if url == SEC_13F_DATA_SETS_CATALOG_URL:
            body = f'<html><a href="{_ZIP_URL}">2026 Q1</a></html>'.encode()
        elif url == _ZIP_URL:
            body = self.zip_bytes
        else:
            raise RuntimeError(f"Unexpected URL request: {url}")
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def _filing(accession: str, *, accepted_at: datetime, form: str = "13F-HR") -> dict[str, str]:
    return {
        "accession": accession,
        "form": form,
        "report_date": "2026-03-31",
        "filing_date": "2026-04-14",
        "accepted_at": accepted_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _submissions_record(filer_cik: str, filings: tuple[dict[str, str], ...]) -> RawRecord:
    retrieved_at = max(
        datetime.fromisoformat(item["accepted_at"].replace("Z", "+00:00")) for item in filings
    )
    record_id = uuid5(
        NAMESPACE_URL,
        f"sec-manager-submissions|{filer_cik}|{json.dumps(filings, sort_keys=True)}",
    )
    return RawRecord(
        record_id=record_id,
        asset_id=None,
        source=SourceReference(
            source_id=f"sec-edgar:manager:{filer_cik}:submissions",
            retrieved_at=retrieved_at,
        ),
        event_time=retrieved_at,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={
            "document": {
                "cik": filer_cik,
                "name": _MANAGERS[filer_cik][0],
                "filings": {
                    "recent": {
                        "accessionNumber": [item["accession"] for item in filings],
                        "filingDate": [item["filing_date"] for item in filings],
                        "reportDate": [item["report_date"] for item in filings],
                        "acceptanceDateTime": [item["accepted_at"] for item in filings],
                        "form": [item["form"] for item in filings],
                        "primaryDocument": [_PRIMARY_DOCUMENT for _ in filings],
                    }
                },
            }
        },
        schema_version="sec-manager-submissions-snapshot-v1",
    )


class _ManagerSubmissionsClient:
    """Serve one fresh Submissions response per manager with deterministic snapshot identity."""

    def __init__(self, filings_by_cik: dict[str, tuple[dict[str, str], ...]]) -> None:
        self.filings_by_cik = dict(filings_by_cik)
        self.fail_for: set[str] = set()
        self.calls: list[str] = []

    def fetch(self, filer_cik: str) -> RawRecord:
        cik = normalize_cik(filer_cik)
        self.calls.append(cik)
        if cik in self.fail_for:
            raise RuntimeError("SEC Submissions is unavailable for this manager")
        return _submissions_record(cik, self.filings_by_cik[cik])


class _DocumentClient:
    def __init__(self, *, forms: dict[str, str] | None = None) -> None:
        self.forms = forms or {}
        self.manifest_calls = 0
        self.document_calls = 0

    @property
    def archives_calls(self) -> int:
        return self.manifest_calls + self.document_calls

    def fetch_manifest(self, document):
        from investment_analyst.providers.fundamentals.sec_document_client import (
            SecAccessionManifest,
        )

        self.manifest_calls += 1
        return SecAccessionManifest(
            entries=("filing.htm", "primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=document.filing.accepted_at,
        )

    def fetch(self, document):
        from investment_analyst.providers.fundamentals.sec_document_client import (
            SecPrimaryDocumentResponse,
        )

        self.document_calls += 1
        accession = document.filing.accession
        form = self.forms.get(accession, document.filing.form)
        if document.name == _PRIMARY_DOCUMENT:
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        elif document.name == "primary_doc.xml":
            content = (
                f"<edgarSubmission><submissionType>{form}</submissionType><filingManager>"
                "<name>Manager LLC</name></filingManager>"
                f"<reportCalendarOrQuarter>{_REPORT_PERIOD_XML}</reportCalendarOrQuarter>"
                "<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal>"
                "</edgarSubmission>"
            ).encode()
        else:
            content = (
                b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
                b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
                b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
                b"</shrsOrPrnAmt></infoTable></informationTable>"
            )
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{accession}/{document.name}",
            retrieved_at=document.filing.accepted_at,
        )


def _materialize_universe(
    location: StorageLocationRequest,
) -> SecInstitutionalUniverseRefreshResult:
    transport = _UniverseTransport(_universe_zip())
    return SecInstitutionalUniverseApplication(
        ApplicationRuntime.create_default(), transport_factory=lambda: transport
    ).refresh(
        SecInstitutionalUniverseRefreshRequest(),
        sec_identity=_IDENTITY,
        location=location,
    )


def _application(
    submissions: _ManagerSubmissionsClient, documents: _DocumentClient
) -> SecInstitutionalHoldingsDirectedRefreshApplication:
    return SecInstitutionalHoldingsDirectedRefreshApplication(
        ApplicationRuntime.create_default(),
        submissions_client_factory=lambda *args: submissions,
        document_client_factory=lambda *args: documents,
    )


def _one_filing_per_manager(accepted_at: datetime) -> dict[str, tuple[dict[str, str], ...]]:
    return {cik: (_filing(f"{cik}-26-000010", accepted_at=accepted_at),) for cik in _MANAGERS}


def test_directed_page_calls_submissions_once_per_manager_and_reuses_evidence(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    submissions = _ManagerSubmissionsClient(_one_filing_per_manager(accepted_at))
    documents = _DocumentClient()

    first = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_offset=0, manager_limit=2, accessions_per_manager=1
        ),
        sec_identity=_IDENTITY,
        location=location,
    )

    assert first.effective_known_at == known_at
    assert first.snapshot_id == universe.snapshot_id
    assert first.dataset_revision_id == universe.revision_id
    assert first.universe_selected_manager_count == len(_MANAGERS)
    assert first.page_manager_count == 2
    assert first.submissions_calls == 2
    assert submissions.calls == ["0001067983", "0000000002"]
    assert first.created == 2 and first.reused == 0
    assert first.rejected_or_failed == 0
    assert first.backlog_after == 0
    assert first.archives_calls == documents.archives_calls == 8
    assert first.semantics_created == 2 and first.semantics_reused == 0
    assert first.traceability_verified is True
    assert [item.manager_cik for item in first.managers] == ["0001067983", "0000000002"]
    for manager in first.managers:
        assert manager.state == "processed" and manager.reason_code is None
        assert manager.submissions_calls == 1 and manager.submissions_created == 1
        assert manager.candidate_cusips == ("037833100",)
        assert manager.report_period == _PERIOD
        assert manager.created_accessions == (f"{manager.manager_cik}-26-000010",)
        assert len(manager.report_ids) == 1
        assert manager.archives_calls == 4

    second = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_offset=0, manager_limit=2, accessions_per_manager=1
        ),
        sec_identity=_IDENTITY,
        location=location,
    )

    assert second.submissions_calls == 2
    assert second.created == 0 and second.reused == 2
    assert second.archives_calls == 0
    assert documents.archives_calls == first.archives_calls
    assert second.semantics_created == 0 and second.semantics_reused == 2
    assert second.traceability_verified is True
    assert [item.report_ids for item in second.managers] == [
        item.report_ids for item in first.managers
    ]
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 2
        )


def test_directed_refresh_imports_a_new_amendment_without_rewriting_previous_reports(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    amendment = universe.available_at - timedelta(hours=1)
    submissions = _ManagerSubmissionsClient(_one_filing_per_manager(accepted_at))
    documents = _DocumentClient()
    request = SecInstitutionalHoldingsDirectedRefreshRequest(
        known_at=known_at, manager_limit=1, accessions_per_manager=5
    )

    first = _application(submissions, documents).refresh(
        request, sec_identity=_IDENTITY, location=location
    )
    original_reports = first.managers[0].report_ids
    archives_after_first = documents.archives_calls
    assert first.created == 1 and first.backlog_after == 0

    cik = "0001067983"
    submissions.filings_by_cik[cik] = (
        _filing(f"{cik}-26-000010", accepted_at=accepted_at),
        _filing(f"{cik}-26-000011", accepted_at=amendment, form="13F-HR/A"),
    )
    documents.forms[f"{cik}-26-000011"] = "13F-HR/A"

    second = _application(submissions, documents).refresh(
        request, sec_identity=_IDENTITY, location=location
    )
    manager = second.managers[0]
    assert manager.pending_before == 1
    assert manager.reused_accessions == (f"{cik}-26-000010",)
    assert manager.created_accessions == (f"{cik}-26-000011",)
    assert manager.backlog_after == 0
    assert len(manager.report_ids) == 2
    assert set(original_reports).issubset(set(manager.report_ids))
    assert documents.archives_calls - archives_after_first == 4
    assert second.archives_calls == 4
    assert second.semantics_created == 1 and second.semantics_reused == 1

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        persisted = repository.list_reports(manager_cik=cik, known_at=known_at)
        assert {report.report_id for report in persisted} == set(original_reports) | set(
            manager.report_ids
        )
        semantics = InstitutionalSemanticsRepository(storage.raw_records)
        assert all(semantics.get_for_parent(report) is not None for report in persisted)


def test_directed_refresh_reports_exact_backlog_and_resumes_the_same_manager(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    cik = "0001067983"
    submissions = _ManagerSubmissionsClient(
        {
            cik: (
                _filing(f"{cik}-26-000010", accepted_at=accepted_at),
                _filing(f"{cik}-26-000011", accepted_at=accepted_at + timedelta(hours=1)),
                _filing(f"{cik}-26-000012", accepted_at=accepted_at + timedelta(hours=2)),
            )
        }
    )
    documents = _DocumentClient()

    first = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_limit=1, accessions_per_manager=1
        ),
        sec_identity=_IDENTITY,
        location=location,
    )
    assert first.managers[0].pending_before == 3
    assert first.managers[0].created_accessions == (f"{cik}-26-000010",)
    assert first.backlog_after == 2
    first_archives = documents.archives_calls

    second = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_limit=1, accessions_per_manager=2
        ),
        sec_identity=_IDENTITY,
        location=location,
    )
    assert second.managers[0].pending_before == 2
    assert second.managers[0].reused_accessions == (f"{cik}-26-000010",)
    assert second.managers[0].created_accessions == (f"{cik}-26-000011", f"{cik}-26-000012")
    assert second.backlog_after == 0
    assert second.created == 2 and second.reused == 1
    assert documents.archives_calls - first_archives == 8

    terminal = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_limit=1, accessions_per_manager=2
        ),
        sec_identity=_IDENTITY,
        location=location,
    )
    assert terminal.created == 0 and terminal.reused == 3
    assert terminal.archives_calls == 0
    assert terminal.backlog_after == 0
    assert terminal.traceability_verified is True


def test_directed_refresh_continues_after_a_manager_failure_and_preserves_progress(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    submissions = _ManagerSubmissionsClient(_one_filing_per_manager(accepted_at))
    submissions.fail_for = {"0000000002"}
    documents = _DocumentClient()

    result = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_limit=3, accessions_per_manager=1
        ),
        sec_identity=_IDENTITY,
        location=location,
    )

    assert [item.manager_cik for item in result.managers] == [
        "0001067983",
        "0000000002",
        "0000000003",
    ]
    failed = result.managers[1]
    assert failed.state == "failed" and failed.reason_code == "RuntimeError"
    assert failed.submissions_calls == 1 and failed.submissions_raw_record_id is None
    assert failed.created_accessions == () and failed.backlog_after == 0
    assert failed.semantics_examined == 0
    assert result.submissions_calls == 3
    assert result.created == 2 and result.rejected_or_failed == 0
    assert result.traceability_verified is False

    submissions.fail_for = set()
    recovered = _application(submissions, documents).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_limit=3, accessions_per_manager=1
        ),
        sec_identity=_IDENTITY,
        location=location,
    )
    assert recovered.created == 1 and recovered.reused == 2
    assert recovered.managers[1].state == "processed"
    assert recovered.managers[1].created_accessions == ("0000000002-26-000010",)
    assert recovered.traceability_verified is True
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 3
        )


def test_refresh_with_storage_reuses_single_writer_connection(tmp_path: Path) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    submissions = _ManagerSubmissionsClient(_one_filing_per_manager(accepted_at))
    documents = _DocumentClient()
    app = _application(submissions, documents)

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        result = app.refresh_with_storage(
            storage,
            SecInstitutionalHoldingsDirectedRefreshRequest(
                known_at=known_at, manager_limit=1, accessions_per_manager=1
            ),
            sec_identity=_IDENTITY,
        )
        assert result.created == 1
        assert result.traceability_verified is True
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 1
        )


_OLDER_PERIOD = date(2025, 12, 31)
_NEWER_PERIOD = date(2026, 3, 31)
_OLDER_ACCESSION = "0001067983-26-000001"
_NEWER_ACCESSION = "0001067983-26-000010"


def _pair_filing(
    accession: str, *, accepted_at: datetime, report_period: date, form: str = "13F-HR"
) -> dict[str, str]:
    return {
        "accession": accession,
        "form": form,
        "report_date": report_period.isoformat(),
        "filing_date": "2026-04-14",
        "accepted_at": accepted_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


class _PeriodDocumentClient:
    """Archives double that renders each accession with its own report period."""

    def __init__(self, periods: dict[str, date]) -> None:
        self.periods = periods
        self.manifest_calls = 0
        self.document_calls = 0

    @property
    def archives_calls(self) -> int:
        return self.manifest_calls + self.document_calls

    def fetch_manifest(self, document):
        from investment_analyst.providers.fundamentals.sec_document_client import (
            SecAccessionManifest,
        )

        self.manifest_calls += 1
        del document
        return SecAccessionManifest(
            entries=("filing.htm", "primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=datetime(2026, 5, 1, tzinfo=UTC),
        )

    def fetch(self, document):
        from investment_analyst.providers.fundamentals.sec_document_client import (
            SecPrimaryDocumentResponse,
        )

        self.document_calls += 1
        accession = document.filing.accession
        period = self.periods[accession]
        if document.name == _PRIMARY_DOCUMENT:
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        elif document.name == "primary_doc.xml":
            content = (
                f"<edgarSubmission><submissionType>{document.filing.form}</submissionType>"
                "<filingManager><name>Manager LLC</name></filingManager>"
                f"<reportCalendarOrQuarter>{period.strftime('%m-%d-%Y')}</reportCalendarOrQuarter>"
                "<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal>"
                "</edgarSubmission>"
            ).encode()
        else:
            content = (
                b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
                b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
                b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
                b"</shrsOrPrnAmt></infoTable></informationTable>"
            )
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{accession}/{document.name}",
            retrieved_at=document.filing.accepted_at,
        )


def test_directed_periods_refresh_shares_one_submissions_for_both_periods(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    newer_accepted = universe.available_at - timedelta(days=1)
    older_accepted = newer_accepted - timedelta(days=90)
    known_at = universe.available_at + timedelta(days=1)
    filings = {
        "0001067983": (
            _pair_filing(_OLDER_ACCESSION, accepted_at=older_accepted, report_period=_OLDER_PERIOD),
            _pair_filing(_NEWER_ACCESSION, accepted_at=newer_accepted, report_period=_NEWER_PERIOD),
        )
    }
    submissions = _ManagerSubmissionsClient(filings)
    documents = _PeriodDocumentClient(
        {_OLDER_ACCESSION: _OLDER_PERIOD, _NEWER_ACCESSION: _NEWER_PERIOD}
    )
    app = _application(submissions, documents)
    request = SecInstitutionalHoldingsDirectedPeriodsRefreshRequest(
        known_at=known_at,
        manager_cik="1067983",
        report_periods=(_OLDER_PERIOD, _NEWER_PERIOD),
        accessions_per_period=1,
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first = app.refresh_periods_with_storage(storage, request, sec_identity=_IDENTITY)

        assert submissions.calls == ["0001067983"]
        assert first.submissions_calls == 1
        assert first.state == "processed" and first.reason_code is None
        assert first.created_accessions == (_OLDER_ACCESSION, _NEWER_ACCESSION)
        assert first.failed_accessions == ()
        assert first.backlog_after == 0
        assert {period.report_period for period in first.periods} == {
            _OLDER_PERIOD,
            _NEWER_PERIOD,
        }
        assert {period.submissions_raw_record_id for period in first.periods} == {
            first.periods[0].submissions_raw_record_id
        }
        assert first.semantics_created == 2
        assert first.traceability_verified is True
        assert len(first.report_ids) == 2
        archives_after_first = documents.archives_calls
        assert archives_after_first > 0

        second = app.refresh_periods_with_storage(storage, request, sec_identity=_IDENTITY)

        assert submissions.calls == ["0001067983", "0001067983"]
        assert second.submissions_calls == 1
        assert second.created_accessions == ()
        assert set(second.reused_accessions) == {_OLDER_ACCESSION, _NEWER_ACCESSION}
        assert second.semantics_created == 0 and second.semantics_reused == 2
        assert documents.archives_calls == archives_after_first
        assert second.traceability_verified is True
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 2
        )


def test_directed_periods_refresh_respects_the_cut_per_period(tmp_path: Path) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    newer_accepted = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    filings = {
        "0001067983": (
            _pair_filing(
                _OLDER_ACCESSION,
                accepted_at=known_at + timedelta(days=30),
                report_period=_OLDER_PERIOD,
            ),
            _pair_filing(_NEWER_ACCESSION, accepted_at=newer_accepted, report_period=_NEWER_PERIOD),
        )
    }
    submissions = _ManagerSubmissionsClient(filings)
    documents = _PeriodDocumentClient(
        {_OLDER_ACCESSION: _OLDER_PERIOD, _NEWER_ACCESSION: _NEWER_PERIOD}
    )
    app = _application(submissions, documents)
    request = SecInstitutionalHoldingsDirectedPeriodsRefreshRequest(
        known_at=known_at,
        manager_cik="1067983",
        report_periods=(_OLDER_PERIOD, _NEWER_PERIOD),
        accessions_per_period=1,
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        summary = app.refresh_periods_with_storage(storage, request, sec_identity=_IDENTITY)

        older, newer = summary.periods
        assert older.eligible_accessions == ()
        assert older.attempted_accessions == ()
        assert newer.eligible_accessions == (_NEWER_ACCESSION,)
        assert newer.created_accessions == (_NEWER_ACCESSION,)
        assert summary.created_accessions == (_NEWER_ACCESSION,)
        assert summary.submissions_calls == 1
        assert summary.traceability_verified is True
