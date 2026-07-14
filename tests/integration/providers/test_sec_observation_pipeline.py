"""Integration tests for offline Apple SEC fundamental observation persistence."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
)
from investment_analyst.core.models.source import SourceReference
from investment_analyst.providers.fundamentals.sec_companyfacts_normalizer import (
    SecCompanyFactsNormalizer,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarClient, SecEdgarIdentity
from investment_analyst.providers.fundamentals.sec_observation_pipeline import (
    SecAaplObservationPipeline,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    create_sec_aapl_asset,
    get_sec_source_definitions,
    sec_document_to_raw_record,
)
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, RecordNotFoundError, StoragePaths

FIXTURE_DIR = Path("tests/fixtures/sec")
FIRST_RETRIEVAL = datetime(2026, 7, 13, 18, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 14, 12, tzinfo=UTC)
ANNUAL_ACCN = "0000320193-26-000001"
QUARTERLY_ACCN = "0000320193-26-000002"


class FixtureTransport:
    """Offline transport for two prepared SEC documents."""

    def __init__(self, submissions: bytes, companyfacts: bytes) -> None:
        self._bodies = [submissions, companyfacts]

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        return HttpResponse(200, self._bodies.pop(0), {}, url)


def _load_fixture(name: str) -> dict[str, object]:
    return json.loads(
        (FIXTURE_DIR / name).read_text(),
        parse_int=str,
        parse_float=str,
    )


def _prepared_documents(
    *,
    marker: str,
    annual_assets: str = "5000",
) -> tuple[bytes, bytes]:
    submissions = _load_fixture("aapl_submissions.json")
    recent = submissions["filings"]["recent"]
    recent["acceptanceDateTime"] = [
        "2026-01-30T21:00:00Z",
        "2026-04-30T21:00:00Z",
    ]
    recent["primaryDocument"] = [
        "aapl-20251231x10k.htm",
        "aapl-20260331x10q.htm",
    ]

    def duration(annual: bool, value: str) -> dict[str, str]:
        return {
            "start": "2025-01-01" if annual else "2026-01-01",
            "end": "2025-12-31" if annual else "2026-03-31",
            "val": value,
            "accn": ANNUAL_ACCN if annual else QUARTERLY_ACCN,
            "fy": "2025" if annual else "2026",
            "fp": "FY" if annual else "Q1",
            "form": "10-K" if annual else "10-Q",
            "filed": "2026-01-30" if annual else "2026-04-30",
        }

    def instant(annual: bool, value: str) -> dict[str, str]:
        result = duration(annual, value)
        del result["start"]
        return result

    companyfacts = _load_fixture("aapl_companyfacts.json")
    companyfacts["description"] = marker
    companyfacts["facts"] = {
        "us-gaap": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {
                "units": {"USD": [duration(True, "1000.25"), duration(False, "260.50")]}
            },
            "NetIncomeLoss": {
                "units": {"USD": [duration(True, "200.10"), duration(False, "52.25")]}
            },
            "Assets": {"units": {"USD": [instant(True, annual_assets), instant(False, "5100")]}},
            "Liabilities": {"units": {"USD": [instant(True, "3000"), instant(False, "3050")]}},
            "StockholdersEquity": {
                "units": {"USD": [instant(True, "2000"), instant(False, "2050")]}
            },
        },
        "dei": companyfacts["facts"]["dei"],
    }
    return (
        json.dumps(submissions, separators=(",", ":"), sort_keys=True).encode(),
        json.dumps(companyfacts, separators=(",", ":"), sort_keys=True).encode(),
    )


def _store_snapshots(
    storage: LocalStorage,
    *,
    retrieved_at: datetime,
    marker: str,
    annual_assets: str = "5000",
) -> tuple[RawRecord, RawRecord]:
    submissions, companyfacts = _prepared_documents(
        marker=marker,
        annual_assets=annual_assets,
    )
    client = SecEdgarClient(
        FixtureTransport(submissions, companyfacts),
        SecEdgarIdentity("Investment Analyst integration@example.com"),
        sleep=lambda _: None,
        clock=lambda: retrieved_at,
    )
    records = tuple(
        sec_document_to_raw_record(document)
        for document in client.fetch_aapl_issuer_documents().documents
    )
    storage.assets.upsert(create_sec_aapl_asset())
    for source in get_sec_source_definitions():
        storage.sources.upsert(source)
    for record in records:
        try:
            storage.raw_records.get(record.record_id)
        except RecordNotFoundError:
            storage.raw_records.save(record)
    return records


def _store_market_observation(storage: LocalStorage) -> NormalizedObservation:
    timestamp = datetime(2026, 7, 10, tzinfo=UTC)
    raw = RawRecord(
        asset_id="crypto:btc-usd",
        source=SourceReference(
            source_id="simulated:daily-bars",
            retrieved_at=timestamp,
            checksum_sha256="c" * 64,
        ),
        event_time=timestamp,
        available_at=timestamp,
        received_at=timestamp,
        payload={"simulated": "true"},
        schema_version="simulated-bars-v1",
    )
    storage.raw_records.save(raw)
    observation = NormalizedObservation(
        raw_record_id=raw.record_id,
        asset_id="crypto:btc-usd",
        field_name="close",
        value="100",
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=timestamp,
        available_at=timestamp,
        normalized_at=timestamp,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="simulated-normalizer-v1",
    )
    storage.observations.save(observation)
    return observation


def test_pipeline_creates_reuses_and_versions_observations(tmp_path: Path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        market_observation = _store_market_observation(storage)
        _store_snapshots(
            storage,
            retrieved_at=FIRST_RETRIEVAL,
            marker="first synthetic snapshot",
        )
        pipeline = SecAaplObservationPipeline(
            storage,
            SecCompanyFactsNormalizer(),
            clock=lambda: NORMALIZED_AT,
        )
        first = pipeline.run()

        assert first.filings_indexed == 2
        assert first.facts_selected == 10
        assert first.observations_created == 10
        assert first.observations_reused == 0
        assert first.annual_count == 5
        assert first.quarterly_count == 5
        assert first.raw_records_created == 0
        assert first.metric_results_created == 0
        assert first.diagnostics_created == 0
        assert first.traceability_verified is True
        first_ids = {
            item.observation_id for item in storage.observations.list(asset_id="equity:us:aapl")
        }

        second = pipeline.run()
        assert second.observations_created == 0
        assert second.observations_reused == 10
        assert {
            item.observation_id for item in storage.observations.list(asset_id="equity:us:aapl")
        } == first_ids

        _store_snapshots(
            storage,
            retrieved_at=FIRST_RETRIEVAL + timedelta(hours=1),
            marker="same facts in a newer snapshot",
        )
        same_facts = pipeline.run()
        assert same_facts.observations_created == 0
        assert same_facts.observations_reused == 10

        _store_snapshots(
            storage,
            retrieved_at=FIRST_RETRIEVAL + timedelta(hours=2),
            marker="corrected annual assets",
            annual_assets="5001",
        )
        corrected = pipeline.run()
        assert corrected.observations_created == 1
        assert corrected.observations_reused == 9
        assert len(storage.observations.list(asset_id="equity:us:aapl")) == 11
        assert storage.observations.get(market_observation.observation_id) == market_observation
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []
        assert len(storage.raw_records.list(source_id="sec-edgar:aapl:companyfacts")) == 3
        assert json.loads(json.dumps(corrected.to_json_dict()))["facts_selected"] == 10
