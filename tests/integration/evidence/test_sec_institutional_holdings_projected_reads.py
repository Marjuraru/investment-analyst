"""Projected 13F reads preserve PIT results while skipping unrelated history."""

import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_holdings.models import (
    INSTITUTIONAL_HOLDINGS_SOURCE_ID,
)
from investment_analyst.evidence.sec_institutional_holdings.repository import (
    InstitutionalHoldingsRepository,
    report_to_raw_record,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def test_13f_projected_reads_preserve_pit_and_order(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, "tests/unit/evidence/sec_institutional_holdings")
    import test_repository as holdings_tests

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        repository = InstitutionalHoldingsRepository(storage.raw_records)
        seed = holdings_tests._seed_mixed_corpus(repository, name_prefix="projected")
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
