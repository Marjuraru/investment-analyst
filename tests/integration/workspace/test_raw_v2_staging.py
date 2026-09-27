"""Raw v2 staging matches v1 identity, order and PIT on scratch corpora."""

import os
import time
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.core.models import RawRecord, SourceReference
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
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    outcome_to_raw_record,
    position_to_raw_record,
    report_to_raw_record,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_holdings_parser import (
    parse_institutional_holdings,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_v2 import RawV2Staging, RawV2StagingError

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


def _generic_record(index: int) -> RawRecord:
    available = datetime(2026, 7, 10, 16, 1, tzinfo=UTC) + timedelta_days(index)
    received = datetime(2026, 7, 10, 16, 3, tzinfo=UTC) + timedelta_days(index)
    return RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="test:staging-corpus",
            record_key=f"staging-{index}",
            retrieved_at=received,
        ),
        event_time=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        available_at=available,
        received_at=received,
        payload={"close": "210.50", "sequence": index},
        schema_version="staging-corpus-v1",
    )


def timedelta_days(index: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(days=index)


def _seed_corpus() -> tuple[
    list[RawRecord], list[InstitutionalHoldingsReport], list[InstitutionalHoldingPosition]
]:
    parsed_at = datetime(2025, 2, 17, tzinfo=UTC)
    first = _filing(cik="0001067983", accession_suffix=1, accepted_day=14)
    second = _filing(cik="0001067983", accession_suffix=2, accepted_day=16)
    other = _filing(cik="0001234567", accession_suffix=3, accepted_day=15)
    records: list[RawRecord] = [_generic_record(index) for index in range(6)]
    reports: list[InstitutionalHoldingsReport] = []
    positions: list[InstitutionalHoldingPosition] = []
    for index, filing in enumerate((first, second, other)):
        report, table_positions = _report(filing, f"staging-{index}", parsed_at)
        reports.append(report)
        positions.extend(table_positions)
        records.append(report_to_raw_record(report))
        records.extend(position_to_raw_record(position) for position in table_positions)
    for index, filing in enumerate((first, second)):
        records.append(
            outcome_to_raw_record(_outcome(filing, f"staging-doc-{index}.xml", parsed_at))
        )
    future = _generic_record(60).model_copy(
        update={
            "available_at": datetime(2026, 9, 1, 16, 1, tzinfo=UTC),
            "received_at": datetime(2026, 9, 1, 16, 3, tzinfo=UTC),
        }
    )
    records.append(future)
    revised = reports[0].model_copy(
        update={"manager_name": "Manager LLC Revised", "parsed_entry_total": 2}
    )
    return records, [reports[0], revised], positions


def _tree_bytes(root: Path) -> tuple[int, int, dict[str, int]]:
    total = 0
    files = 0
    by_kind: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        files += 1
        suffix = path.suffix or "none"
        by_kind[suffix] = by_kind.get(suffix, 0) + size
    return total, files, by_kind


def test_raw_v2_matches_v1_identity_order_and_pit(tmp_path: Path) -> None:
    records, _, _ = _seed_corpus()
    first_cut = datetime(2025, 2, 15, 12, tzinfo=UTC)
    full_cut = datetime(2025, 2, 17, tzinfo=UTC)

    with LocalStorage(StoragePaths.from_root(tmp_path / "v1")) as storage:
        storage.raw_records.save_many(records)
        v1_early = [record.record_id for record in storage.raw_records.list(available_to=first_cut)]
        v1_full = [record.record_id for record in storage.raw_records.list(available_to=full_cut)]
        v1_manager = storage.raw_records.select_record_ids_by_json_field(
            field="report_manager",
            values=("0001067983",),
            available_to=full_cut,
        )
        v1_documents = {
            record.record_id: record for record in storage.raw_records.list(available_to=full_cut)
        }
        v1_root = tmp_path / "v1"
        started = time.perf_counter()
        assert set(v1_documents) == set(v1_full)
        v1_elapsed_ms = (time.perf_counter() - started) * 1000.0

    destination = (tmp_path / "v2").absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(destination, connection)
    with staging:
        started = time.perf_counter()
        receipt = staging.save_many(records)
        assert receipt.created_count == len(records)
        v2_elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert [record for record in staging.list_record_ids(available_to=first_cut)] == v1_early
        assert [record for record in staging.list_record_ids(available_to=full_cut)] == v1_full
        v2_reports = staging.list_record_ids(
            manager_cik="0001067983",
            schema_version="sec-institutional-holdings-report-v1",
            available_to=full_cut,
        )
        assert sorted(v2_reports, key=str) == sorted(v1_manager, key=str)
        assert staging.get_many(v1_full) == v1_documents
        repeated = staging.save_many(records)
        assert repeated.created_count == 0
        assert repeated.reused_count == len(records)
        conflict = records[0].model_copy(update={"schema_version": "conflict-v1"})
        with pytest.raises(Exception, match="different content"):
            staging.save(conflict)
        assert staging.get(records[0].record_id) == records[0]
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'raw_v2_index'"
            ).fetchall()
        }
        assert "document_json" not in columns
    staging.close()

    v1_bytes, v1_files, _ = _tree_bytes(v1_root / "data" / "raw")
    v2_bytes, v2_files, _ = _tree_bytes(destination / "raw")
    v1_db = (v1_root / "data" / "processed" / "investment_analyst.duckdb").stat().st_size
    v2_db = (destination / "raw-v2-index.duckdb").stat().st_size
    wal = list(destination.glob("*.wal"))
    index_rows = connection.execute("SELECT count(*) FROM raw_v2_index").fetchone()[0]
    assert index_rows == len(records)
    assert v2_files == len(records)
    print(
        f"raw_v2_staging: v1_raw_bytes={v1_bytes} v1_files={v1_files} v1_db={v1_db} "
        f"v2_raw_bytes={v2_bytes} v2_files={v2_files} v2_db={v2_db} "
        f"v2_wal={len(wal)} v1_ms={v1_elapsed_ms:.1f} v2_ms={v2_elapsed_ms:.1f}"
    )


def test_raw_v2_rejects_tampering_and_symlinks(tmp_path: Path) -> None:
    records, _, _ = _seed_corpus()
    destination = (tmp_path / "v2").absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(destination, connection)
    with staging:
        staging.save_many(records[:4])
        row = connection.execute(
            "SELECT relative_path, checksum_sha256 FROM raw_v2_index WHERE record_id = ?",
            [str(records[0].record_id)],
        ).fetchone()
        assert row is not None
        blob = destination / "raw" / Path(row[0])
        blob.write_text('{"tampered":true}', encoding="utf-8")
        with pytest.raises(RawV2StagingError, match="checksum mismatch"):
            staging.get(records[0].record_id)
        connection.execute(
            "UPDATE raw_v2_index SET projected_manager_cik = ? WHERE record_id = ?",
            ["0000000000", str(records[1].record_id)],
        )
        blob1 = (
            destination
            / "raw"
            / Path(
                connection.execute(
                    "SELECT relative_path FROM raw_v2_index WHERE record_id = ?",
                    [str(records[1].record_id)],
                ).fetchone()[0]
            )
        )
        assert blob1.is_file()
        with pytest.raises(RawV2StagingError, match="projection"):
            staging.get(records[1].record_id)
        connection.execute(
            "UPDATE raw_v2_index SET projected_manager_cik = NULL WHERE record_id = ?",
            [str(records[1].record_id)],
        )
        assert staging.get(records[1].record_id) == records[1]
    staging.close()

    (tmp_path / "link-target").mkdir()
    linked = tmp_path / "linked"
    os.symlink(tmp_path / "link-target", linked)
    with pytest.raises(RawV2StagingError, match="symbolic link"):
        RawV2Staging(linked.absolute(), duckdb.connect(":memory:")).open()
    (tmp_path / "markerless").mkdir()
    (tmp_path / "markerless" / "stray.txt").write_text("stray", encoding="utf-8")
    with pytest.raises(RawV2StagingError, match="no staging marker"):
        RawV2Staging((tmp_path / "markerless").absolute(), duckdb.connect(":memory:")).open()
    (tmp_path / "bad-marker").mkdir()
    (tmp_path / "bad-marker" / "raw-v2-staging.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RawV2StagingError, match="incompatible"):
        RawV2Staging((tmp_path / "bad-marker").absolute(), duckdb.connect(":memory:")).open()
