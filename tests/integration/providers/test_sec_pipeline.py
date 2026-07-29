"""Offline integration tests for the configurable SEC raw-document pipeline."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.core.models import Asset, AssetClass
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarIdentity,
)
from investment_analyst.providers.fundamentals.sec_pipeline import (
    SecAaplFundamentalsPipeline,
    SecIssuerFundamentalsPipeline,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    ASSET_ID,
    COMPANY_FACTS_SOURCE_ID,
    SUBMISSIONS_SOURCE_ID,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

SUBMISSIONS = Path("tests/fixtures/sec/aapl_submissions.json").read_bytes()
COMPANY_FACTS = Path("tests/fixtures/sec/aapl_companyfacts.json").read_bytes()
FIRST_TIME = datetime(2026, 7, 13, 18, tzinfo=UTC)
SECOND_TIME = FIRST_TIME + timedelta(hours=1)


class FixtureTransport:
    """Offline transport returning exactly two configured SEC documents."""

    def __init__(self, submissions: bytes, company_facts: bytes) -> None:
        self.bodies = [submissions, company_facts]
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append((url, dict(headers)))
        return HttpResponse(200, self.bodies.pop(0), {}, url)


def _pipeline(
    storage: LocalStorage,
    *,
    retrieved_at: datetime,
    company_facts: bytes = COMPANY_FACTS,
) -> tuple[SecAaplFundamentalsPipeline, FixtureTransport]:
    transport = FixtureTransport(SUBMISSIONS, company_facts)
    client = SecEdgarClient(
        transport,
        SecEdgarIdentity("Investment Analyst integration@example.com"),
        sleep=lambda _: None,
        clock=lambda: retrieved_at,
    )
    return SecAaplFundamentalsPipeline(storage, client), transport


def test_complete_round_trip_idempotence_and_asset_merge(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        storage.assets.upsert(
            Asset(
                asset_id=ASSET_ID,
                symbol="AAPL",
                name="Apple Inc.",
                asset_class=AssetClass.EQUITY,
                quote_currency="USD",
                exchange="NASDAQ",
                provider_symbols={"alpaca_iex": "AAPL"},
                is_active=True,
            )
        )
        first_pipeline, transport = _pipeline(storage, retrieved_at=FIRST_TIME)
        first = first_pipeline.run()

        assert first.documents_received == 2
        assert first.raw_records_created == 2
        assert first.raw_records_reused == 0
        assert first.content_changed is False
        assert first.traceability_verified is True
        assert len(transport.calls) == 2
        assert len(storage.raw_records.list(source_id=SUBMISSIONS_SOURCE_ID)) == 1
        assert len(storage.raw_records.list(source_id=COMPANY_FACTS_SOURCE_ID)) == 1
        assert len(storage.sources.list_all()) == 2
        assert storage.assets.get(ASSET_ID).provider_symbols == {
            "alpaca_iex": "AAPL",
            "sec_cik": "0000320193",
        }
        assert storage.observations.list() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []

        original_submissions = storage.raw_records.get(first.submissions_record_id)
        original_company = storage.raw_records.get(first.companyfacts_record_id)
        second_pipeline, _ = _pipeline(storage, retrieved_at=SECOND_TIME)
        second = second_pipeline.run()

        assert second.raw_records_created == 0
        assert second.raw_records_reused == 2
        assert second.submissions_record_id == first.submissions_record_id
        assert second.companyfacts_record_id == first.companyfacts_record_id
        assert storage.raw_records.get(second.submissions_record_id).received_at == (
            original_submissions.received_at
        )
        assert storage.raw_records.get(second.companyfacts_record_id).received_at == (
            original_company.received_at
        )
        assert json.loads(json.dumps(second.to_json_dict()))["documents_received"] == 2


def test_company_facts_revision_creates_one_new_version_and_keeps_previous(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        first_pipeline, _ = _pipeline(storage, retrieved_at=FIRST_TIME)
        first = first_pipeline.run()
        revised = COMPANY_FACTS.replace(b"1000.25", b"1002.25")
        revised_pipeline, _ = _pipeline(
            storage,
            retrieved_at=SECOND_TIME,
            company_facts=revised,
        )
        second = revised_pipeline.run()

        assert second.raw_records_created == 1
        assert second.raw_records_reused == 1
        assert second.content_changed is True
        assert second.submissions_record_id == first.submissions_record_id
        assert second.companyfacts_record_id != first.companyfacts_record_id
        company_records = storage.raw_records.list(source_id=COMPANY_FACTS_SOURCE_ID)
        assert len(company_records) == 2
        assert {record.record_id for record in company_records} == {
            first.companyfacts_record_id,
            second.companyfacts_record_id,
        }
        assert storage.raw_records.get(first.companyfacts_record_id)
        assert storage.observations.list() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []


def test_two_issuers_are_idempotent_and_isolated_in_one_workspace(tmp_path: Path) -> None:
    amd_configuration = SecAssetConfiguration(
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
    amd_submissions = json.loads(SUBMISSIONS)
    amd_submissions.update(
        {
            "cik": "2488",
            "name": amd_configuration.name,
            "tickers": ["AMD"],
        }
    )
    amd_company_facts = json.loads(COMPANY_FACTS)
    amd_company_facts.update(
        {
            "cik": amd_configuration.cik,
            "entityName": amd_configuration.name,
        }
    )

    def amd_pipeline(storage: LocalStorage, retrieved_at: datetime):
        transport = FixtureTransport(
            json.dumps(amd_submissions).encode(),
            json.dumps(amd_company_facts).encode(),
        )
        client = SecEdgarClient(
            transport,
            SecEdgarIdentity("Investment Analyst integration@example.com"),
            cik=amd_configuration.cik,
            ticker=amd_configuration.ticker,
            sleep=lambda _: None,
            clock=lambda: retrieved_at,
        )
        return SecIssuerFundamentalsPipeline(
            storage,
            client,
            configuration=amd_configuration,
        )

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        apple, _ = _pipeline(storage, retrieved_at=FIRST_TIME)
        apple_summary = apple.run()
        amd_first = amd_pipeline(storage, FIRST_TIME).run()
        amd_second = amd_pipeline(storage, SECOND_TIME).run()

        assert amd_first.asset_id == amd_configuration.asset_id
        assert amd_first.cik == amd_configuration.cik
        assert amd_first.raw_records_created == 2
        assert amd_second.raw_records_created == 0
        assert amd_second.raw_records_reused == 2
        assert len(storage.assets.list_all()) == 2
        assert len(storage.sources.list_all()) == 4
        assert len(storage.raw_records.list()) == 4
        assert storage.assets.get(amd_configuration.asset_id).provider_symbols == {
            "sec_cik": amd_configuration.cik
        }
        assert storage.raw_records.get(apple_summary.submissions_record_id).asset_id == ASSET_ID
        assert {
            record.asset_id
            for record in storage.raw_records.list(
                source_id=amd_configuration.companyfacts_source_id
            )
        } == {amd_configuration.asset_id}
        assert storage.observations.list() == []
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []
