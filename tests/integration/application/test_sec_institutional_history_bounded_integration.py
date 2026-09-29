"""Bounded scratch integration for the two-close 13F history window.

Covers the complete scratch integration of both closes and the next target:
compares semantic JSON before/after per manager/asset/cut, counts hydrated
documents, and proves hydration scales with the selected target rather than
with foreign managers or market metrics. Times and RSS are orientative
measurements without an absolute CI threshold. Corrupt or foreign selected
evidence fails closed.
"""

from __future__ import annotations

import hashlib
import io
import json
import resource
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.application.sec_institutional_history import (
    SecInstitutionalHistoryApplication,
)
from investment_analyst.application.sec_institutional_history_models import (
    SecInstitutionalHistoryRequest,
)
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult
from investment_analyst.evidence.sec_institutional_correspondence.repository import (
    SecInstitutionalRowCorrespondenceRepository,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
)
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    InstitutionalSemanticsRepository,
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
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

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

_BOTH_MANAGERS = {
    "0000000002": ("ALPHA ASSET MANAGEMENT", Decimal("50000"), Decimal("60000")),
    "0001067983": ("BERKSHIRE HATHAWAY INC", Decimal("80000"), Decimal("100000")),
}
_NEWER_ONLY_MANAGER = "0000000003", "BETA PARTNERS LP", Decimal("10000")
_OLDER_ONLY_MANAGER = "0000000004", "GAMMA CAPITAL LLC", Decimal("20000")
_DENSE_FOREIGN_MANAGERS = tuple(f"{1000000005 + index:010d}" for index in range(5))


def _older_accession(cik: str) -> str:
    return f"{cik}-26-000001"


def _newer_accession(cik: str) -> str:
    return f"{cik}-26-000010"


def _dataset_zip(rows: tuple[tuple[str, str, str, Decimal], ...]) -> bytes:
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
    for index, foreign_cik in enumerate(_DENSE_FOREIGN_MANAGERS):
        rows.append(
            (_older_accession(foreign_cik), foreign_cik, f"DENSE MANAGER {index}", Decimal("7000"))
        )
    return tuple(rows)


def _newer_rows() -> tuple[tuple[str, str, str, Decimal], ...]:
    rows = [
        (_newer_accession(cik), cik, name, newer)
        for cik, (name, _, newer) in _BOTH_MANAGERS.items()
    ]
    cik, name, value = _NEWER_ONLY_MANAGER
    rows.append((_newer_accession(cik), cik, name, value))
    for index, foreign_cik in enumerate(_DENSE_FOREIGN_MANAGERS):
        rows.append(
            (_newer_accession(foreign_cik), foreign_cik, f"DENSE MANAGER {index}", Decimal("8000"))
        )
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
    def __init__(self) -> None:
        self.catalog_calls = 0
        self.zip_calls: dict[str, int] = {}

    def get(
        self, url: str, *, headers, timeout_seconds: float, max_response_bytes=None
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
            raise RuntimeError(f"Unexpected URL request: {url}")
        return HttpResponse(status_code=200, body=body, headers={}, url=url)


def _manager_name(cik: str) -> str:
    if cik in _BOTH_MANAGERS:
        return _BOTH_MANAGERS[cik][0]
    if cik == _NEWER_ONLY_MANAGER[0]:
        return _NEWER_ONLY_MANAGER[1]
    if cik == _OLDER_ONLY_MANAGER[0]:
        return _OLDER_ONLY_MANAGER[1]
    return f"DENSE MANAGER {_DENSE_FOREIGN_MANAGERS.index(cik)}"


class _SubmissionsClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, filer_cik: str) -> RawRecord:
        self.calls.append(filer_cik)
        accessions: list[tuple[str, str, str]] = []
        if filer_cik in _BOTH_MANAGERS or filer_cik in _DENSE_FOREIGN_MANAGERS:
            accessions.append((_older_accession(filer_cik), "2025-12-31", "2026-02-20T12:00:00Z"))
            accessions.append((_newer_accession(filer_cik), "2026-03-31", "2026-05-20T12:00:00Z"))
        elif filer_cik in {_NEWER_ONLY_MANAGER[0]}:
            accessions.append((_newer_accession(filer_cik), "2026-03-31", "2026-05-20T12:00:00Z"))
        else:
            accessions.append((_older_accession(filer_cik), "2025-12-31", "2026-02-20T12:00:00Z"))
        retrieved_at = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
        record_id = uuid5(
            NAMESPACE_URL, f"submissions|{filer_cik}|{'|'.join(item[0] for item in accessions)}"
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


class _DocumentClient:
    def __init__(self) -> None:
        self.manifest_calls = 0
        self.document_calls = 0

    def fetch_manifest(self, document) -> SecAccessionManifest:
        self.manifest_calls += 1
        return SecAccessionManifest(
            entries=("filing.htm", "primary_doc.xml", "infotable.xml"),
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
        value, shares = (100_000, 12) if accession.endswith("-26-000010") else (80_000, 10)
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


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, delta: timedelta) -> datetime:
        self.now = self.now + delta
        return self.now


def _application(
    transport, submissions, documents, clock, runtime
) -> SecInstitutionalHistoryApplication:
    return SecInstitutionalHistoryApplication(
        runtime,
        transport_factory=lambda: transport,
        submissions_client_factory=lambda *args, **kwargs: submissions,
        document_client_factory=lambda *args, **kwargs: documents,
        clock=lambda: clock.now,
    )


def _run(
    *,
    transport,
    submissions,
    documents,
    clock,
    workspace: Path,
    delta: timedelta = timedelta(days=1),
):
    runtime = ApplicationRuntime.create_default()
    application = _application(transport, submissions, documents, clock, runtime)
    known_at = clock.advance(delta)
    outbox = workspace / "state" / "cazatiburones_notification_outbox_state_v1.json"
    summary = application.run_cycle(
        SecInstitutionalHistoryRequest(known_at=known_at),
        sec_identity=_IDENTITY,
        location=StorageLocationRequest(legacy_root=workspace),
        outbox_state=outbox,
    )
    return summary


def _seed_dense_market_metrics(workspace: Path, *, count: int = 2000) -> list:
    from investment_analyst.storage import LocalStorage, StoragePaths

    created = []
    with LocalStorage(StoragePaths.from_root(workspace)) as storage:
        for index in range(count):
            metric = MetricResult(
                result_id=uuid4(),
                asset_id="equity:us:aapl",
                metric_key="market.close_copy",
                value=Decimal("210.50"),
                unit="USD",
                as_of=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
                available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
                computed_at=datetime(2026, 7, 10, 16, 5, tzinfo=UTC),
                parameters={"window": 1, "sequence": index},
                input_observation_ids=[uuid4()],
                algorithm_version="1.0.0",
                quality=DataQuality.VALID,
            )
            storage.metric_results.save(metric)
            created.append(metric.result_id)
    return created


def _canonical_metric_json(storage: LocalStorage, *, asset_id: str) -> str:
    results = sorted(
        storage.metric_results.list(asset_id=asset_id),
        key=lambda item: str(item.result_id),
    )
    return json.dumps([item.model_dump(mode="json") for item in results], sort_keys=True)


def test_two_close_history_preserves_pit_and_scales_with_target(tmp_path: Path) -> None:
    transport = _CountingTransport()
    submissions = _SubmissionsClient()
    documents = _DocumentClient()
    clock = _Clock(datetime(2026, 9, 11, 12, 0, tzinfo=UTC))

    first = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )
    assert first.status == "preparing"
    second = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )
    assert second.status == "preparing"

    market_ids = set(_seed_dense_market_metrics(tmp_path, count=2000))

    hydrated_raw: list[tuple] = []
    hydrated_observations: list[tuple] = []
    hydrated_metrics: list = []
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        original_raw_get_many = storage.raw_records.get_many
        original_observation_get_many = storage.observations.get_many
        original_metric_get = storage.metric_results.get

        def spy_raw_get_many(record_ids):  # type: ignore[no-untyped-def]
            hydrated_raw.append(tuple(record_ids))
            return original_raw_get_many(record_ids)

        def spy_observation_get_many(record_ids):  # type: ignore[no-untyped-def]
            hydrated_observations.append(tuple(record_ids))
            return original_observation_get_many(record_ids)

        def spy_metric_get(result_id):  # type: ignore[no-untyped-def]
            hydrated_metrics.append(result_id)
            return original_metric_get(result_id)

        storage.raw_records.get_many = spy_raw_get_many  # type: ignore[method-assign]
        storage.observations.get_many = spy_observation_get_many  # type: ignore[method-assign]
        storage.metric_results.get = spy_metric_get  # type: ignore[method-assign]
        storage.raw_records.list = (  # type: ignore[method-assign]
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("bounded history must not hydrate full raw history")
            )
        )
        storage.observations.list = (  # type: ignore[method-assign]
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("bounded history must not hydrate full observation history")
            )
        )
        raw_list = storage.raw_records.list
        observation_list = storage.observations.list
        del raw_list, observation_list

    started = time.perf_counter()
    third = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert third.status == "processed"
    assert third.target is not None and third.target.state == "processed"
    assert third.target.traceability_verified is True
    target = third.target
    assert target.manager_cik == "0000000002"

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        observations = InstitutionalObservationService(storage).query(
            InstitutionalObservationQuery(
                asset_id="equity:us:aapl", known_at=third.effective_known_at
            )
        )
        periods = {view.report.report_period for view in observations.observations}
        assert periods == {_OLDER_PERIOD, _NEWER_PERIOD}
        before_json = _canonical_metric_json(storage, asset_id="equity:us:aapl")

    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        after_json = _canonical_metric_json(storage, asset_id="equity:us:aapl")
    assert before_json == after_json

    assert not (set(hydrated_metrics) & market_ids)
    total_raw_hydrated = sum(len(batch) for batch in hydrated_raw)
    with LocalStorage(StoragePaths.from_root(tmp_path), read_only=True) as storage:
        total_raw = storage.raw_records.count()
    assert total_raw_hydrated < total_raw
    candidate_report_count = len(target.older_candidate_ids) + len(target.newer_candidate_ids)
    assert total_raw_hydrated <= 6 * max(1, candidate_report_count) + 20
    print(
        f"bounded_history: target={target.manager_cik} "
        f"candidates={candidate_report_count} "
        f"raw_hydrated={total_raw_hydrated} total_raw={total_raw} "
        f"elapsed_ms={elapsed_ms:.1f} peak_rss_kb={peak_rss_kb}"
    )


def _prepare_processed_window(tmp_path: Path):
    transport = _CountingTransport()
    submissions = _SubmissionsClient()
    documents = _DocumentClient()
    clock = _Clock(datetime(2026, 9, 11, 12, 0, tzinfo=UTC))
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
    third = _run(
        transport=transport,
        submissions=submissions,
        documents=documents,
        clock=clock,
        workspace=tmp_path,
    )
    assert third.target is not None and third.target.state == "processed"
    return third.target, third.effective_known_at


def test_selected_corrupt_or_foreign_evidence_fails_closed(tmp_path: Path) -> None:
    target, known_at = _prepare_processed_window(tmp_path)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        holdings = InstitutionalHoldingsRepository(storage.raw_records)
        reports = holdings.list_reports(manager_cik=target.manager_cik, known_at=known_at)
        assert reports
        semantics = InstitutionalSemanticsRepository(storage.raw_records)
        artifact = semantics.get_for_parent(reports[0])
        assert artifact is not None
        claims = repository.list(
            known_at=known_at, artifact_id=artifact.artifact_id, manager_cik=target.manager_cik
        )
        assert len(claims) >= 1
        selected = claims[0]
        raw_record_id = type(selected).expected_raw_record_id(selected.correspondence_id)
        row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(raw_record_id)],
        ).fetchone()
        assert row is not None
        (storage.paths.raw_dir / row[0]).write_text('{"tampered":true}', encoding="utf-8")
        with _assert_raises_storage_error():
            repository.list(
                known_at=known_at, artifact_id=artifact.artifact_id, manager_cik=target.manager_cik
            )
        with _assert_raises_storage_error():
            repository.selected_claim_ids_for_candidate(
                known_at=known_at,
                claim_ids=(selected.correspondence_id,),
            )

    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_target, foreign_known_at = _prepare_processed_window(foreign_root)
    with LocalStorage(StoragePaths.from_root(foreign_root)) as storage:
        from investment_analyst.evidence.sec_institutional_correspondence.models import (
            SecInstitutionalRowCorrespondence,
        )
        from investment_analyst.evidence.sec_institutional_observations.models import (
            InstitutionalObservationRequest as _ForeignObservationRequest,
        )

        repository = SecInstitutionalRowCorrespondenceRepository(storage.raw_records)
        holdings = InstitutionalHoldingsRepository(storage.raw_records)
        reports = holdings.list_reports(
            manager_cik=foreign_target.manager_cik, known_at=foreign_known_at
        )
        semantics = InstitutionalSemanticsRepository(storage.raw_records)
        artifact = semantics.get_for_parent(reports[0])
        assert artifact is not None
        row = artifact.rows[0]
        foreign_claim = SecInstitutionalRowCorrespondence.claim(
            asset_id="equity:us:msft",
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
            available_at=max(foreign_known_at, artifact.available_at),
            recorded_at=foreign_known_at,
        )
        repository.save(foreign_claim)
        foreign_visible = repository.list(
            known_at=foreign_known_at,
            artifact_id=artifact.artifact_id,
            manager_cik=foreign_target.manager_cik,
        )
        assert any(
            claim.correspondence_id == foreign_claim.correspondence_id for claim in foreign_visible
        )
        summary = InstitutionalObservationService(storage).normalize(
            _ForeignObservationRequest(
                asset_id="equity:us:msft",
                manager_cik=foreign_target.manager_cik,
                report_ids=(reports[0].report_id,),
                known_at=foreign_known_at,
            )
        )
        assert summary.skipped_by_reason.get("row_ambiguous_asset", 0) >= 1
        assert summary.rows_linked < summary.rows_examined


@contextmanager
def _assert_raises_storage_error():
    try:
        yield
    except StorageError:
        return
    raise AssertionError("selected corrupt evidence must fail closed")
