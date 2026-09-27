"""Projected 13F reads preserve PIT results while skipping unrelated history."""

import time
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_SOURCE_ID,
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
    InstitutionalHoldingsResolutionOutcome,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
    report_to_raw_record,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    parse_institutional_holdings,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>2024-12-31</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>
<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"""


def _filing(*, cik: str, accession_suffix: int, accepted_day: int) -> SecFiling:
    accession = f"0000950123-25-{accession_suffix:06d}"
    return SecFiling(
        filing_id=SecFiling.expected_id(cik, accession),
        filer_cik=cik,
        accession=accession,
        form="13F-HR",
        filing_date=date(2025, 2, accepted_day),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 2, accepted_day, 18, tzinfo=UTC),
        is_amendment=False,
    )


def _filing_revision(filing: SecFiling, name: str, digest: str) -> SecFilerDocumentRevision:
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name),
        filing=filing,
        name=name,
    )
    revision_id = SecFilerDocumentRevision.expected_id(document.document_id, digest)
    return SecFilerDocumentRevision(
        revision_id=revision_id,
        filer_cik=filing.filer_cik,
        document=document,
        raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid4(),
        content_sha256=digest,
        content_size_bytes=12,
        available_at=filing.accepted_at,
        retrieved_at=datetime(2025, 2, 15, tzinfo=UTC),
        source_url=f"https://www.sec.gov/Archives/{name}",
    )


def _report(
    filing: SecFiling, name_seed: str, parsed_at: datetime
) -> tuple[InstitutionalHoldingsReport, tuple[InstitutionalHoldingPosition, ...]]:
    return parse_institutional_holdings(
        _COVER,
        _TABLE,
        cover_revision=_filing_revision(filing, f"{name_seed}-primary.xml", "a" * 64),
        information_table_revision=_filing_revision(filing, f"{name_seed}-info.xml", "b" * 64),
        parsed_at=parsed_at,
    )


def _outcome(
    filing: SecFiling, resource_name: str, retrieved_at: datetime
) -> InstitutionalHoldingsResolutionOutcome:
    outcome_id = InstitutionalHoldingsResolutionOutcome.expected_id(
        filing.accession, resource_name, "c" * 64, "accepted"
    )
    return InstitutionalHoldingsResolutionOutcome(
        outcome_id=outcome_id,
        raw_record_id=InstitutionalHoldingsResolutionOutcome.expected_raw_record_id(outcome_id),
        filing=filing,
        discovery_raw_record_id=uuid4(),
        declared_locator=f"locator/{resource_name}",
        resource_name=resource_name,
        resource_url=f"https://www.sec.gov/Archives/{resource_name}",
        content_sha256="c" * 64,
        content_size_bytes=12,
        manifest_url="https://www.sec.gov/Archives/manifest.xml",
        manifest_sha256="d" * 64,
        available_at=filing.accepted_at,
        retrieved_at=retrieved_at,
        status="accepted",
        reason_code="structured_sec_xml",
    )


def _seed_mixed_corpus(
    repository: InstitutionalHoldingsRepository, *, name_prefix: str
) -> dict[str, object]:
    parsed_at = datetime(2025, 2, 17, tzinfo=UTC)
    target_filings = [
        _filing(cik="0001067983", accession_suffix=1, accepted_day=14),
        _filing(cik="0001067983", accession_suffix=2, accepted_day=16),
    ]
    other_filing = _filing(cik="0001234567", accession_suffix=3, accepted_day=15)
    target_reports: list[InstitutionalHoldingsReport] = []
    target_positions: list[InstitutionalHoldingPosition] = []
    seen_report_ids: set[object] = set()
    for index, filing in enumerate([*target_filings, other_filing]):
        report, positions = _report(filing, f"{name_prefix}-{index}", parsed_at)
        assert report.report_id not in seen_report_ids
        seen_report_ids.add(report.report_id)
        repository.save_report(report)
        repository.save_positions(positions)
        if filing.filer_cik == "0001067983":
            target_reports.append(report)
            target_positions.extend(positions)
    target_outcomes = [
        _outcome(target_filings[0], f"{name_prefix}-primary-0.xml", parsed_at),
        _outcome(target_filings[1], f"{name_prefix}-primary-1.xml", parsed_at),
    ]
    other_outcome = _outcome(other_filing, f"{name_prefix}-primary-2.xml", parsed_at)
    for outcome in [*target_outcomes, other_outcome]:
        repository.save_outcome(outcome)
    return {
        "target_reports": target_reports,
        "target_positions": target_positions,
        "target_outcomes": target_outcomes,
    }


def test_13f_projected_reads_preserve_pit_and_order(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository, name_prefix="projected")
        for index in range(60):
            storage.raw_records.save(
                RawRecord(
                    record_id=uuid4(),
                    asset_id=None,
                    source=SourceReference(
                        source_id="sec-edgar:other-corpus",
                        record_key=f"unrelated-{index}",
                        retrieved_at=datetime(2025, 2, 15, tzinfo=UTC),
                    ),
                    event_time=datetime(2025, 2, 14, 18, tzinfo=UTC),
                    available_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
                    received_at=datetime(2025, 2, 15, tzinfo=UTC),
                    payload={"kind": "unrelated", "index": index},
                    schema_version="sec-unrelated-v1",
                )
            )
        expected_reports = sorted(
            seed["target_reports"],  # type: ignore[arg-type]
            key=lambda item: (
                item.available_at,
                item.cover_revision.document.filing.accession,
                str(item.report_id),
            ),
        )
        expected_outcomes = sorted(
            seed["target_outcomes"],  # type: ignore[arg-type]
            key=lambda item: (
                item.available_at,
                item.filing.accession,
                str(item.outcome_id),
            ),
        )
        expected_positions = sorted(
            seed["target_positions"],  # type: ignore[arg-type]
            key=lambda item: (str(item.report_id), item.row_number),
        )

        hydrated: list[object] = []
        original_get_many = storage.raw_records.get_many

        def spy_get_many(record_ids):  # type: ignore[no-untyped-def]
            hydrated.append(tuple(record_ids))
            return original_get_many(record_ids)

        def forbidden_list(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise AssertionError("projected 13F reads must not hydrate whole documents")

        storage.raw_records.get_many = spy_get_many  # type: ignore[method-assign]
        storage.raw_records.list = forbidden_list  # type: ignore[method-assign]
        connection = storage.store.connection
        examined_before = connection.execute(
            "SELECT count(*) FROM raw_record_index WHERE source_id = ?",
            [INSTITUTIONAL_HOLDINGS_SOURCE_ID],
        ).fetchone()[0]

        started = time.perf_counter()
        first_cut = datetime(2025, 2, 15, 12, tzinfo=UTC)
        reports = repository.list_reports(manager_cik="0001067983", known_at=first_cut)
        outcomes = repository.list_outcomes(manager_cik="0001067983", known_at=first_cut)
        hydrated_reports = sum(len(batch) for batch in hydrated)
        hydrated.clear()
        full_cut = datetime(2025, 2, 17, tzinfo=UTC)
        full_reports = repository.list_reports(manager_cik="0001067983", known_at=full_cut)
        full_outcomes = repository.list_outcomes(manager_cik="0001067983", known_at=full_cut)
        full_positions = repository.list_positions(
            report_ids={report.report_id for report in full_reports},  # type: ignore[union-attr]
            known_at=full_cut,
        )
        hydrated_selected = sum(len(batch) for batch in hydrated)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        import resource

        peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        assert reports == expected_reports[:1]
        assert outcomes == expected_outcomes[:1]
        assert [report.report_id for report in full_reports] == [
            report.report_id
            for report in expected_reports  # type: ignore[union-attr]
        ]
        assert [outcome.outcome_id for outcome in full_outcomes] == [
            outcome.outcome_id
            for outcome in expected_outcomes  # type: ignore[union-attr]
        ]
        assert [position.position_id for position in full_positions] == [
            position.position_id
            for position in expected_positions  # type: ignore[union-attr]
        ]
        assert hydrated_reports == len(reports) + len(outcomes)
        assert hydrated_selected == len(full_reports) + len(full_outcomes) + len(full_positions)
        assert examined_before >= len(full_reports) + len(full_outcomes) + len(full_positions)
        selected_records = [
            report_to_raw_record(report)  # type: ignore[arg-type]
            for report in full_reports
        ]
        assert {record.record_id for record in selected_records} <= {
            record_id for batch in hydrated for record_id in batch
        }
        print(
            f"13f_projected_reads: examined={examined_before} "
            f"hydrated={hydrated_selected} elapsed_ms={elapsed_ms:.1f} "
            f"peak_rss_kb={peak_rss_kb}"
        )
