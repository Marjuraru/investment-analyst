"""Integration tests for the scheduled Form 13F cycle application service."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_institutional_cycle import (
    SecInstitutionalCycleApplication,
)
from investment_analyst.application.sec_institutional_cycle_models import (
    SecInstitutionalCycleRequest,
)
from investment_analyst.application.sec_institutional_cycle_state import (
    SecInstitutionalCycleStateStore,
)
from investment_analyst.core.models import RawRecord, SourceReference
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

_PERIOD = date(2026, 3, 31)
_ZIP_URL = (
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    "01mar2026-31may2026_form13f.zip"
)
_ZIP_URL_NEW = (
    "https://www.sec.gov/files/structureddata/data/form-13f-data-sets/"
    "01jun2026-31aug2026_form13f.zip"
)
_IDENTITY = SecEdgarIdentity("Analyst user@example.com")
_MANAGERS = {
    "0001067983": ("BERKSHIRE HATHAWAY INC", "80000"),
    "0000000002": ("ALPHA ASSET MANAGEMENT", "50000"),
}
_PRIMARY_DOCUMENT = "xslForm13F_X02/primary_doc.xml"


def _universe_zip(period_str: str = "31-MAR-2026") -> bytes:
    submissions = ["ACCESSION_NUMBER\tFILING_DATE\tSUBMISSIONTYPE\tCIK\tPERIODOFREPORT"]
    coverpage = ["ACCESSION_NUMBER\tFILINGMANAGER_NAME\tISAMENDMENT"]
    infotable = ["ACCESSION_NUMBER\tCUSIP\tVALUE"]
    for cik, (name, value) in _MANAGERS.items():
        accession = f"{cik}-26-000010"
        submissions.append(f"{accession}\t15-APR-2026\t13F-HR\t{cik}\t{period_str}")
        coverpage.append(f"{accession}\t{name}\tN")
        infotable.append(f"{accession}\t037833100\t{value}")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("SUBMISSION.tsv", "\n".join(submissions) + "\n")
        archive.writestr("COVERPAGE.tsv", "\n".join(coverpage) + "\n")
        archive.writestr("INFOTABLE.tsv", "\n".join(infotable) + "\n")
    return stream.getvalue()


class _MockHttpTransport(HttpTransport):
    def __init__(self, zip_bytes: bytes, catalog_zip_url: str = _ZIP_URL) -> None:
        self.zip_bytes = zip_bytes
        self.catalog_zip_url = catalog_zip_url
        self.catalog_calls = 0
        self.zip_calls = 0

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
            body = f'<html><a href="{self.catalog_zip_url}">2026 Q1</a></html>'.encode()
        elif url in (_ZIP_URL, _ZIP_URL_NEW):
            self.zip_calls += 1
            body = self.zip_bytes
        else:
            raise RuntimeError(f"Unexpected URL request: {url}")
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


class _MockSubmissionsClient:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def fetch(self, filer_cik: str) -> RawRecord:
        self.calls[filer_cik] = self.calls.get(filer_cik, 0) + 1
        captured = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        accession = f"{filer_cik}-26-000010"
        record_id = uuid5(NAMESPACE_URL, f"submissions|{filer_cik}|{accession}")
        return RawRecord(
            record_id=record_id,
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
                    "name": _MANAGERS[filer_cik][0],
                    "filings": {
                        "recent": {
                            "accessionNumber": [accession],
                            "filingDate": ["2026-04-15"],
                            "reportDate": ["2026-03-31"],
                            "acceptanceDateTime": ["2026-04-15T12:00:00Z"],
                            "form": ["13F-HR"],
                            "primaryDocument": [_PRIMARY_DOCUMENT],
                        }
                    },
                }
            },
            schema_version=MANAGER_SUBMISSIONS_SCHEMA_VERSION,
        )


class _MockDocumentClient:
    def __init__(self, *, fail_accession: str | None = None) -> None:
        self.fail_accession = fail_accession
        self.archives_calls = 0

    def fetch_manifest(self, document) -> SecAccessionManifest:
        self.archives_calls += 1
        return SecAccessionManifest(
            entries=("primary_doc.xml", "infotable.xml"),
            sha256="c" * 64,
            size_bytes=100,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
        )

    def fetch(self, document) -> SecPrimaryDocumentResponse:
        self.archives_calls += 1
        accession = document.filing.accession
        if self.fail_accession == accession:
            raise RuntimeError(f"Simulated SEC Archives failure for {accession}")
        retrieved_at = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        if document.name == _PRIMARY_DOCUMENT:
            content = b"""<edgarSubmission><headerData><submissionInfo>
<periodOfReport>03-31-2026</periodOfReport></submissionInfo></headerData>
<formData><coverPage><reportCalendarOrQuarter>03-31-2026</reportCalendarOrQuarter>
<filingManager><name>Manager</name></filingManager></coverPage></formData>
</edgarSubmission>"""
        elif document.name == "primary_doc.xml":
            content = b"""<edgarSubmission><submissionType>13F-HR</submissionType>
<reportCalendarOrQuarter>03-31-2026</reportCalendarOrQuarter>
<filingManager><name>Manager</name></filingManager>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>80000</tableValueTotal></edgarSubmission>"""
        else:
            content = b"""<informationTable><infoTable>
<nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>037833100</cusip><value>80000</value>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority>
<Sole>100</Sole><Shared>0</Shared><None>0</None></votingAuthority>
</infoTable></informationTable>"""
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{accession}/{document.name}",
            retrieved_at=retrieved_at,
        )


def test_cycle_first_run_acquires_universe_and_materializes_first_manager() -> None:
    zip_data = _universe_zip()
    transport = _MockHttpTransport(zip_data)
    subs_client = _MockSubmissionsClient()
    doc_client = _MockDocumentClient()

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = temp_path / "state"
        location = StorageLocationRequest(legacy_root=workspace)

        runtime = ApplicationRuntime.create_default()
        state_store = SecInstitutionalCycleStateStore(
            state_root / "sec_institutional_cycle_state_v1.json"
        )
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        app = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: doc_client,
            state_store=state_store,
            clock=lambda: now,
        )

        summary = app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now),
            sec_identity=_IDENTITY,
            location=location,
        )

        assert summary.status == "processed"
        assert summary.catalog_calls == 1
        assert summary.zip_calls == 1
        assert summary.submissions_calls == 1
        assert summary.archives_calls > 0
        assert summary.manager_cursor_before == 0
        assert summary.manager_cursor_after == 1
        assert summary.total_managers == 2
        assert not summary.coverage_complete
        assert summary.observations_created >= 1
        assert summary.traceability_verified

        # State file verification
        persisted_state = state_store.load()
        assert persisted_state.manager_cursor == 1
        assert persisted_state.total_managers == 2
        assert persisted_state.last_status == "success"
        assert persisted_state.snapshot_id == summary.snapshot_id

        # Verify AAPL observation in storage
        with LocalStorage(StoragePaths.from_root(workspace), read_only=True) as storage:
            obs_service = InstitutionalObservationService(storage)
            obs_result = obs_service.query(
                InstitutionalObservationQuery(asset_id="equity:us:aapl", known_at=now)
            )
            assert len(obs_result.observations) >= 1
            assert obs_result.observations[0].row.cusip == "037833100"


def test_cycle_second_run_reuses_zip_and_advances_to_next_manager() -> None:
    zip_data = _universe_zip()
    transport = _MockHttpTransport(zip_data)
    subs_client = _MockSubmissionsClient()
    doc_client = _MockDocumentClient()

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = temp_path / "state"
        location = StorageLocationRequest(legacy_root=workspace)

        runtime = ApplicationRuntime.create_default()
        state_store = SecInstitutionalCycleStateStore(
            state_root / "sec_institutional_cycle_state_v1.json"
        )
        now1 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        app = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: doc_client,
            state_store=state_store,
            clock=lambda: now1,
        )

        app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now1),
            sec_identity=_IDENTITY,
            location=location,
        )

        # Recreate objects (restart simulation)
        runtime_2 = ApplicationRuntime.create_default()
        state_store_2 = SecInstitutionalCycleStateStore(
            state_root / "sec_institutional_cycle_state_v1.json"
        )
        now2 = now1 + timedelta(days=1)
        app_2 = SecInstitutionalCycleApplication(
            runtime_2,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: doc_client,
            state_store=state_store_2,
            clock=lambda: now2,
        )

        # Run 2: 1 day later (cache hit under 7 days)
        summary2 = app_2.run_cycle(
            SecInstitutionalCycleRequest(known_at=now2),
            sec_identity=_IDENTITY,
            location=location,
        )

        assert summary2.status == "processed"
        assert summary2.catalog_calls == 1
        assert summary2.zip_calls == 0  # Zero GET of ZIP archive!
        assert summary2.manager_cursor_before == 1
        assert summary2.manager_cursor_after == 2
        assert summary2.coverage_complete  # 2 of 2 processed!

        persisted_state = state_store_2.load()
        assert persisted_state.manager_cursor == 2
        assert persisted_state.total_managers == 2


def test_cycle_forces_zip_refresh_after_seven_days() -> None:
    zip_data = _universe_zip()
    transport = _MockHttpTransport(zip_data)
    subs_client = _MockSubmissionsClient()
    doc_client = _MockDocumentClient()

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = temp_path / "state"
        location = StorageLocationRequest(legacy_root=workspace)

        runtime = ApplicationRuntime.create_default()
        state_store = SecInstitutionalCycleStateStore(
            state_root / "sec_institutional_cycle_state_v1.json"
        )
        now1 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        simulated_clock = [now1]
        app = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: doc_client,
            state_store=state_store,
            clock=lambda: simulated_clock[0],
        )

        app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now1),
            sec_identity=_IDENTITY,
            location=location,
        )

        # 8 days later -> cache expired, forces ZIP download
        now2 = now1 + timedelta(days=8)
        simulated_clock[0] = now2
        summary2 = app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now2),
            sec_identity=_IDENTITY,
            location=location,
        )

        assert summary2.catalog_calls == 1
        assert summary2.zip_calls == 1  # ZIP re-downloaded after 7 days!
        assert summary2.manager_cursor_before == 1
        assert summary2.manager_cursor_after == 2


def test_cycle_failure_preserves_cursor_and_repeats_page() -> None:
    zip_data = _universe_zip()
    transport = _MockHttpTransport(zip_data)
    subs_client = _MockSubmissionsClient()

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = temp_path / "state"
        location = StorageLocationRequest(legacy_root=workspace)

        runtime = ApplicationRuntime.create_default()
        state_store = SecInstitutionalCycleStateStore(
            state_root / "sec_institutional_cycle_state_v1.json"
        )

        # Failing document client on first manager
        failing_doc_client = _MockDocumentClient(fail_accession="0001067983-26-000010")
        now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        app = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: failing_doc_client,
            state_store=state_store,
            clock=lambda: now,
        )

        summary = app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now),
            sec_identity=_IDENTITY,
            location=location,
        )

        assert summary.status == "failed"
        assert summary.manager_cursor_before == 0
        assert summary.manager_cursor_after == 0  # Did not advance!

        # State recorded failure without advancing cursor
        state = state_store.load()
        assert state.manager_cursor == 0
        assert state.last_status == "failed"

        # Now fix document client and re-run -> repeats manager 0 successfully!
        now_fixed = now + timedelta(minutes=5)
        app_fixed = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: _MockDocumentClient(),
            state_store=state_store,
            clock=lambda: now_fixed,
        )
        summary_fixed = app_fixed.run_cycle(
            SecInstitutionalCycleRequest(known_at=now_fixed),
            sec_identity=_IDENTITY,
            location=location,
        )
        assert summary_fixed.status == "processed"
        assert summary_fixed.manager_cursor_before == 0
        assert summary_fixed.manager_cursor_after == 1
        assert state_store.load().manager_cursor == 1


def test_cycle_new_period_or_url_forces_zip_download_and_resets_cursor() -> None:
    zip_data = _universe_zip(period_str="31-MAR-2026")
    zip_data_new = _universe_zip(period_str="30-JUN-2026")
    transport = _MockHttpTransport(zip_data, catalog_zip_url=_ZIP_URL)
    subs_client = _MockSubmissionsClient()
    doc_client = _MockDocumentClient()

    with TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        workspace = temp_path / "workspace"
        state_root = temp_path / "state"
        location = StorageLocationRequest(legacy_root=workspace)

        runtime = ApplicationRuntime.create_default()
        state_store = SecInstitutionalCycleStateStore(
            state_root / "sec_institutional_cycle_state_v1.json"
        )
        now1 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        app = SecInstitutionalCycleApplication(
            runtime,
            transport_factory=lambda: transport,
            submissions_client_factory=lambda *a, **k: subs_client,
            document_client_factory=lambda *a, **k: doc_client,
            state_store=state_store,
            clock=lambda: now1,
        )

        # First run: acquires _ZIP_URL, processes first manager (cursor 0 -> 1)
        summary1 = app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now1),
            sec_identity=_IDENTITY,
            location=location,
        )
        assert summary1.status == "processed"
        assert summary1.zip_calls == 1
        assert summary1.manager_cursor_before == 0
        assert summary1.manager_cursor_after == 1
        assert state_store.load().manager_cursor == 1

        # Now simulate catalog update to a new quarter / dataset URL: _ZIP_URL_NEW
        transport.catalog_zip_url = _ZIP_URL_NEW
        transport.zip_bytes = zip_data_new
        now2 = now1 + timedelta(days=1)  # under 7 days, but new URL/period forces zip download!

        summary2 = app.run_cycle(
            SecInstitutionalCycleRequest(known_at=now2),
            sec_identity=_IDENTITY,
            location=location,
        )

        # Acceptance 2: new period / URL forces ZIP download and resets manager cursor to 0
        assert summary2.status == "processed"
        assert summary2.catalog_calls == 1
        assert summary2.zip_calls == 1  # ZIP downloaded for new URL despite cache < 7 days
        assert summary2.manager_cursor_before == 0  # Reset to 0 for new snapshot
        assert summary2.manager_cursor_after == 1  # Advanced from 0 to 1
        assert summary2.dataset_url == _ZIP_URL_NEW

        persisted = state_store.load()
        assert persisted.manager_cursor == 1
        assert persisted.dataset_url == _ZIP_URL_NEW
        assert persisted.snapshot_id == summary2.snapshot_id
