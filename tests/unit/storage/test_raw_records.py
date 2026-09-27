"""Tests for immutable canonical raw-record files."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingPosition,
    InstitutionalHoldingsReport,
    InstitutionalHoldingsResolutionOutcome,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    parse_institutional_holdings,
)
from investment_analyst.storage import (
    LocalStorage,
    RecordConflictError,
    RecordNotFoundError,
    StorageError,
    StoragePaths,
)
from investment_analyst.storage import (
    raw_records as raw_records_module,
)

from .conftest import make_raw_record

_THIRTEEN_F_COVER = b"""<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>
<name>Manager LLC</name></filingManager>
<reportCalendarOrQuarter>2024-12-31</reportCalendarOrQuarter>
<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal></edgarSubmission>"""
_THIRTEEN_F_TABLE = b"""<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>
<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>
<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion><votingAuthority><Sole>10</Sole>
<Shared>0</Shared><None>0</None></votingAuthority></infoTable></informationTable>"""


def _thirteen_f_filing(*, cik: str, accession_suffix: int, accepted_day: int) -> SecFiling:
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


def _thirteen_f_revision(filing: SecFiling, name: str, digest: str) -> SecFilerDocumentRevision:
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


def _thirteen_f_report(
    filing: SecFiling, name_seed: str, parsed_at: datetime
) -> tuple[InstitutionalHoldingsReport, tuple[InstitutionalHoldingPosition, ...]]:
    return parse_institutional_holdings(
        _THIRTEEN_F_COVER,
        _THIRTEEN_F_TABLE,
        cover_revision=_thirteen_f_revision(filing, f"{name_seed}-primary.xml", "a" * 64),
        information_table_revision=_thirteen_f_revision(filing, f"{name_seed}-info.xml", "b" * 64),
        parsed_at=parsed_at,
    )


def _thirteen_f_outcome(
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


def _seed_institutional_holdings(repository, *, name_prefix: str) -> dict[str, object]:
    parsed_at = datetime(2025, 2, 17, tzinfo=UTC)
    target_filings = [
        _thirteen_f_filing(cik="0001067983", accession_suffix=1, accepted_day=14),
        _thirteen_f_filing(cik="0001067983", accession_suffix=2, accepted_day=16),
    ]
    other_filing = _thirteen_f_filing(cik="0001234567", accession_suffix=3, accepted_day=15)
    target_reports: list[InstitutionalHoldingsReport] = []
    target_positions: list[InstitutionalHoldingPosition] = []
    seen_report_ids: set[object] = set()
    for index, filing in enumerate([*target_filings, other_filing]):
        report, positions = _thirteen_f_report(filing, f"{name_prefix}-{index}", parsed_at)
        assert report.report_id not in seen_report_ids
        seen_report_ids.add(report.report_id)
        repository.save_report(report)
        repository.save_positions(positions)
        if filing.filer_cik == "0001067983":
            target_reports.append(report)
            target_positions.extend(positions)
    target_outcomes = [
        _thirteen_f_outcome(target_filings[0], f"{name_prefix}-primary-0.xml", parsed_at),
        _thirteen_f_outcome(target_filings[1], f"{name_prefix}-primary-1.xml", parsed_at),
    ]
    other_outcome = _thirteen_f_outcome(other_filing, f"{name_prefix}-primary-2.xml", parsed_at)
    for outcome in [*target_outcomes, other_outcome]:
        repository.save_outcome(outcome)
    return {
        "target_reports": target_reports,
        "target_positions": target_positions,
        "target_outcomes": target_outcomes,
    }


def _indexed_path(storage, record_id) -> Path:
    row = storage.store.connection.execute(
        "SELECT relative_path FROM raw_record_index WHERE record_id = ?",
        [str(record_id)],
    ).fetchone()
    assert row is not None
    return storage.paths.raw_dir / row[0]


def test_save_and_recover_raw_record(storage) -> None:
    record = make_raw_record()

    storage.raw_records.save(record)
    recovered = storage.raw_records.get(record.record_id)

    assert recovered == record
    assert _indexed_path(storage, record.record_id).is_file()


def test_recover_raw_records_in_one_verified_batch(storage) -> None:
    first = make_raw_record()
    second = make_raw_record()
    storage.raw_records.save(first)
    storage.raw_records.save(second)

    recovered = storage.raw_records.get_many([second.record_id, first.record_id, second.record_id])

    assert recovered == {
        first.record_id: first,
        second.record_id: second,
    }
    assert storage.raw_records.get_many([]) == {}
    with pytest.raises(RecordNotFoundError, match="was not found"):
        storage.raw_records.get_many([first.record_id, uuid4()])


def test_raw_path_is_partitioned_and_safe(storage) -> None:
    record = make_raw_record(source_id="../../market/../../../escape")

    storage.raw_records.save(record)
    path = _indexed_path(storage, record.record_id).resolve()
    raw_root = storage.paths.raw_dir.resolve()
    relative = path.relative_to(raw_root)

    assert path.is_relative_to(raw_root)
    assert ".." not in relative.parts
    assert relative.parts[0].startswith("source=")
    assert relative.parts[1] == "received_date=2026-07-10"


def test_checksum_detects_raw_file_modification(storage) -> None:
    record = make_raw_record()
    storage.raw_records.save(record)
    path = _indexed_path(storage, record.record_id)
    path.write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(StorageError, match="checksum mismatch"):
        storage.raw_records.get(record.record_id)

    with pytest.raises(StorageError, match="checksum mismatch"):
        storage.raw_records.get_many([record.record_id])


def test_explicit_index_integrity_verification_detects_divergent_duplicate_document(
    storage,
) -> None:
    record = make_raw_record()
    storage.raw_records.save(record)
    storage.store.connection.execute(
        "UPDATE raw_record_index SET document_json = ? WHERE record_id = ?",
        ['{"different":"index-copy"}', str(record.record_id)],
    )

    assert storage.raw_records.get(record.record_id) == record
    with pytest.raises(StorageError, match="index does not match file"):
        storage.raw_records.verify_index_integrity([record.record_id])


def test_raw_save_is_idempotent_for_identical_content(storage) -> None:
    record = make_raw_record()

    storage.raw_records.save(record)
    storage.raw_records.save(record)
    count = storage.store.connection.execute(
        "SELECT count(*) FROM raw_record_index WHERE record_id = ?",
        [str(record.record_id)],
    ).fetchone()

    assert count == (1,)


def test_raw_save_rejects_same_id_with_different_content(storage) -> None:
    record = make_raw_record()
    storage.raw_records.save(record)
    conflicting = record.model_copy(update={"schema_version": "2"})

    with pytest.raises(RecordConflictError, match="different content"):
        storage.raw_records.save(conflicting)


def test_raw_count_and_availability_bounds_use_index_filters_without_loading_documents(
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 10, 16, tzinfo=UTC)
    first = make_raw_record().model_copy(
        update={
            "asset_id": "equity:us:aapl",
            "schema_version": "receipt-v1",
            "available_at": start,
            "received_at": start,
        }
    )
    second = make_raw_record().model_copy(
        update={
            "asset_id": "equity:us:aapl",
            "schema_version": "receipt-v1",
            "available_at": start + timedelta(hours=1),
            "received_at": start + timedelta(hours=1),
        }
    )
    foreign = make_raw_record().model_copy(
        update={
            "asset_id": "equity:us:amd",
            "schema_version": "receipt-v1",
            "available_at": start + timedelta(hours=2),
            "received_at": start + timedelta(hours=2),
        }
    )
    for record in (first, second, foreign):
        storage.raw_records.save(record)

    monkeypatch.setattr(
        storage.raw_records,
        "get_many",
        lambda record_ids: pytest.fail("aggregate query materialized raw documents"),
    )

    assert (
        storage.raw_records.count(
            asset_id="equity:us:aapl",
            source_id="alpaca:bars",
            schema_version="receipt-v1",
        )
        == 2
    )
    assert storage.raw_records.count(asset_id="equity:us:missing") == 0
    assert storage.raw_records.available_at_bounds(
        asset_id="equity:us:aapl",
        source_id="alpaca:bars",
        schema_version="receipt-v1",
    ) == (start, start + timedelta(hours=1))


def test_save_many_is_byte_identical_to_per_record_save(tmp_path: Path) -> None:
    records = [
        make_raw_record(record_id=uuid4()).model_copy(
            update={"payload": {"index": i, "val": f"test_{i}"}}
        )
        for i in range(5)
    ]
    with LocalStorage(StoragePaths.from_root(tmp_path / "single")) as storage_single:
        for record in records:
            storage_single.raw_records.save(record)

        with LocalStorage(StoragePaths.from_root(tmp_path / "batch")) as storage_batch:
            receipt = storage_batch.raw_records.save_many(records)
            assert receipt.created_ids == tuple(r.record_id for r in records)
            assert receipt.reused_ids == ()

            # Re-saving identical records returns all reused
            receipt_reused = storage_batch.raw_records.save_many(records)
            assert receipt_reused.created_ids == ()
            assert receipt_reused.reused_ids == tuple(r.record_id for r in records)

            for record in records:
                path_single = _indexed_path(storage_single, record.record_id)
                path_batch = _indexed_path(storage_batch, record.record_id)

                assert path_single.is_file()
                assert path_batch.is_file()
                assert path_batch.read_bytes() == path_single.read_bytes()

                row_single = storage_single.store.connection.execute(
                    """
                    SELECT relative_path, checksum_sha256, document_json
                    FROM raw_record_index
                    WHERE record_id = ?
                    """,
                    [str(record.record_id)],
                ).fetchone()
                row_batch = storage_batch.store.connection.execute(
                    """
                    SELECT relative_path, checksum_sha256, document_json
                    FROM raw_record_index
                    WHERE record_id = ?
                    """,
                    [str(record.record_id)],
                ).fetchone()
                assert row_batch == row_single

                assert storage_batch.raw_records.get(record.record_id) == record


def test_save_many_uses_a_bounded_number_of_queries(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = [
        make_raw_record(record_id=uuid4()).model_copy(update={"payload": {"i": i}})
        for i in range(20)
    ]

    class _ConnectionProxy:
        def __init__(self, target):
            self._target = target
            self.queries: list[str] = []

        def execute(self, query, *args, **kwargs):
            self.queries.append(str(query).strip())
            return self._target.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._target, name)

    proxy = _ConnectionProxy(storage.raw_records._connection)
    monkeypatch.setattr(storage.raw_records, "_connection", proxy)

    receipt = storage.raw_records.save_many(records)
    assert receipt.created_count == 20
    # 1 SELECT to check existing index + 1 INSERT for new records
    assert len(proxy.queries) == 2
    assert "SELECT" in proxy.queries[0]
    assert "INSERT INTO raw_record_index" in proxy.queries[1]

    proxy.queries.clear()
    receipt_reused = storage.raw_records.save_many(records)
    assert receipt_reused.reused_count == 20
    # For already existing records, only 1 SELECT query is executed
    assert len(proxy.queries) == 1
    assert "SELECT" in proxy.queries[0]


def test_save_many_reports_conflicts_and_preserves_previous_chunks(
    storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(raw_records_module, "_RAW_RECORD_BATCH_CHUNK_SIZE", 2)

    r1 = make_raw_record(record_id=uuid4()).model_copy(update={"payload": {"n": 1}})
    r2 = make_raw_record(record_id=uuid4()).model_copy(update={"payload": {"n": 2}})
    r3 = make_raw_record(record_id=uuid4()).model_copy(update={"payload": {"n": 3}})
    r4 = make_raw_record(record_id=uuid4()).model_copy(update={"payload": {"n": 4}})

    # Save r4 first so that a conflicting version can be supplied in chunk 2
    storage.raw_records.save(r4)
    r4_conflict = r4.model_copy(update={"schema_version": "conflicting-version"})

    # Chunk 1: [r1, r2], Chunk 2: [r3, r4_conflict]
    with pytest.raises(RecordConflictError, match="already has different content"):
        storage.raw_records.save_many([r1, r2, r3, r4_conflict])

    # Chunk 1 was saved before chunk 2 failed and must be preserved
    assert storage.raw_records.get(r1.record_id) == r1
    assert storage.raw_records.get(r2.record_id) == r2

    # Within-chunk duplicate conflict test
    r5 = make_raw_record(record_id=uuid4()).model_copy(update={"payload": {"n": 5}})
    r5_diff = r5.model_copy(update={"schema_version": "conflicting-version"})
    with pytest.raises(RecordConflictError, match="already has different content"):
        storage.raw_records.save_many([r5, r5_diff])


def test_select_13f_ids_before_hydration(tmp_path: Path) -> None:
    from investment_analyst.evidence.sec_institutional_holdings.repository import (
        InstitutionalHoldingsRepository,
        report_to_raw_record,
    )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = _seed_institutional_holdings(repository, name_prefix="select")
        known_at = datetime(2025, 2, 17, tzinfo=UTC)
        selected = storage.raw_records.select_record_ids_by_json_field(
            field="report_manager",
            values=("0001067983",),
            source_id="sec-edgar:institutional-holdings-13f",
            schema_version="sec-institutional-holdings-report-v1",
            available_to=known_at,
        )
        assert set(selected) == {
            report_to_raw_record(report).record_id
            for report in seed["target_reports"]  # type: ignore[union-attr]
        }
        assert (
            storage.raw_records.select_record_ids_by_json_field(field="report_manager", values=())
            == []
        )
        with pytest.raises(StorageError, match="not supported"):
            storage.raw_records.select_record_ids_by_json_field(
                field="payload.report.manager_cik", values=("0001067983",)
            )
