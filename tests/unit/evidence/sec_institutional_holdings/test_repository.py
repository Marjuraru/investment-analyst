from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord
from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
    InstitutionalHoldingsResolutionOutcome,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
    outcome_from_raw_record,
    outcome_to_raw_record,
    position_from_raw_record,
    position_to_raw_record,
    report_from_raw_record,
    report_to_raw_record,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    parse_institutional_holdings,
)
from investment_analyst.storage import (
    LocalStorage,
    StorageError,
    StoragePaths,
)

_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>2024-12-31</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal></edgarSubmission>"""
_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>
<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"""


def _revision(name: str, digest: str) -> SecFilerDocumentRevision:
    filing = SecFiling(
        filing_id=SecFiling.expected_id("0001067983", "0000950123-25-000001"),
        filer_cik="0001067983",
        accession="0000950123-25-000001",
        form="13F-HR",
        filing_date=date(2025, 2, 14),
        report_date=date(2024, 12, 31),
        accepted_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
        is_amendment=False,
    )
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


def test_report_and_position_raw_records_round_trip_without_asset() -> None:
    report, positions = parse_institutional_holdings(
        _COVER,
        _TABLE,
        cover_revision=_revision("primary_doc.xml", "a" * 64),
        information_table_revision=_revision("infotable.xml", "b" * 64),
        parsed_at=datetime(2025, 2, 15, tzinfo=UTC),
    )
    report_record = report_to_raw_record(report)
    position_record = position_to_raw_record(positions[0])

    assert report_record.asset_id is None
    assert position_record.asset_id is None
    assert report_from_raw_record(report_record) == report
    assert position_from_raw_record(position_record) == positions[0]


def _filing(
    *,
    cik: str,
    accession_suffix: int,
    accepted_day: int,
    report_date: date | None = date(2024, 12, 31),
) -> SecFiling:
    accession = f"0000950123-25-{accession_suffix:06d}"
    return SecFiling(
        filing_id=SecFiling.expected_id(cik, accession),
        filer_cik=cik,
        accession=accession,
        form="13F-HR",
        filing_date=date(2025, 2, accepted_day),
        report_date=report_date,
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
    report, positions = parse_institutional_holdings(
        _COVER,
        _TABLE,
        cover_revision=_filing_revision(filing, f"{name_seed}-primary.xml", "a" * 64),
        information_table_revision=_filing_revision(filing, f"{name_seed}-info.xml", "b" * 64),
        parsed_at=parsed_at,
    )
    return report, positions


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
    repository: InstitutionalHoldingsRepository, *, name_prefix: str = "seed"
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


def _wrap_raw_store(raw_records):
    observed: list[tuple[object, ...]] = []
    list_calls = 0
    original_list = raw_records.list
    original_get_many = raw_records.get_many
    original_list_record_ids = raw_records.list_record_ids

    def failing_list(*args: object, **kwargs: object):
        nonlocal list_calls
        list_calls += 1
        return original_list(*args, **kwargs)

    def spy_get_many(record_ids) -> dict:
        observed.append(tuple(record_ids))
        return original_get_many(record_ids)

    raw_records.list = failing_list  # type: ignore[method-assign]
    raw_records.get_many = spy_get_many  # type: ignore[method-assign]
    return observed, original_list_record_ids, lambda: list_calls


def test_13f_lists_bound_get_many_batches(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        with patch.object(InstitutionalHoldingsRepository, "_LIST_BATCH_SIZE", 1):
            seed = _seed_mixed_corpus(repository)
            observed, _, list_calls = _wrap_raw_store(storage.raw_records)
            known_at = datetime(2025, 2, 18, tzinfo=UTC)
            target_reports = sorted(
                seed["target_reports"],  # type: ignore[arg-type]
                key=lambda item: (
                    item.available_at,
                    item.cover_revision.document.filing.accession,
                    str(item.report_id),
                ),
            )
            target_outcomes = sorted(
                seed["target_outcomes"],  # type: ignore[arg-type]
                key=lambda item: (
                    item.available_at,
                    item.filing.accession,
                    str(item.outcome_id),
                ),
            )
            target_positions = sorted(
                seed["target_positions"],  # type: ignore[arg-type]
                key=lambda item: (str(item.report_id), item.row_number),
            )
            reports = repository.list_reports(manager_cik="0001067983", known_at=known_at)
            report_batches = [len(chunk) for chunk in observed]
            observed.clear()
            outcomes = repository.list_outcomes(manager_cik="0001067983", known_at=known_at)
            outcome_batches = [len(chunk) for chunk in observed]
            observed.clear()
            positions = repository.list_positions(
                report_ids={report.report_id for report in reports}, known_at=known_at
            )
            position_batches = [len(chunk) for chunk in observed]

    assert [report.report_id for report in reports] == [
        report.report_id for report in target_reports
    ]
    assert [outcome.outcome_id for outcome in outcomes] == [
        outcome.outcome_id for outcome in target_outcomes
    ]
    assert [position.position_id for position in positions] == [
        position.position_id for position in target_positions
    ]
    assert report_batches == [1, 1]
    assert outcome_batches == [1, 1]
    assert position_batches == [1, 1]
    assert list_calls() == 0


def test_13f_lists_preserve_filters_order_and_known_at(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path / "lists")) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository)
        known_at = datetime(2025, 2, 17, tzinfo=UTC)
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
        reports = repository.list_reports(manager_cik="0001067983", known_at=known_at)
        outcomes = repository.list_outcomes(manager_cik="0001067983", known_at=known_at)
        positions = repository.list_positions(
            report_ids={report.report_id for report in expected_reports},
            known_at=known_at,
        )
        before_reports = repository.list_reports(
            manager_cik="0001067983", known_at=datetime(2025, 2, 14, 17, tzinfo=UTC)
        )
        before_outcomes = repository.list_outcomes(
            manager_cik="0001067983", known_at=datetime(2025, 2, 14, 17, tzinfo=UTC)
        )
        before_positions = repository.list_positions(
            report_ids={report.report_id for report in expected_reports},
            known_at=datetime(2025, 2, 14, 17, tzinfo=UTC),
        )
        empty_positions = repository.list_positions(report_ids=set(), known_at=known_at)
        assert [report.report_id for report in reports] == [
            report.report_id for report in expected_reports
        ]
        assert [outcome.outcome_id for outcome in outcomes] == [
            outcome.outcome_id for outcome in expected_outcomes
        ]
        assert [position.position_id for position in positions] == [
            position.position_id for position in expected_positions
        ]
        assert before_reports == []
        assert before_outcomes == []
        assert before_positions == []
        assert empty_positions == []
        assert all(report.manager_cik == "0001067983" for report in reports)
        assert all(outcome.filing.filer_cik == "0001067983" for outcome in outcomes)
        assert all(
            position.report_id in {report.report_id for report in expected_reports}
            for position in positions
        )


def test_13f_lists_materialize_only_selected_records(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository, name_prefix="selected")
        known_at = datetime(2025, 2, 17, tzinfo=UTC)

        hydrated: list[object] = []
        original_get_many = storage.raw_records.get_many

        def spy_get_many(record_ids):  # type: ignore[no-untyped-def]
            hydrated.append(tuple(record_ids))
            return original_get_many(record_ids)

        storage.raw_records.get_many = spy_get_many  # type: ignore[method-assign]
        reports = repository.list_reports(manager_cik="0001067983", known_at=known_at)
        positions = repository.list_positions(
            report_ids={report.report_id for report in reports},  # type: ignore[union-attr]
            known_at=known_at,
        )

        assert [report.report_id for report in reports] == [  # type: ignore[union-attr]
            report.report_id
            for report in sorted(  # type: ignore[union-attr]
                seed["target_reports"],  # type: ignore[arg-type]
                key=lambda item: (
                    item.available_at,
                    item.cover_revision.document.filing.accession,
                    str(item.report_id),
                ),
            )
        ]
        assert positions
        assert hydrated
        assert sum(len(batch) for batch in hydrated) == len(reports) + len(positions)


def test_13f_projected_reads_reject_selected_corruption(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository, name_prefix="select-corrupt")
        target_report_id = seed["target_reports"][0].report_id  # type: ignore[union-attr]
        target_record_id = next(
            record.record_id
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version="sec-institutional-holdings-report-v1",
            )
            if report_from_raw_record(record).report_id == target_report_id
        )
        row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(target_record_id)],
        ).fetchone()
        assert row is not None
        (storage.paths.raw_dir / row[0]).write_text('{"tampered":true}', encoding="utf-8")
        with pytest.raises(StorageError, match="checksum mismatch"):
            repository.list_reports(
                manager_cik="0001067983", known_at=datetime(2025, 2, 17, tzinfo=UTC)
            )

    with LocalStorage(StoragePaths.from_root(tmp_path / "lists")) as storage:
        baseline_repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed_lists = _seed_mixed_corpus(baseline_repository, name_prefix="lists-baseline")
        known_at = datetime(2025, 2, 17, tzinfo=UTC)
        baseline_reports = [
            report_from_raw_record(record)
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version="sec-institutional-holdings-report-v1",
                available_to=known_at,
            )
        ]
        candidate = [report for report in baseline_reports if report.manager_cik == "0001067983"]
        assert sorted(report.report_id for report in candidate) == sorted(
            report.report_id  # type: ignore[union-attr]
            for report in seed_lists["target_reports"]  # type: ignore[union-attr]
        )


def test_13f_lists_propagate_missing_and_corrupt_records(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path / "missing")) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository, name_prefix="missing")
        target_record_id = InstitutionalHoldingsReport.expected_raw_record_id(
            seed["target_reports"][0].report_id  # type: ignore[union-attr]
        )
        known_at = datetime(2025, 2, 17, tzinfo=UTC)
        target_report_ids = {
            report.report_id
            for report in seed["target_reports"]  # type: ignore[union-attr]
        }

        row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(target_record_id)],
        ).fetchone()
        assert row is not None
        target_position_record_id = next(
            record.record_id
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version="sec-institutional-holding-position-v1",
            )
            if position_from_raw_record(record).report_id == seed["target_reports"][0].report_id  # type: ignore[union-attr]
        )
        target_position_row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(target_position_record_id)],
        ).fetchone()
        assert target_position_row is not None
        (storage.paths.raw_dir / row[0]).unlink()
        with pytest.raises(StorageError, match="indexed raw record file is missing"):
            repository.list_reports(manager_cik="0001067983", known_at=known_at)
        (storage.paths.raw_dir / target_position_row[0]).unlink()
        with pytest.raises(StorageError, match="indexed raw record file is missing"):
            repository.list_positions(report_ids=target_report_ids, known_at=known_at)

    with LocalStorage(StoragePaths.from_root(tmp_path / "corrupt")) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository, name_prefix="corrupt")
        target_report_id = seed["target_reports"][0].report_id  # type: ignore[union-attr]
        target_record_id = next(
            record.record_id
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version="sec-institutional-holdings-report-v1",
            )
            if report_from_raw_record(record).report_id == target_report_id
        )
        other_report_id = report_from_raw_record(
            next(
                record
                for record in storage.raw_records.list(
                    source_id="sec-edgar:institutional-holdings-13f",
                    schema_version="sec-institutional-holdings-report-v1",
                )
                if report_from_raw_record(record).manager_cik != "0001067983"
            )
        ).report_id
        row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(target_record_id)],
        ).fetchone()
        assert row is not None
        (storage.paths.raw_dir / row[0]).write_text('{"tampered":true}', encoding="utf-8")
        known_at = datetime(2025, 2, 17, tzinfo=UTC)
        with pytest.raises(StorageError, match="checksum mismatch"):
            repository.list_reports(manager_cik="0001067983", known_at=known_at)
        other_position_record_id = next(
            record.record_id
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version="sec-institutional-holding-position-v1",
            )
            if position_from_raw_record(record).report_id == other_report_id
        )
        other_row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(other_position_record_id)],
        ).fetchone()
        assert other_row is not None
        (storage.paths.raw_dir / other_row[0]).write_text('{"tampered":true}', encoding="utf-8")
        with pytest.raises(StorageError, match="checksum mismatch"):
            repository.list_positions(
                report_ids={
                    report.report_id
                    for report in seed["target_reports"]  # type: ignore[union-attr]
                }
                | {other_report_id},
                known_at=known_at,
            )

    with LocalStorage(StoragePaths.from_root(tmp_path / "outcome")) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_mixed_corpus(repository, name_prefix="outcome")
        target_outcome_id = seed["target_outcomes"][0].outcome_id  # type: ignore[union-attr]
        target_outcome_record_id = next(
            record.record_id
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
            )
            if outcome_from_raw_record(record).outcome_id == target_outcome_id
        )
        visible_record: RawRecord = next(
            record
            for record in storage.raw_records.list(
                source_id="sec-edgar:institutional-holdings-13f",
                schema_version=INSTITUTIONAL_HOLDINGS_OUTCOME_SCHEMA_VERSION,
            )
            if outcome_from_raw_record(record).filing.filer_cik != "0001067983"
        )
        known_at = datetime(2025, 2, 17, tzinfo=UTC)
        row = storage.store.connection.execute(
            "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
            [str(target_outcome_record_id)],
        ).fetchone()
        assert row is not None
        (storage.paths.raw_dir / row[0]).unlink()
        with pytest.raises(StorageError, match="indexed raw record file is missing"):
            repository.list_outcomes(manager_cik="0001067983", known_at=known_at)
        assert outcome_to_raw_record(outcome_from_raw_record(visible_record)) == visible_record
