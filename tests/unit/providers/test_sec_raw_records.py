"""Unit tests for SEC source definitions and raw snapshot conversion."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.core.models import Asset, AssetClass, SourceType
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecDocumentType,
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    ASSET_ID,
    COMPANY_FACTS_SOURCE_ID,
    SUBMISSIONS_SOURCE_ID,
    create_sec_aapl_asset,
    create_sec_asset,
    get_sec_source_definitions,
    sec_document_to_raw_record,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.serialization import canonical_json_text

SUBMISSIONS = Path("tests/fixtures/sec/aapl_submissions.json").read_bytes()
COMPANY_FACTS = Path("tests/fixtures/sec/aapl_companyfacts.json").read_bytes()
FIRST_TIME = datetime(2026, 7, 13, 18, tzinfo=UTC)
SECOND_TIME = FIRST_TIME + timedelta(days=1)


class FixtureTransport:
    """Offline two-document transport."""

    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = list(bodies)

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        return HttpResponse(200, self.bodies.pop(0), {}, url)


def _documents(*, retrieved_at: datetime = FIRST_TIME, company: bytes = COMPANY_FACTS):
    client = SecEdgarClient(
        FixtureTransport([SUBMISSIONS, company]),
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        sleep=lambda _: None,
        clock=lambda: retrieved_at,
    )
    return client.fetch_aapl_issuer_documents().documents


def test_sources_are_official_and_fundamental() -> None:
    submissions, company_facts = get_sec_source_definitions()

    assert submissions.source_id == SUBMISSIONS_SOURCE_ID
    assert company_facts.source_id == COMPANY_FACTS_SOURCE_ID
    assert submissions.source_type is SourceType.FUNDAMENTALS
    assert company_facts.source_type is SourceType.FUNDAMENTALS
    assert submissions.is_official is True
    assert company_facts.is_official is True
    assert "snapshots" in submissions.coverage_notes
    assert "XBRL" in company_facts.coverage_notes


def test_asset_preserves_alpaca_symbol_and_adds_sec_cik() -> None:
    existing = Asset(
        asset_id=ASSET_ID,
        symbol="AAPL",
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
        provider_symbols={"alpaca_iex": "AAPL"},
        is_active=True,
    )

    updated = create_sec_aapl_asset(existing)

    assert updated.provider_symbols == {
        "alpaca_iex": "AAPL",
        "sec_cik": "0000320193",
    }


def test_second_issuer_uses_distinct_asset_sources_and_record_ids() -> None:
    configuration = SecAssetConfiguration(
        asset_id="equity:us:amd",
        cik="0000002488",
        ticker="AMD",
        submissions_source_id="sec-edgar:amd:submissions",
        companyfacts_source_id="sec-edgar:amd:companyfacts",
        name="Advanced Micro Devices, Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )
    submissions = json.loads(SUBMISSIONS)
    submissions.update(
        {
            "cik": "2488",
            "name": configuration.name,
            "tickers": ["AMD"],
        }
    )
    company_facts = json.loads(COMPANY_FACTS)
    company_facts.update(
        {
            "cik": configuration.cik,
            "entityName": configuration.name,
        }
    )
    client = SecEdgarClient(
        FixtureTransport(
            [
                json.dumps(submissions).encode(),
                json.dumps(company_facts).encode(),
            ]
        ),
        SecEdgarIdentity("Investment Analyst tests@example.com"),
        cik=configuration.cik,
        ticker=configuration.ticker,
        sleep=lambda _: None,
        clock=lambda: FIRST_TIME,
    )
    records = tuple(
        sec_document_to_raw_record(document, configuration)
        for document in client.fetch_issuer_documents().documents
    )
    sources = get_sec_source_definitions(configuration)

    assert create_sec_asset(configuration).asset_id == "equity:us:amd"
    assert {record.asset_id for record in records} == {"equity:us:amd"}
    assert {record.source.source_id for record in records} == {
        "sec-edgar:amd:submissions",
        "sec-edgar:amd:companyfacts",
    }
    assert {source.source_id for source in sources} == {
        "sec-edgar:amd:submissions",
        "sec-edgar:amd:companyfacts",
    }
    assert all("Advanced Micro Devices" in source.dataset_name for source in sources)
    apple_ids = {sec_document_to_raw_record(document).record_id for document in _documents()}
    assert apple_ids.isdisjoint({record.record_id for record in records})


def test_uuid_is_stable_ignores_retrieval_time_and_changes_with_content() -> None:
    first_documents = _documents(retrieved_at=FIRST_TIME)
    later_documents = _documents(retrieved_at=SECOND_TIME)
    revised_body = COMPANY_FACTS.replace(b"1000.25", b"1001.25")
    revised_documents = _documents(retrieved_at=SECOND_TIME, company=revised_body)

    first_records = {doc.document_type: sec_document_to_raw_record(doc) for doc in first_documents}
    later_records = {doc.document_type: sec_document_to_raw_record(doc) for doc in later_documents}
    revised_records = {
        doc.document_type: sec_document_to_raw_record(doc) for doc in revised_documents
    }

    assert (
        first_records[SecDocumentType.SUBMISSIONS].record_id
        == later_records[SecDocumentType.SUBMISSIONS].record_id
    )
    assert (
        first_records[SecDocumentType.COMPANY_FACTS].record_id
        == later_records[SecDocumentType.COMPANY_FACTS].record_id
    )
    assert (
        first_records[SecDocumentType.COMPANY_FACTS].record_id
        != revised_records[SecDocumentType.COMPANY_FACTS].record_id
    )


def test_raw_record_payload_schema_source_and_checksum_are_complete() -> None:
    records = [sec_document_to_raw_record(document) for document in _documents()]

    for record in records:
        assert record.asset_id == ASSET_ID
        assert record.event_time == FIRST_TIME
        assert record.available_at == FIRST_TIME
        assert record.received_at == FIRST_TIME
        assert isinstance(record.payload, dict)
        assert record.payload["document"]
        assert record.payload["body_sha256"] == record.source.checksum_sha256
        assert record.source.raw_uri is not None
        assert record.source.raw_uri.startswith("https://data.sec.gov/")
        assert "tests@example.com" not in canonical_json_text(record)
    assert records[0].schema_version == "sec-edgar-submissions-snapshot-v1"
    assert records[1].schema_version == "sec-edgar-companyfacts-snapshot-v1"


def test_raw_record_serialization_and_storage_round_trip(tmp_path: Path) -> None:
    records = [sec_document_to_raw_record(document) for document in _documents()]

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        for record in records:
            storage.raw_records.save(record)
            assert storage.raw_records.get(record.record_id) == record

    serialized = json.loads(records[1].model_dump_json())
    assert serialized["source"]["checksum_sha256"] == records[1].source.checksum_sha256
    assert serialized["payload"]["document"]["cik"] == "0000320193"
