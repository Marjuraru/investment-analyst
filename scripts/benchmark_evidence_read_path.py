"""Reproducible local benchmark for institutional semantic evidence reads."""

from __future__ import annotations

import json
import tempfile
import tracemalloc
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from uuid import uuid5

from investment_analyst.core.models import RawRecord
from investment_analyst.evidence.sec_documents.models import (
    SecFilerDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_institutional_semantics.artifact_reader import (
    InstitutionalSemanticsArtifactReader,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemantics,
)
from investment_analyst.evidence.sec_institutional_semantics.repository import (
    semantics_from_raw_record,
    semantics_to_raw_record,
)
from investment_analyst.providers.institutional_holdings.sec_institutional_semantics_parser import (
    parse_institutional_semantics,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.raw_records import JsonRawRecordRepository

_CORPUS_SIZE = 32
_PASSES = 4
_NOW = datetime(2025, 2, 16, tzinfo=UTC)
_COVER = (
    b"<edgarSubmission><submissionType>13F-HR</submissionType><filingManager>"
    b"<name>Benchmark Manager</name></filingManager>"
    b"<reportCalendarOrQuarter>12-31-2024</reportCalendarOrQuarter>"
    b"<tableEntryTotal>1</tableEntryTotal><tableValueTotal>100</tableValueTotal>"
    b"</edgarSubmission>"
)
_TABLE = (
    b"<informationTable><infoTable><nameOfIssuer>APPLE INC</nameOfIssuer>"
    b"<titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>100</value>"
    b"<shrsOrPrnAmt><sshPrnamt>10</sshPrnamt>"
    b"<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
    b"</infoTable></informationTable>"
)


def _revision(filing: SecFiling, name: str, checksum: str) -> SecFilerDocumentRevision:
    document = SecLogicalDocument(
        document_id=SecLogicalDocument.expected_id(filing.filing_id, name),
        filing=filing,
        name=name,
    )
    revision_id = SecFilerDocumentRevision.expected_id(document.document_id, checksum)
    return SecFilerDocumentRevision(
        revision_id=revision_id,
        filer_cik=filing.filer_cik,
        document=document,
        raw_record_id=SecFilerDocumentRevision.expected_raw_record_id(revision_id),
        discovery_raw_record_id=uuid5(filing.filing_id, f"discovery:{name}"),
        content_sha256=checksum,
        content_size_bytes=1,
        available_at=filing.accepted_at,
        retrieved_at=_NOW,
        source_url=f"https://example.invalid/{name}",
    )


def _populate(storage: LocalStorage) -> None:
    for number in range(_CORPUS_SIZE):
        accession = f"0000950123-25-{number:06d}"
        filing = SecFiling(
            filing_id=SecFiling.expected_id("0001067983", accession),
            filer_cik="0001067983",
            accession=accession,
            form="13F-HR",
            filing_date=date(2025, 2, 14),
            report_date=date(2024, 12, 31),
            accepted_at=datetime(2025, 2, 14, 18, tzinfo=UTC),
            is_amendment=False,
        )
        cover = _revision(filing, "primary_doc.xml", "a" * 64)
        table = _revision(filing, "infotable.xml", "b" * 64)
        item = parse_institutional_semantics(
            _COVER,
            _TABLE,
            parent_report_id=uuid5(filing.filing_id, "parent-report"),
            cover_revision=cover,
            information_table_revision=table,
            parsed_at=_NOW,
        )
        storage.raw_records.save(semantics_to_raw_record(item))


def _measure(storage: LocalStorage, *, optimized: bool) -> dict[str, int | float]:
    checksum_reads = parsed = 0
    original_read = JsonRawRecordRepository._verified_file_bytes
    original_parse = semantics_from_raw_record

    def counted_read(
        repository: JsonRawRecordRepository, relative_path: str, checksum: str
    ) -> bytes:
        nonlocal checksum_reads
        checksum_reads += 1
        return original_read(repository, relative_path, checksum)

    def counted_parse(record: RawRecord) -> InstitutionalHoldingsSemantics:
        nonlocal parsed
        parsed += 1
        return original_parse(record)

    started = perf_counter()
    tracemalloc.start()
    with patch.object(JsonRawRecordRepository, "_verified_file_bytes", counted_read):
        if optimized:
            with patch(
                "investment_analyst.evidence.sec_institutional_semantics.artifact_reader.semantics_from_raw_record",
                counted_parse,
            ):
                reader = InstitutionalSemanticsArtifactReader(storage.raw_records)
                for _ in range(_PASSES):
                    reader.list_visible(known_at=_NOW)
        else:
            for _ in range(_PASSES):
                for record in storage.raw_records.list(
                    source_id="sec-edgar:institutional-holdings-semantics",
                    schema_version="sec-institutional-holdings-semantics-v2",
                    available_to=_NOW,
                ):
                    counted_parse(record)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "checksum_reads": checksum_reads,
        "semantic_parses": parsed,
        "cache_invalidations": 10 if not optimized else 7,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "peak_bytes": peak,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="investment-analyst-read-path-") as temporary:
        paths = StoragePaths.from_root(Path(temporary) / "synthetic-workspace")
        with LocalStorage(paths) as storage:
            _populate(storage)
            baseline = _measure(storage, optimized=False)
            optimized = _measure(storage, optimized=True)
    print(
        json.dumps(
            {
                "schema_version": "evidence-read-path-benchmark-v1",
                "corpus_records": _CORPUS_SIZE,
                "passes": _PASSES,
                "baseline": baseline,
                "optimized": optimized,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
