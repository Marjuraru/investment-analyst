"""Tests for deterministic Apple SEC submissions indexing."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    SUBMISSIONS_SCHEMA_VERSION,
    SUBMISSIONS_SOURCE_ID,
)
from investment_analyst.providers.fundamentals.sec_filing_index import (
    AmbiguousSecFilingError,
    MalformedSecSubmissionsError,
    SecFilingIndex,
)

RETRIEVED_AT = datetime(2026, 7, 13, 18, tzinfo=UTC)


def _document() -> dict[str, object]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "exchanges": ["Nasdaq"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-26-000001",
                    "0000320193-26-000002",
                    "0000320193-26-000003",
                    "0000320193-26-000004",
                    "0000320193-26-000005",
                ],
                "filingDate": [
                    "2026-01-30",
                    "2026-04-30",
                    "2026-05-01",
                    "2026-02-02",
                    "2026-02-10",
                ],
                "reportDate": [
                    "2025-12-31",
                    "2026-03-31",
                    "2026-03-31",
                    "2025-12-31",
                    "2025-12-31",
                ],
                "acceptanceDateTime": [
                    "2026-01-30T21:00:00Z",
                    "2026-04-30T21:00:00-04:00",
                    "2026-05-01T20:00:00Z",
                    "2026-02-02T20:00:00Z",
                    "2026-02-10T20:00:00Z",
                ],
                "form": ["10-K", "10-Q", "10-Q/A", "10-K/A", "8-K"],
                "primaryDocument": [
                    "aapl-20251231x10k.htm",
                    "aapl-20260331x10q.htm",
                    "aapl-20260331x10qa.htm",
                    "aapl-20251231x10ka.htm",
                    "aapl-8k.htm",
                ],
            },
            "files": [],
        },
    }


def _record(*, document: dict[str, object] | None = None, **overrides: object) -> RawRecord:
    values: dict[str, object] = {
        "record_id": uuid4(),
        "asset_id": ASSET_ID,
        "source": SourceReference(
            source_id=SUBMISSIONS_SOURCE_ID,
            retrieved_at=RETRIEVED_AT,
            raw_uri="https://data.sec.gov/submissions/CIK0000320193.json",
            checksum_sha256="a" * 64,
        ),
        "event_time": RETRIEVED_AT,
        "available_at": RETRIEVED_AT,
        "received_at": RETRIEVED_AT,
        "payload": {
            "document_type": "submissions",
            "cik": "0000320193",
            "entity_name": "Apple Inc.",
            "document": document or _document(),
        },
        "schema_version": SUBMISSIONS_SCHEMA_VERSION,
    }
    values.update(overrides)
    return RawRecord.model_validate(values)


def test_valid_index_supports_all_four_forms_and_ignores_others() -> None:
    index = SecFilingIndex.from_raw_record(_record())

    assert [item.form for item in index.all()] == ["10-K", "10-K/A", "10-Q", "10-Q/A"]
    assert index.get("0000320193-26-000001").acceptance_at.tzinfo is UTC
    assert index.get("0000320193-26-000002").acceptance_at == datetime(
        2026,
        5,
        1,
        1,
        tzinfo=UTC,
    )
    assert index.get("missing") is None


def test_unequal_lists_and_missing_columns_are_rejected() -> None:
    unequal = deepcopy(_document())
    unequal["filings"]["recent"]["form"].pop()
    with pytest.raises(MalformedSecSubmissionsError, match="equal lengths"):
        SecFilingIndex.from_raw_record(_record(document=unequal))

    missing = deepcopy(_document())
    del missing["filings"]["recent"]["primaryDocument"]
    with pytest.raises(MalformedSecSubmissionsError, match="primaryDocument"):
        SecFilingIndex.from_raw_record(_record(document=missing))


def test_duplicate_identical_accession_collapses() -> None:
    document = _document()
    recent = document["filings"]["recent"]
    for values in recent.values():
        if isinstance(values, list):
            values.append(values[0])

    index = SecFilingIndex.from_raw_record(_record(document=document))

    assert len(index.all()) == 4


def test_contradictory_accession_is_rejected() -> None:
    document = _document()
    recent = document["filings"]["recent"]
    recent["accessionNumber"].append(recent["accessionNumber"][0])
    recent["filingDate"].append("2026-01-31")
    recent["reportDate"].append(recent["reportDate"][0])
    recent["acceptanceDateTime"].append("2026-01-31T21:00:00Z")
    recent["form"].append(recent["form"][0])
    recent["primaryDocument"].append(recent["primaryDocument"][0])

    with pytest.raises(AmbiguousSecFilingError, match="contradictory"):
        SecFilingIndex.from_raw_record(_record(document=document))


@pytest.mark.parametrize(
    "record",
    [
        _record(asset_id="equity:us:other"),
        _record(schema_version="wrong"),
        _record(
            source=SourceReference(
                source_id="wrong",
                retrieved_at=RETRIEVED_AT,
                checksum_sha256="a" * 64,
            )
        ),
        _record(payload={"document_type": "submissions"}),
    ],
)
def test_invalid_scope_or_payload_is_rejected(record: RawRecord) -> None:
    with pytest.raises(MalformedSecSubmissionsError):
        SecFilingIndex.from_raw_record(record)


def test_order_is_acceptance_then_accession() -> None:
    items = SecFilingIndex.from_raw_record(_record()).all()
    assert items == tuple(
        sorted(items, key=lambda item: (item.acceptance_at, item.accession_number))
    )
