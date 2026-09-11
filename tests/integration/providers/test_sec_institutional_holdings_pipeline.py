import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import FILER_REVISION_SCHEMA_VERSION
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
    INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION,
)
from investment_analyst.providers.fundamentals.sec_document_client import (
    SecAccessionManifest,
    SecPrimaryDocumentResponse,
)
from investment_analyst.providers.institutional_holdings import (
    sec_institutional_holdings_pipeline,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace import backup as backup_module

_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>
<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"""


class _SubmissionsClient:
    def fetch(self, filer_cik):
        captured = datetime(2025, 2, 15, tzinfo=UTC)
        return RawRecord(
            record_id=uuid4(),
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
                    "name": "Manager LLC",
                    "filings": {
                        "recent": {
                            "accessionNumber": ["0000950123-25-000001"],
                            "filingDate": ["2025-02-14"],
                            "reportDate": ["2024-12-31"],
                            "acceptanceDateTime": ["2025-02-14T18:00:00Z"],
                            "form": ["13F-HR"],
                            "primaryDocument": ["xslForm13F_X02/primary_doc.xml"],
                        }
                    },
                }
            },
            schema_version="sec-manager-submissions-snapshot-v1",
        )


class _DocumentClient:
    def __init__(self, *, xml_count: int = 2) -> None:
        self._xml_count = xml_count
        self._retrieved_at = datetime(2025, 2, 15, tzinfo=UTC)

    def fetch_manifest(self, document):
        del document
        entries = {
            0: ("filing.htm",),
            1: ("primary_doc.xml",),
            2: ("primary_doc.xml", "infotable.xml"),
            3: ("primary_doc.xml", "infotable.xml", "extra.xml"),
        }[self._xml_count]
        return SecAccessionManifest(
            entries=entries,
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=self._retrieved_at,
        )

    def fetch(self, document):
        if document.name == "xslForm13F_X02/primary_doc.xml":
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        else:
            content = _COVER if document.name == "primary_doc.xml" else _TABLE
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{document.name}",
            retrieved_at=self._retrieved_at,
        )


def test_pipeline_persists_two_revisions_report_and_positions_idempotently(
    tmp_path: Path,
) -> None:
    request = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
        filer_cik="1067983", forms=("13F-HR",)
    )
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _SubmissionsClient(), _DocumentClient()
        )
        first = pipeline.run(request)
        second = pipeline.run(request)

        assert [report.report_id for report in first] == [report.report_id for report in second]
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION)
            == 3
        )
        assert storage.raw_records.count(schema_version=FILER_REVISION_SCHEMA_VERSION) == 2
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 1
        )
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDING_POSITION_SCHEMA_VERSION)
            == 1
        )
        assert storage.raw_records.count(schema_version="sec-document-revision-v2") == 0
        assert first[0].available_at == datetime(2025, 2, 14, 18, tzinfo=UTC)
        assert first[0].value_total_matches is True
        assert backup_module._scan_raw_records(storage) == storage.raw_records.count()


def test_pipeline_records_rejection_for_zero_one_or_three_xml(
    tmp_path: Path,
) -> None:
    for xml_count in (0, 1, 3):
        root = tmp_path / str(xml_count)
        with LocalStorage(StoragePaths.from_root(root)) as storage:
            result = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
                storage, _SubmissionsClient(), _DocumentClient(xml_count=xml_count)
            ).run(
                sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                    filer_cik="1067983", forms=("13F-HR",)
                )
            )

            assert result == ()
            assert (
                storage.raw_records.count(
                    schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION
                )
                == 1
            )
            assert storage.raw_records.count(schema_version=FILER_REVISION_SCHEMA_VERSION) == 0


_PERIOD = date(2026, 3, 31)
_CUT = datetime(2026, 6, 2, tzinfo=UTC)
_DISCOVERY_AT = datetime(2026, 7, 1, tzinfo=UTC)
_RETRIEVED_AT = datetime(2026, 7, 2, tzinfo=UTC)
_PRIMARY_DOCUMENT = "xslForm13F_X02/primary_doc.xml"


def _cover_xml(value: int, form: str = "13F-HR") -> bytes:
    return (
        f"<edgarSubmission><submissionType>{form}</submissionType><filingManager>"
        "<name>Manager LLC</name></filingManager>"
        "<reportCalendarOrQuarter>03-31-2026</reportCalendarOrQuarter>"
        f"<tableEntryTotal>1</tableEntryTotal><tableValueTotal>{value}</tableValueTotal>"
        "</edgarSubmission>"
    ).encode()


def _table_xml(value: int) -> bytes:
    return (
        b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
        b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip>"
        + f"<value>{value}</value>".encode()
        + b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
        b"<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>"
        b"<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"
    )


def _filings_record(*, filer_cik: str, filings: tuple[dict[str, str], ...]) -> RawRecord:
    return RawRecord(
        record_id=uuid4(),
        asset_id=None,
        source=SourceReference(
            source_id=f"sec-edgar:manager:{filer_cik}:submissions",
            retrieved_at=_DISCOVERY_AT,
        ),
        event_time=_DISCOVERY_AT,
        available_at=_DISCOVERY_AT,
        received_at=_DISCOVERY_AT,
        payload={
            "document": {
                "cik": filer_cik,
                "name": "Manager LLC",
                "filings": {
                    "recent": {
                        "accessionNumber": [item["accession"] for item in filings],
                        "filingDate": [item["filing_date"] for item in filings],
                        "reportDate": [item["report_date"] for item in filings],
                        "acceptanceDateTime": [item["accepted_at"] for item in filings],
                        "form": [item["form"] for item in filings],
                        "primaryDocument": [
                            item.get("primary_document", _PRIMARY_DOCUMENT) for item in filings
                        ],
                    }
                },
            }
        },
        schema_version="sec-manager-submissions-snapshot-v1",
    )


def _filing(
    accession: str,
    *,
    accepted_at: str,
    form: str = "13F-HR",
    report_date: str = "2026-03-31",
    filing_date: str = "2026-04-14",
) -> dict[str, str]:
    return {
        "accession": accession,
        "form": form,
        "report_date": report_date,
        "filing_date": filing_date,
        "accepted_at": accepted_at,
    }


class _PeriodSubmissionsClient:
    def __init__(
        self, filings: tuple[dict[str, str], ...], *, filer_cik: str = "0001067983"
    ) -> None:
        self.calls = 0
        self.record = _filings_record(filer_cik=filer_cik, filings=filings)

    def fetch(self, filer_cik):
        self.calls += 1
        return self.record


class _PeriodDocumentClient:
    def __init__(
        self,
        values: dict[str, int],
        *,
        forms: dict[str, str] | None = None,
        fail_for: tuple[str, ...] = (),
        reject_for: tuple[str, ...] = (),
    ) -> None:
        self.values = values
        self.forms = forms or {}
        self.fail_for = set(fail_for)
        self.reject_for = set(reject_for)
        self.manifest_calls = 0
        self.document_calls = 0

    @property
    def archives_calls(self) -> int:
        return self.manifest_calls + self.document_calls

    def fetch_manifest(self, document):
        self.manifest_calls += 1
        accession = document.filing.accession
        entries = (
            ("filing.htm",)
            if accession in self.reject_for
            else ("filing.htm", "primary_doc.xml", "infotable.xml")
        )
        return SecAccessionManifest(
            entries=entries,
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=_RETRIEVED_AT,
        )

    def fetch(self, document):
        self.document_calls += 1
        accession = document.filing.accession
        if accession in self.fail_for:
            raise RuntimeError("SEC Archives is unavailable for this accession")
        if document.name == _PRIMARY_DOCUMENT:
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        elif document.name == "primary_doc.xml":
            content = _cover_xml(self.values[accession], self.forms.get(accession, "13F-HR"))
        else:
            content = _table_xml(self.values[accession])
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{accession}/{document.name}",
            retrieved_at=_RETRIEVED_AT,
        )


def _period_request(accessions_per_manager: int = 1):
    return sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPeriodImportRequest(
        filer_cik="1067983",
        report_period=_PERIOD,
        known_at=_CUT,
        accessions_per_manager=accessions_per_manager,
    )


def test_period_mode_selects_only_the_target_period_accepted_at_the_cut(
    tmp_path: Path,
) -> None:
    filings = (
        _filing("0000000001-26-000001", accepted_at="2026-04-15T12:00:00Z"),
        _filing("0000000002-26-000001", accepted_at="2026-06-20T12:00:00Z", form="13F-HR/A"),
        _filing(
            "0000000003-26-000001",
            accepted_at="2026-02-01T12:00:00Z",
            report_date="2025-12-31",
        ),
        _filing("0000000004-26-000001", accepted_at="2026-04-01T00:00:00Z", form="13F-NT"),
    )
    submissions_client = _PeriodSubmissionsClient(filings)
    document_client = _PeriodDocumentClient({"0000000001-26-000001": 100})
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, submissions_client, document_client
        )
        result = pipeline.run_period(_period_request())

        assert result.eligible_accessions == ("0000000001-26-000001",)
        assert result.created_accessions == ("0000000001-26-000001",)
        assert result.reused_accessions == ()
        assert result.pending_before == 1
        assert result.backlog_after == 0
        assert result.submissions_created == 1 and result.submissions_reused == 0
        assert submissions_client.calls == 1
        assert result.known_at == _CUT
        report = result.reports[0]
        assert report.available_at == datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        assert report.available_at.date() != report.cover_revision.document.filing.filing_date
        assert report.report_period == _PERIOD

        manual = pipeline.run(
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsImportRequest(
                filer_cik="1067983", forms=("13F-HR",)
            )
        )
        assert [item.report_id for item in manual] == [report.report_id]
        assert report.report_id in {item.report_id for item in manual}


def test_period_mode_reuses_materialized_accessions_without_archives(tmp_path: Path) -> None:
    values = {"0000000001-26-000001": 100, "0000000002-26-000001": 200}
    filings = (
        _filing("0000000001-26-000001", accepted_at="2026-04-15T12:00:00Z"),
        _filing("0000000002-26-000001", accepted_at="2026-04-20T12:00:00Z", form="13F-HR/A"),
    )
    submissions_client = _PeriodSubmissionsClient(filings)
    document_client = _PeriodDocumentClient(values, forms={"0000000002-26-000001": "13F-HR/A"})
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, submissions_client, document_client
        )
        first = pipeline.run_period(_period_request(accessions_per_manager=2))
        first_archives = document_client.archives_calls
        assert first.created_accessions == tuple(sorted(values))
        assert first.reused_accessions == ()
        assert first.backlog_after == 0
        assert first_archives == 8

        second = pipeline.run_period(_period_request(accessions_per_manager=2))
        assert document_client.archives_calls == first_archives
        assert second.reused_accessions == tuple(sorted(values))
        assert second.created_accessions == ()
        assert second.attempted_accessions == ()
        assert second.pending_before == 0 and second.backlog_after == 0
        assert second.submissions_created == 0 and second.submissions_reused == 1
        assert submissions_client.calls == 2
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 2
        )


def test_period_mode_reports_exact_backlog_and_resumes_without_skipping(
    tmp_path: Path,
) -> None:
    values = {
        "0000000001-26-000001": 100,
        "0000000003-26-000001": 300,
        "0000000002-26-000001": 200,
    }
    filings = (
        _filing("0000000001-26-000001", accepted_at="2026-04-15T12:00:00Z"),
        _filing("0000000002-26-000001", accepted_at="2026-04-16T12:00:00Z"),
        _filing("0000000003-26-000001", accepted_at="2026-04-17T12:00:00Z"),
    )
    document_client = _PeriodDocumentClient(values)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _PeriodSubmissionsClient(filings), document_client
        )
        created: list[str] = []
        backlogs: list[int] = []
        for _ in range(3):
            result = pipeline.run_period(_period_request())
            assert result.pending_before == 3 - len(created)
            created.extend(result.created_accessions)
            backlogs.append(result.backlog_after)

        assert created == [
            "0000000001-26-000001",
            "0000000002-26-000001",
            "0000000003-26-000001",
        ]
        assert backlogs == [2, 1, 0]

        terminal = pipeline.run_period(_period_request())
        assert terminal.created_accessions == ()
        assert terminal.reused_accessions == tuple(created)
        assert terminal.backlog_after == 0
        assert terminal.pending_before == 0


def test_period_mode_rejects_conflicting_duplicate_accession_before_archives(
    tmp_path: Path,
) -> None:
    filings = (
        _filing("0000000001-26-000001", accepted_at="2026-04-15T12:00:00Z"),
        _filing("0000000001-26-000001", accepted_at="2026-04-16T12:00:00Z"),
    )
    document_client = _PeriodDocumentClient({"0000000001-26-000001": 100})
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _PeriodSubmissionsClient(filings), document_client
        )
        with pytest.raises(
            sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipelineError,
            match="duplicate accession conflicts",
        ):
            pipeline.run_period(_period_request())
        assert document_client.archives_calls == 0
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 0
        )
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION)
            == 0
        )


def test_period_mode_records_a_failed_accession_and_preserves_progress(
    tmp_path: Path,
) -> None:
    filings = (
        _filing("0000000001-26-000001", accepted_at="2026-04-15T12:00:00Z"),
        _filing("0000000002-26-000001", accepted_at="2026-04-16T12:00:00Z"),
    )
    values = {"0000000001-26-000001": 100, "0000000002-26-000001": 200}
    failing = _PeriodDocumentClient(values, fail_for=("0000000002-26-000001",))
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _PeriodSubmissionsClient(filings), failing
        ).run_period(_period_request(accessions_per_manager=2))
        assert first.created_accessions == ("0000000001-26-000001",)
        assert first.failed_accessions == ("0000000002-26-000001",)
        assert first.failure_codes == ("RuntimeError",)
        assert first.rejected_accessions == ()
        assert first.rejected_or_failed == 1
        assert first.pending_before == 2 and first.backlog_after == 1
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 1
        )

        recovered = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _PeriodSubmissionsClient(filings), _PeriodDocumentClient(values)
        ).run_period(_period_request(accessions_per_manager=2))
        assert recovered.created_accessions == ("0000000002-26-000001",)
        assert recovered.reused_accessions == ("0000000001-26-000001",)
        assert recovered.backlog_after == 0
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_REPORT_SCHEMA_VERSION)
            == 2
        )


def test_period_mode_terminal_rejected_outcome_is_reused_and_does_not_call_archives(
    tmp_path: Path,
) -> None:
    filings = (
        _filing("0000000001-26-000001", accepted_at="2026-04-15T12:00:00Z"),
        _filing("0000000002-26-000001", accepted_at="2026-04-16T12:00:00Z"),
    )
    values = {"0000000001-26-000001": 100, "0000000002-26-000001": 200}
    rejecting = _PeriodDocumentClient(values, reject_for=("0000000001-26-000001",))

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        # Step 1: Run 1 accession - filing 1 is rejected
        pipeline_1 = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, _PeriodSubmissionsClient(filings), rejecting
        )
        first = pipeline_1.run_period(_period_request(accessions_per_manager=1))
        assert first.attempted_accessions == ("0000000001-26-000001",)
        assert first.created_accessions == ()
        assert first.rejected_accessions == ("0000000001-26-000001",)
        assert first.pending_before == 2
        assert first.backlog_after == 2
        assert len(first.reports) == 0
        assert (
            storage.raw_records.count(schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION)
            >= 1
        )

        # Step 2: Run 1 accession - filing 1 is recognized as terminal-rejected (reused),
        # so filing 2 is attempted without calling Archives for filing 1!
        second = pipeline_1.run_period(_period_request(accessions_per_manager=1))
        assert second.reused_accessions == ("0000000001-26-000001",)
        assert second.attempted_accessions == ("0000000002-26-000001",)
        assert second.created_accessions == ("0000000002-26-000001",)
        assert second.rejected_accessions == ()
        assert second.pending_before == 1
        assert second.backlog_after == 0
        assert len(second.reports) == 1

        # Step 3: Run again - both filings are terminal, backlog is 0, zero Archives calls!
        archives_before_third = rejecting.archives_calls
        third = pipeline_1.run_period(_period_request(accessions_per_manager=1))
        assert set(third.reused_accessions) == {
            "0000000001-26-000001",
            "0000000002-26-000001",
        }
        assert third.attempted_accessions == ()
        assert third.pending_before == 0
        assert third.backlog_after == 0
        assert rejecting.archives_calls == archives_before_third


_OLDER_PERIOD = date(2025, 12, 31)
_NEWER_PERIOD = date(2026, 3, 31)


def _pair_cover_xml(value: int, period: date, form: str = "13F-HR") -> bytes:
    return (
        f"<edgarSubmission><submissionType>{form}</submissionType><filingManager>"
        "<name>Manager LLC</name></filingManager>"
        f"<reportCalendarOrQuarter>{period.strftime('%m-%d-%Y')}</reportCalendarOrQuarter>"
        f"<tableEntryTotal>1</tableEntryTotal><tableValueTotal>{value}</tableValueTotal>"
        "</edgarSubmission>"
    ).encode()


class _PairDocumentClient:
    """Bounded Archives double that renders each accession with its own report period."""

    def __init__(
        self,
        values: dict[str, int],
        periods: dict[str, date],
        *,
        reject_for: tuple[str, ...] = (),
    ) -> None:
        self.values = values
        self.periods = periods
        self.reject_for = set(reject_for)
        self.manifest_calls = 0
        self.document_calls = 0

    @property
    def archives_calls(self) -> int:
        return self.manifest_calls + self.document_calls

    def fetch_manifest(self, document):
        self.manifest_calls += 1
        accession = document.filing.accession
        entries = (
            ("filing.htm",)
            if accession in self.reject_for
            else ("filing.htm", "primary_doc.xml", "infotable.xml")
        )
        return SecAccessionManifest(
            entries=entries,
            sha256="c" * 64,
            size_bytes=10,
            url="https://www.sec.gov/Archives/index.json",
            retrieved_at=_RETRIEVED_AT,
        )

    def fetch(self, document):
        self.document_calls += 1
        accession = document.filing.accession
        if document.name == _PRIMARY_DOCUMENT:
            content = b"<!DOCTYPE html><html><body>declared locator</body></html>"
        elif document.name == "primary_doc.xml":
            content = _pair_cover_xml(
                self.values[accession], self.periods[accession], form=document.filing.form
            )
        else:
            content = _table_xml(self.values[accession])
        return SecPrimaryDocumentResponse(
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            url=f"https://www.sec.gov/Archives/{accession}/{document.name}",
            retrieved_at=_RETRIEVED_AT,
        )


def _pair_request(
    report_periods: tuple[date, ...],
    accessions_per_period: int = 1,
):
    return sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPeriodsImportRequest(
        filer_cik="1067983",
        report_periods=report_periods,
        known_at=_CUT,
        accessions_per_period=accessions_per_period,
    )


def test_periods_mode_shares_one_submissions_revision_across_two_report_periods(
    tmp_path: Path,
) -> None:
    filings = (
        _filing(
            "0000000001-26-000001",
            accepted_at="2026-02-10T12:00:00Z",
            report_date=_OLDER_PERIOD.isoformat(),
            filing_date="2026-02-09",
        ),
        _filing("0000000002-26-000001", accepted_at="2026-05-10T12:00:00Z"),
    )
    periods = {
        "0000000001-26-000001": _OLDER_PERIOD,
        "0000000002-26-000001": _NEWER_PERIOD,
    }
    values = {"0000000001-26-000001": 100, "0000000002-26-000001": 200}
    submissions_client = _PeriodSubmissionsClient(filings)
    document_client = _PairDocumentClient(values, periods)

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, submissions_client, document_client
        )

        first = pipeline.run_periods(_pair_request((_OLDER_PERIOD, _NEWER_PERIOD)))

        assert submissions_client.calls == 1
        assert first.submissions_created == 1 and first.submissions_reused == 0
        assert tuple(item.report_period for item in first.periods) == (
            _OLDER_PERIOD,
            _NEWER_PERIOD,
        )
        assert {item.submissions_raw_record_id for item in first.periods} == {
            first.submissions_raw_record_id
        }
        assert first.created_accessions == (
            "0000000001-26-000001",
            "0000000002-26-000001",
        )
        assert first.attempted_accessions == first.created_accessions
        assert first.backlog_after == 0
        assert {report.report_period for report in first.reports} == {
            _OLDER_PERIOD,
            _NEWER_PERIOD,
        }
        archives_after_first = document_client.archives_calls

        second = pipeline.run_periods(_pair_request((_OLDER_PERIOD, _NEWER_PERIOD)))

        assert submissions_client.calls == 2
        assert second.submissions_created == 0 and second.submissions_reused == 1
        assert second.created_accessions == ()
        assert set(second.reused_accessions) == {
            "0000000001-26-000001",
            "0000000002-26-000001",
        }
        assert second.attempted_accessions == ()
        assert second.backlog_after == 0
        assert document_client.archives_calls == archives_after_first


def test_periods_mode_bounds_attempts_per_period_and_preserves_backlog(tmp_path: Path) -> None:
    filings = (
        _filing(
            "0000000001-26-000001",
            accepted_at="2026-02-10T12:00:00Z",
            report_date=_OLDER_PERIOD.isoformat(),
            filing_date="2026-02-09",
        ),
        _filing("0000000002-26-000001", accepted_at="2026-05-10T12:00:00Z"),
        _filing(
            "0000000003-26-000001",
            accepted_at="2026-05-12T12:00:00Z",
            form="13F-HR/A",
        ),
    )
    periods = {
        "0000000001-26-000001": _OLDER_PERIOD,
        "0000000002-26-000001": _NEWER_PERIOD,
        "0000000003-26-000001": _NEWER_PERIOD,
    }
    values = {
        "0000000001-26-000001": 100,
        "0000000002-26-000001": 200,
        "0000000003-26-000001": 300,
    }
    submissions_client = _PeriodSubmissionsClient(filings)
    document_client = _PairDocumentClient(values, periods)

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, submissions_client, document_client
        )

        first = pipeline.run_periods(_pair_request((_OLDER_PERIOD, _NEWER_PERIOD)))

        assert submissions_client.calls == 1
        assert len(first.attempted_accessions) == 2
        older, newer = first.periods
        assert older.attempted_accessions == ("0000000001-26-000001",)
        assert newer.attempted_accessions == ("0000000002-26-000001",)
        assert newer.backlog_after == 1
        assert first.backlog_after == 1

        second = pipeline.run_periods(_pair_request((_OLDER_PERIOD, _NEWER_PERIOD)))

        assert submissions_client.calls == 2
        assert second.created_accessions == ("0000000003-26-000001",)
        assert second.reused_accessions == (
            "0000000001-26-000001",
            "0000000002-26-000001",
        )
        assert len(second.attempted_accessions) == 1
        assert second.backlog_after == 0


def test_periods_mode_reuses_a_terminal_rejection_without_archives(tmp_path: Path) -> None:
    filings = (
        _filing(
            "0000000001-26-000001",
            accepted_at="2026-02-10T12:00:00Z",
            report_date=_OLDER_PERIOD.isoformat(),
            filing_date="2026-02-09",
        ),
        _filing("0000000002-26-000001", accepted_at="2026-05-10T12:00:00Z"),
    )
    periods = {
        "0000000001-26-000001": _OLDER_PERIOD,
        "0000000002-26-000001": _NEWER_PERIOD,
    }
    values = {"0000000001-26-000001": 100, "0000000002-26-000001": 200}
    submissions_client = _PeriodSubmissionsClient(filings)
    document_client = _PairDocumentClient(values, periods, reject_for=("0000000001-26-000001",))

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipeline(
            storage, submissions_client, document_client
        )

        first = pipeline.run_periods(_pair_request((_OLDER_PERIOD, _NEWER_PERIOD)))

        older, newer = first.periods
        assert older.rejected_accessions == ("0000000001-26-000001",)
        assert older.created_accessions == ()
        assert older.backlog_after == 1
        assert newer.created_accessions == ("0000000002-26-000001",)
        archives_after_first = document_client.archives_calls

        second = pipeline.run_periods(_pair_request((_OLDER_PERIOD, _NEWER_PERIOD)))

        older_again, newer_again = second.periods
        assert older_again.reused_accessions == ("0000000001-26-000001",)
        assert older_again.attempted_accessions == ()
        assert newer_again.reused_accessions == ("0000000002-26-000001",)
        assert second.created_accessions == ()
        assert document_client.archives_calls == archives_after_first


def test_periods_request_rejects_invalid_bounds() -> None:
    request_type = sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPeriodsImportRequest

    with pytest.raises(sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipelineError):
        request_type(
            filer_cik="1067983",
            report_periods=(_OLDER_PERIOD, _NEWER_PERIOD, date(2026, 6, 30)),
            known_at=_CUT,
        )

    with pytest.raises(sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipelineError):
        request_type(
            filer_cik="1067983",
            report_periods=(_NEWER_PERIOD, _OLDER_PERIOD),
            known_at=_CUT,
        )

    with pytest.raises(sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipelineError):
        request_type(
            filer_cik="1067983",
            report_periods=(_OLDER_PERIOD, _OLDER_PERIOD),
            known_at=_CUT,
        )

    with pytest.raises(sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipelineError):
        request_type(
            filer_cik="1067983",
            report_periods=(_OLDER_PERIOD, _NEWER_PERIOD),
            known_at=_CUT,
            accessions_per_period=11,
        )

    with pytest.raises(sec_institutional_holdings_pipeline.SecInstitutionalHoldingsPipelineError):
        request_type(
            filer_cik="1067983",
            report_periods=(datetime(2025, 12, 31, tzinfo=UTC), _NEWER_PERIOD),
            known_at=_CUT,
        )
