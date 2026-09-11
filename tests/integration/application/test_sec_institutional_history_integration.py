"""Integration tests for the two-close institutional 13F history window."""

from __future__ import annotations

import hashlib
import io
import zipfile
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.alerts.cazatiburones_notifications import CazatiburonesNotificationStore
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_institutional_history import (
    SecInstitutionalHistoryApplication,
)
from investment_analyst.application.sec_institutional_history_models import (
    SecInstitutionalHistoryRequest,
)
from investment_analyst.application.sec_institutional_history_state import (
    SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME,
    SecInstitutionalHistoryStateStore,
)
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.providers.institutional_holdings.sec_manager_submissions import (
    MANAGER_SUBMISSIONS_SCHEMA_VERSION,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.models import WorkspaceAccessMode

_OLDER_WINDOW = (date(2025, 12, 1), date(2026, 2, 28))
_NEWER_WINDOW = (date(2026, 3, 1), date(2026, 5, 31))
_THIRD_WINDOW = (date(2025, 9, 1), date(2025, 11, 30))
_OLDER_PERIOD = date(2025, 12, 31)
_NEWER_PERIOD = date(2026, 3, 31)
_ZIP_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
_OLDER_URL = f"{_ZIP_BASE}01dec2025-28feb2026_form13f.zip"
_NEWER_URL = f"{_ZIP_BASE}01mar2026-31may2026_form13f.zip"
_THIRD_URL = f"{_ZIP_BASE}01sep2025-30nov2025_form13f.zip"
_IDENTITY = SecEdgarIdentity("Analyst user@example.com")
_PRIMARY_DOCUMENT = "xslForm13F_X02/primary_doc.xml"
_CUSIP = "037833100"

# Two managers hold AAPL in both closes, one only in the newer and one only in the older.
_BOTH_MANAGERS = {
    "0000000002": ("ALPHA ASSET MANAGEMENT", Decimal("50000"), Decimal("60000")),
    "0001067983": ("BERKSHIRE HATHAWAY INC", Decimal("80000"), Decimal("100000")),
}
_NEWER_ONLY_MANAGER = "0000000003", "BETA PARTNERS LP", Decimal("10000")
_OLDER_ONLY_MANAGER = "0000000004", "GAMMA CAPITAL LLC", Decimal("20000")


def _older_accession(cik: str) -> str:
    return f"{cik}-26-000001"


def _newer_accession(cik: str) -> str:
    return f"{cik}-26-000010"


def _dataset_zip(rows: tuple[tuple[str, str, str, Decimal], ...]) -> bytes:
    """Build one official-format dataset archive: (accession, cik, name, value)."""
    submissions = ["ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"]
    coverpage = ["ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT"]
    infotable = ["ACCESSION_NUMBER\tCUSIP\tVALUE"]
    for accession, cik, name, value in rows:
        period = "31-DEC-2025" if accession == _older_accession(cik) else "31-MAR-2026"
        filing_date = "15-FEB-2026" if period == "31-DEC-2025" else "15-APR-2026"
        submissions.append(f"{accession}\t{filing_date}\t13F-HR\t{cik}\t{period}")
        coverpage.append(f"{accession}\t{name}\tN")
        infotable.append(f"{accession}\t{_CUSIP}\t{value}")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SUBMISSION.tsv", "\n".join(submissions) + "\n")
        archive.writestr("COVERPAGE.tsv", "\n".join(coverpage) + "\n")
        archive.writestr("INFOTABLE.tsv", "\n".join(infotable) + "\n")
    return stream.getvalue()


def _older_rows() -> tuple[tuple[str, str, str, Decimal], ...]:
    rows = [
        (_older_accession(cik), cik, name, older)
        for cik, (name, older, _) in _BOTH_MANAGERS.items()
    ]
    cik, name, value = _OLDER_ONLY_MANAGER
    rows.append((_older_accession(cik), cik, name, value))
    return tuple(rows)


def _newer_rows() -> tuple[tuple[str, str, str, Decimal], ...]:
    rows = [
        (_newer_accession(cik), cik, name, newer)
        for cik, (name, _, newer) in _BOTH_MANAGERS.items()
    ]
    cik, name, value = _NEWER_ONLY_MANAGER
    rows.append((_newer_accession(cik), cik, name, value))
    return tuple(rows)


def _catalog_html() -> bytes:
    links = "".join(
        f'<a href="/files/structureddata/data/form-13f-data-sets/{url.rsplit("/", 1)[-1]}">'
        f"{period[0]}..{period[1]}</a>"
        for url, period in (
            (_NEWER_URL, _NEWER_WINDOW),
            (_OLDER_URL, _OLDER_WINDOW),
            (_THIRD_URL, _THIRD_WINDOW),
        )
    )
    return f"<html><body>{links}</body></html>".encode()


class _CountingTransport(HttpTransport):
    """Serve the official catalog and the three datasets while counting every URL."""

    def __init__(self) -> None:
        self.catalog_calls = 0
        self.zip_calls: dict[str, int] = {}
        self.unexpected: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        if url == SEC_13F_DATA_SETS_CATALOG_URL:
            self.catalog_calls += 1
            body = _catalog_html()
        elif url == _OLDER_URL:
            self.zip_calls[url] = self.zip_calls.get(url, 0) + 1
            body = _dataset_zip(_older_rows())
        elif url == _NEWER_URL:
            self.zip_calls[url] = self.zip_calls.get(url, 0) + 1
            body = _dataset_zip(_newer_rows())
        else:
            self.unexpected.append(url)
            raise RuntimeError(f"Unexpected URL request: {url}")
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


class _SubmissionsClient:
    """One fresh Submissions response per manager declaring both of its filings."""

    def __init__(self, *, missing: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.missing = missing

    def fetch(self, filer_cik: str) -> RawRecord:
        self.calls.append(filer_cik)
        accessions: list[tuple[str, str, str]] = []
        if filer_cik in _BOTH_MANAGERS:
            accessions.append((_older_accession(filer_cik), "2025-12-31", "2026-02-20T12:00:00Z"))
            accessions.append((_newer_accession(filer_cik), "2026-03-31", "2026-05-20T12:00:00Z"))
        elif filer_cik in {_NEWER_ONLY_MANAGER[0]}:
            accessions.append((_newer_accession(filer_cik), "2026-03-31", "2026-05-20T12:00:00Z"))
        else:
            accessions.append((_older_accession(filer_cik), "2025-12-31", "2026-02-20T12:00:00Z"))
        accessions = [item for item in accessions if item[0] not in self.missing]
        retrieved_at = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
        record_id = uuid5(
            NAMESPACE_URL,
            f"submissions|{filer_cik}|{'|'.join(item[0] for item in accessions)}",
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
                    "name": _manager_name(filer_cik),
                    "filings": {
                        "recent": {
                            "accessionNumber": [item[0] for item in accessions],
                            "filingDate": [
                                "2026-02-19" if item[1] == "2025-12-31" else "2026-05-19"
                                for item in accessions
                            ],
                            "reportDate": [item[1] for item in accessions],
                            "acceptanceDateTime": [item[2] for item in accessions],
                            "form": ["13F-HR" for _ in accessions],
                            "primaryDocument": [_PRIMARY_DOCUMENT for _ in accessions],
                        }
                    },
                }
            },
            schema_version=MANAGER_SUBMISSIONS_SCHEMA_VERSION,
        )


def _manager_name(cik: str) -> str:
    if cik in _BOTH_MANAGERS:
        return _BOTH_MANAGERS[cik][0]
    if cik == _NEWER_ONLY_MANAGER[0]:
        return _NEWER_ONLY_MANAGER[1]
    return _OLDER_ONLY_MANAGER[1]


class _DocumentClient:
    """Archives double that renders each accession with its own report period and form."""

    def __init__(self, *, reject: frozenset[str] = frozenset()) -> None:
        self.reject = reject
        self.manifest_calls = 0
        self.document_calls = 0

    @property
    def archives_calls(self) -> int:
        return self.manifest_calls + self.document_calls

    def fetch_manifest(self, document) -> SecAccessionManifest:
        self.manifest_calls += 1
        accession = document.filing.accession
        entries = (
            ("filing.htm",)
            if accession in self.reject
            else ("filing.htm", "primary_doc.xml", "infotable.xml")
        )
        return SecAccessionManifest(
            entries=entries,
            sha256="c" * 64,
            size_bytes=100,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=document.filing.accepted_at,
        )

    def fetch(self, document) -> SecPrimaryDocumentResponse:
        self.document_calls += 1
        accession = document.filing.accession
        period = document.filing.report_date
        assert period is not None
        value, shares = _reported_position(accession)
        if document.name == _PRIMARY_DOCUMENT:
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        elif document.name == "primary_doc.xml":
            content = (
                f"<edgarSubmission><submissionType>{document.filing.form}</submissionType>"
                "<filingManager><name>Manager LLC</name></filingManager>"
                f"<reportCalendarOrQuarter>{period.strftime('%m-%d-%Y')}</reportCalendarOrQuarter>"
                f"<tableEntryTotal>1</tableEntryTotal><tableValueTotal>{value}</tableValueTotal>"
                "</edgarSubmission>"
            ).encode()
        else:
            content = b"".join(
                (
                    b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>",
                    b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip>",
                    f"<value>{value}</value>".encode(),
                    f"<shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt>".encode(),
                    b"<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>",
                    b"</infoTable></informationTable>",
                )
            )
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{accession}/{document.name}",
            retrieved_at=document.filing.accepted_at,
        )


def _reported_position(accession: str) -> tuple[int, int]:
    """Report a material change between the older and the newer accession of one manager."""
    if accession.endswith("-26-000010"):
        return 100_000, 12
    return 80_000, 10


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, delta: timedelta) -> datetime:
        self.now = self.now + delta
        return self.now


def _application(
    transport: _CountingTransport,
    submissions: _SubmissionsClient,
    documents: _DocumentClient,
    clock: _Clock,
    runtime: ApplicationRuntime,
) -> SecInstitutionalHistoryApplication:
    return SecInstitutionalHistoryApplication(
        runtime,
        transport_factory=lambda: transport,
        submissions_client_factory=lambda *args, **kwargs: submissions,
        document_client_factory=lambda *args, **kwargs: documents,
        clock=lambda: clock.now,
    )


def _state_store(workspace: Path) -> SecInstitutionalHistoryStateStore:
    return SecInstitutionalHistoryStateStore(
        workspace / "state" / SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME
    )


def _counting_runtime(runtime: ApplicationRuntime, modes: list[WorkspaceAccessMode]) -> None:
    original = runtime.open_storage

    @contextmanager
    def counting(request, *, access_mode):
        modes.append(access_mode)
        with original(request, access_mode=access_mode) as storage:
            yield storage

    runtime.open_storage = counting


def _run(
    *,
    transport: _CountingTransport,
    submissions: _SubmissionsClient,
    documents: _DocumentClient,
    clock: _Clock,
    workspace: Path,
    modes: list[WorkspaceAccessMode] | None = None,
    delta: timedelta = timedelta(days=1),
) -> tuple[object, Path]:
    runtime = ApplicationRuntime.create_default()
    if modes is not None:
        _counting_runtime(runtime, modes)
    application = _application(transport, submissions, documents, clock, runtime)
    known_at = clock.advance(delta)
    outbox = workspace / "state" / "cazatiburones_notification_outbox_state_v1.json"
    summary = application.run_cycle(
        SecInstitutionalHistoryRequest(known_at=known_at),
        sec_identity=_IDENTITY,
        location=StorageLocationRequest(legacy_root=workspace),
        outbox_state=outbox,
    )
    return summary, outbox


def _prepare_window(
    tmp_path: Path,
) -> tuple[_CountingTransport, _SubmissionsClient, _DocumentClient, _Clock]:
    transport = _CountingTransport()
    submissions = _SubmissionsClient()
    documents = _DocumentClient()
    clock = _Clock(datetime(2026, 9, 11, 12, 0, tzinfo=UTC))
    return transport, submissions, documents, clock


def test_history_window_prepares_two_adjacent_datasets_and_processes_both_closes(
    tmp_path: Path,
) -> None:
    transport, submissions, documents, clock = _prepare_window(tmp_path)
    modes: list[WorkspaceAccessMode] = []
    state_path = tmp_path / "state" / SEC_INSTITUTIONAL_HISTORY_STATE_FILE_NAME

    first, outbox = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
        modes=modes,
    )

    assert first.status == "preparing"
    assert first.phase == "preparing"
    assert first.catalog_calls == 1
    assert first.zip_calls == 1
    assert transport.zip_calls == {_OLDER_URL: 1}
    assert first.submissions_calls == 0
    assert first.target is None
    assert state_path.exists()
    assert modes == [WorkspaceAccessMode.READ_WRITE]

    second, _ = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert second.status == "preparing"
    assert second.zip_calls == 1
    assert transport.zip_calls == {_OLDER_URL: 1, _NEWER_URL: 1}
    assert transport.catalog_calls == 2
    assert len(second.periods) == 2
    assert [item.role for item in second.periods] == ["older", "newer"]
    assert second.periods[0].period_start == _OLDER_WINDOW[0]
    assert second.periods[1].period_start == _NEWER_WINDOW[0]
    assert second.periods[0].dataset_url == _OLDER_URL
    assert second.periods[1].dataset_url == _NEWER_URL
    assert second.periods[0].zip_downloaded is False
    assert second.periods[0].snapshot_reused is True
    assert second.periods[1].zip_downloaded is True
    assert submissions.calls == []

    third, _ = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert third.status == "processed"
    assert third.zip_calls == 0
    assert third.catalog_calls == 1
    assert third.periods[0].snapshot_reused is True
    assert third.periods[1].snapshot_reused is True
    assert third.common_manager_count == 2
    assert third.excluded_manager_count == 2
    assert third.non_comparable_manager_count == 0
    assert third.total_targets == 2
    assert third.target_cursor_before == 0
    assert third.target_cursor_after == 1
    assert third.manager_cik == "0000000002"
    assert third.submissions_calls == 1
    target = third.target
    assert target is not None and target.state == "processed"
    assert target.older_report_period == _OLDER_PERIOD
    assert target.newer_report_period == _NEWER_PERIOD
    assert target.rejected_accessions == ()
    assert target.created_accessions == (
        _older_accession("0000000002"),
        _newer_accession("0000000002"),
    )
    assert target.older_observations_created >= 1
    assert target.newer_observations_created >= 1
    assert target.metrics_created + target.metrics_reused >= 1
    assert target.weights_created + target.weights_reused >= 1
    assert target.events_created >= 1
    assert third.notifications_created >= 1
    assert target.traceability_verified
    assert submissions.calls == ["0000000002"]

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        observations = InstitutionalObservationService(storage).query(
            InstitutionalObservationQuery(
                asset_id="equity:us:aapl", known_at=third.effective_known_at
            )
        )
        periods = {view.report.report_period for view in observations.observations}
        assert periods == {_OLDER_PERIOD, _NEWER_PERIOD}
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 2
        )
    assert CazatiburonesNotificationStore(outbox).load().items

    fourth, _ = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert fourth.status == "completed"
    assert fourth.phase == "completed"
    assert fourth.target_cursor_before == 1
    assert fourth.target_cursor_after == 2
    assert fourth.coverage_complete is True
    assert fourth.manager_cik == "0001067983"
    assert submissions.calls == ["0000000002", "0001067983"]
    initial_items = CazatiburonesNotificationStore(outbox).load().items

    fifth, _ = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert fifth.status == "completed"
    assert fifth.target is None
    assert fifth.zip_calls == 0
    assert fifth.submissions_calls == 0
    assert fifth.notifications_created == 0
    assert transport.zip_calls == {_OLDER_URL: 1, _NEWER_URL: 1}
    assert transport.unexpected == []
    persisted = _state_store(tmp_path).load()
    assert persisted.target_cursor == 2
    assert persisted.total_targets == 2
    assert persisted.cycle_count == 2
    repeated_items = CazatiburonesNotificationStore(outbox).load().items
    assert [item.notification_id for item in repeated_items] == [
        item.notification_id for item in initial_items
    ]


def test_history_window_records_terminal_rejection_without_synthetic_event(
    tmp_path: Path,
) -> None:
    transport, submissions, _, clock = _prepare_window(tmp_path)
    documents = _DocumentClient()
    _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )
    _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    rejecting_documents = _DocumentClient(reject=frozenset({_newer_accession("0000000002")}))
    rejected, _ = _run(
        transport=transport,
        submissions=submissions,
        documents=rejecting_documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert rejected.status == "processed"
    assert rejected.target is not None
    assert rejected.target.rejected_accessions == (_newer_accession("0000000002"),)
    assert rejected.target.created_accessions == (_older_accession("0000000002"),)
    assert rejected.target.events_created == 0
    assert rejected.target.event_candidates == 0
    assert rejected.target.metrics_created + rejected.target.metrics_reused == 0
    assert rejected.notifications_created == 0
    assert rejected.target_cursor_before == 0
    assert rejected.target_cursor_after == 1

    persisted = _state_store(tmp_path).load()
    assert persisted.target_cursor == 1
    assert persisted.last_status == "success"
    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 1
        )
        observations = InstitutionalObservationService(storage).query(
            InstitutionalObservationQuery(
                asset_id="equity:us:aapl", known_at=rejected.effective_known_at
            )
        )
        assert {view.report.report_period for view in observations.observations} == {_OLDER_PERIOD}


class _FailingSubmissionsClient(_SubmissionsClient):
    """Raise for one manager to prove cursor preservation on a bounded acquisition failure."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    def fetch(self, filer_cik: str) -> RawRecord:
        if filer_cik == self.failing:
            self.calls.append(filer_cik)
            raise RuntimeError("SEC Submissions is unavailable for this manager")
        return super().fetch(filer_cik)


def test_history_window_preserves_cursor_and_progress_on_failed_target(
    tmp_path: Path,
) -> None:
    transport, submissions, documents, clock = _prepare_window(tmp_path)
    _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )
    _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    failing = _FailingSubmissionsClient("0000000002")
    failed, _ = _run(
        transport=transport,
        submissions=failing,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert failed.status == "failed"
    assert failed.reason_code is not None
    assert failed.target is not None
    assert failed.target.state == "failed"
    assert failed.manager_cik == "0000000002"
    assert failed.target_cursor_before == 0
    assert failed.target_cursor_after == 0
    persisted = _state_store(tmp_path).load()
    assert persisted.target_cursor == 0
    assert persisted.last_status == "failed"

    recovered, _ = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )

    assert recovered.status == "processed"
    assert recovered.target is not None
    assert recovered.target.manager_cik == "0000000002"
    assert recovered.target.created_accessions == (
        _older_accession("0000000002"),
        _newer_accession("0000000002"),
    )
    assert recovered.target_cursor_before == 0
    assert recovered.target_cursor_after == 1
    assert recovered.target.events_created >= 1
    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        reports = storage.raw_records.count(
            schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION
        )
        assert reports == 2
