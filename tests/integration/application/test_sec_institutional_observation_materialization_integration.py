"""Integration tests for directed institutional-observation materialization.

The chain is real: an official universe archive is materialized through the integrated application,
one manager page is acquired and enriched by ``SEC-CORPUS-28``, and ``SEC-CORPUS-29`` proves the
row-scoped correspondence and completes the observation layer without any network call.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_institutional_holdings_refresh import (
    SecInstitutionalHoldingsDirectedRefreshApplication,
    plan_directed_manager_page,
)
from investment_analyst.application.sec_institutional_holdings_refresh_models import (
    SecInstitutionalHoldingsDirectedRefreshRequest,
)
from investment_analyst.application.sec_institutional_observation_materialization import (
    MISSING_UNIVERSE_INSTRUCTION,
    SecInstitutionalObservationMaterializationApplication,
    SecInstitutionalObservationMaterializationError,
    plan_materialization_page,
)
from investment_analyst.application.sec_institutional_observation_materialization_models import (
    SecInstitutionalObservationMaterializationRequest,
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
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import (
    SOURCE_ID as OBSERVATION_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpResponse, HttpTransport, UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    SEC_13F_DATA_SETS_CATALOG_URL,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_PERIOD = date(2026, 3, 31)
_PERIOD_XML = "03-31-2026"
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
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers,
        timeout_seconds: float,
        max_response_bytes: int | None = None,
    ) -> HttpResponse:
        self.calls.append(url)
        if url == SEC_13F_DATA_SETS_CATALOG_URL:
            body = f'<html><a href="{_ZIP_URL}">2026 Q1</a></html>'.encode()
        elif url == _ZIP_URL:
            body = self.zip_bytes
        else:
            raise AssertionError(f"unexpected SEC request: {url}")
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
    def __init__(self, *, extra_rows: int = 0) -> None:
        self.extra_rows = extra_rows
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
        if document.name == _PRIMARY_DOCUMENT:
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        elif document.name == "primary_doc.xml":
            content = (
                f"<edgarSubmission><submissionType>{document.filing.form}</submissionType>"
                "<filingManager><name>Manager LLC</name></filingManager>"
                f"<reportCalendarOrQuarter>{_PERIOD_XML}</reportCalendarOrQuarter>"
                "<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal>"
                "</edgarSubmission>"
            ).encode()
        else:
            rows = [
                b"<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
                b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
                b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
                b"</shrsOrPrnAmt></infoTable>"
            ]
            rows.extend(
                (
                    b"<infoTable><nameOfIssuer>OTHER ISSUER</nameOfIssuer>"
                    b"<titleOfClass>COM</titleOfClass><cusip>594918104</cusip><value>7</value>"
                    b"<shrsOrPrnAmt><sshPrnamt>1</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>"
                    b"</shrsOrPrnAmt></infoTable>"
                )
                for _ in range(self.extra_rows)
            )
            content = b"<informationTable>" + b"".join(rows) + b"</informationTable>"
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


def _acquire(
    location: StorageLocationRequest,
    submissions: _ManagerSubmissionsClient,
    documents: _DocumentClient,
    *,
    known_at: datetime,
    manager_limit: int = 3,
):
    return SecInstitutionalHoldingsDirectedRefreshApplication(
        ApplicationRuntime.create_default(),
        submissions_client_factory=lambda *args: submissions,
        document_client_factory=lambda *args: documents,
    ).refresh(
        SecInstitutionalHoldingsDirectedRefreshRequest(
            known_at=known_at, manager_limit=manager_limit, accessions_per_manager=1
        ),
        sec_identity=_IDENTITY,
        location=location,
    )


def _filings(accepted_at: datetime) -> dict[str, tuple[dict[str, str], ...]]:
    return {cik: (_filing(f"{cik}-26-000010", accepted_at=accepted_at),) for cik in _MANAGERS}


def _prepared(tmp_path: Path, *, extra_rows: int = 0, manager_limit: int = 3):
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    submissions = _ManagerSubmissionsClient(_filings(accepted_at))
    documents = _DocumentClient(extra_rows=extra_rows)
    acquisition = _acquire(
        location, submissions, documents, known_at=known_at, manager_limit=manager_limit
    )
    return location, universe, known_at, submissions, documents, acquisition


def test_materialization_proves_claims_and_observations_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, universe, known_at, _, documents, acquisition = _prepared(tmp_path)
    archives_after_acquisition = documents.archives_calls

    def forbidden_get(self, url, **kwargs):
        raise AssertionError(f"materialization must not perform network calls: {url}")

    monkeypatch.setattr(UrlLibHttpTransport, "get", forbidden_get)

    application = SecInstitutionalObservationMaterializationApplication(
        ApplicationRuntime.create_default()
    )
    request = SecInstitutionalObservationMaterializationRequest(known_at=known_at, manager_limit=3)
    first = application.materialize(request, location=location)

    assert first.snapshot_id == universe.snapshot_id
    assert first.dataset_revision_id == universe.revision_id
    assert first.page_manager_count == 3
    assert first.candidate_count == 3
    assert first.claims_created == 3
    assert first.claims_reused == 0
    assert first.claims_ambiguous == 0
    assert first.observations_created == 6
    assert first.failed_candidates == 0 and first.failed_runs == 0
    assert first.traceability_verified is True
    assert documents.archives_calls == archives_after_acquisition

    for candidate in first.candidates:
        manager = next(
            item for item in acquisition.managers if item.manager_cik == candidate.manager_cik
        )
        assert candidate.candidate_id in manager.candidate_ids
        assert candidate.asset_id == "equity:us:aapl"
        assert candidate.cusip == "037833100"
        assert candidate.report_period == _PERIOD
        assert candidate.state == "processed"
        assert candidate.rows_examined == 1
        assert candidate.rows_matched == 1
        assert candidate.rows_unmatched == 0
        assert len(candidate.claim_ids) == 1
        assert candidate.report_ids == manager.report_ids
    assert {item.asset_id for item in first.runs} == {"equity:us:aapl"}
    assert sum(item.rows_linked for item in first.runs) == 3

    second = application.materialize(request, location=location)
    assert second.claims_created == 0
    assert second.claims_reused == 3
    assert second.observations_created == 0
    assert second.observations_reused == 6
    assert second.traceability_verified is True
    assert [item.claim_ids for item in second.candidates] == [
        item.claim_ids for item in first.candidates
    ]
    assert [item.observations_reused for item in second.runs] == [2, 2, 2]

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        assert storage.raw_records.count(schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION) == 3
        observations = storage.observations.list(
            asset_id="equity:us:aapl", source_id=OBSERVATION_SOURCE_ID
        )
        assert len(observations) == 6
        assert {
            json.loads(item.source.record_key)["correspondence_id"] for item in observations
        } == {str(value) for item in first.candidates for value in item.claim_ids}
        assert {item.field_name for item in observations} == {
            "institutional_reported_fair_value",
            "institutional_reported_shares",
        }


def test_page_selection_matches_the_acquired_page_and_preserves_candidate_tuples(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        snapshot = SecInstitutionalUniverseRepository(
            storage.raw_records, storage.documents
        ).get_snapshot(universe.snapshot_id)

    directed = plan_directed_manager_page(snapshot, offset=0, limit=25)
    planned = plan_materialization_page(snapshot, offset=0, limit=25)
    assert [(item.manager_cik, item.report_period) for item in planned] == [
        (item.manager_cik, item.report_period) for item in directed
    ]
    assert all(len(item.candidates) >= 1 for item in planned)
    for target in planned:
        for candidate in target.candidates:
            assert candidate.manager_cik == target.manager_cik
            assert candidate.report_period == target.report_period
            assert candidate.asset_id == "equity:us:aapl"
    first = plan_materialization_page(snapshot, offset=0, limit=2)
    second = plan_materialization_page(snapshot, offset=2, limit=2)
    assert [item.manager_cik for item in first + second] == [
        item.manager_cik for item in planned[:4]
    ]
    assert plan_materialization_page(snapshot, offset=3, limit=5) == ()
    with pytest.raises(SecInstitutionalObservationMaterializationError):
        plan_materialization_page(snapshot, offset=-1, limit=1)
    with pytest.raises(SecInstitutionalObservationMaterializationError):
        plan_materialization_page(snapshot, offset=0, limit=0)


def test_materialization_reports_explicit_states_and_recovers_partial_progress(
    tmp_path: Path,
) -> None:
    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    accepted_at = universe.available_at - timedelta(days=1)
    known_at = universe.available_at + timedelta(days=1)
    submissions = _ManagerSubmissionsClient(_filings(accepted_at))
    submissions.fail_for = {"0000000002"}
    documents = _DocumentClient()
    acquisition = _acquire(location, submissions, documents, known_at=known_at, manager_limit=3)
    assert [item.state for item in acquisition.managers] == ["processed", "failed", "processed"]
    assert acquisition.managers[1].created_accessions == ()

    application = SecInstitutionalObservationMaterializationApplication(
        ApplicationRuntime.create_default()
    )
    request = SecInstitutionalObservationMaterializationRequest(known_at=known_at, manager_limit=3)

    partial = application.materialize(request, location=location)
    assert partial.page_manager_count == 3
    states = {item.manager_cik: item.state for item in partial.candidates}
    assert states == {
        "0001067983": "processed",
        "0000000002": "missing_report",
        "0000000003": "processed",
    }
    assert partial.claims_created == 2
    assert partial.claims_reused == 0
    assert partial.observations_created == 4
    assert partial.failed_candidates == 0
    assert partial.traceability_verified is True
    assert sum(len(item.claim_ids) for item in partial.candidates) == 2
    pending = next(item for item in partial.candidates if item.manager_cik == "0000000002")
    assert pending.report_ids == () and pending.claim_ids == ()
    assert pending.rows_examined == 0 and pending.artifact_ids == ()

    submissions.fail_for = set()
    recovered_acquisition = _acquire(
        location, submissions, documents, known_at=known_at, manager_limit=3
    )
    assert recovered_acquisition.created == 1
    recovered = application.materialize(request, location=location)
    assert recovered.failed_candidates == 0
    assert recovered.claims_created == 1
    assert recovered.claims_reused == 2
    assert recovered.observations_created == 2
    assert recovered.observations_reused == 4
    assert recovered.traceability_verified is True
    assert {item.manager_cik: item.state for item in recovered.candidates} == {
        "0001067983": "processed",
        "0000000002": "processed",
        "0000000003": "processed",
    }


def test_materialization_fails_closed_without_a_visible_snapshot(tmp_path: Path) -> None:
    application = SecInstitutionalObservationMaterializationApplication(
        ApplicationRuntime.create_default()
    )
    early = SecInstitutionalObservationMaterializationRequest(
        known_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(
        SecInstitutionalObservationMaterializationError, match="manager universe snapshot"
    ):
        application.materialize(early, location=StorageLocationRequest(legacy_root=tmp_path))
    assert MISSING_UNIVERSE_INSTRUCTION.endswith(
        "run scripts/refresh_sec_institutional_manager_universe.py first"
    )

    location = StorageLocationRequest(legacy_root=tmp_path)
    universe = _materialize_universe(location)
    with pytest.raises(SecInstitutionalObservationMaterializationError):
        application.materialize(
            SecInstitutionalObservationMaterializationRequest(
                known_at=universe.available_at - timedelta(seconds=1)
            ),
            location=location,
        )
