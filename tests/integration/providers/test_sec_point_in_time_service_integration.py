"""Integration tests for persisted SEC point-in-time observations."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    COMPANYFACTS_SOURCE_ID,
    TRANSFORMATION_VERSION,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
)
from investment_analyst.providers.fundamentals.sec_query_models import SecFundamentalQuery
from investment_analyst.storage import LocalStorage, StoragePaths


def _persisted_observation(
    *,
    field_name: str,
    value: str,
    period_end: date,
    available_at: datetime,
    accession: str,
) -> NormalizedObservation:
    raw_record_id = uuid4()
    key = {
        "accession_number": accession,
        "taxonomy": "us-gaap",
        "tag": {
            "fundamental.revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
            "fundamental.net_income": "NetIncomeLoss",
            "fundamental.assets": "Assets",
            "fundamental.liabilities": "Liabilities",
            "fundamental.stockholders_equity": "StockholdersEquity",
        }[field_name],
        "unit": "USD",
        "period": period_end.isoformat(),
        "companyfacts_record_id": str(raw_record_id),
        "submissions_record_id": str(uuid4()),
    }
    instant = field_name in {
        "fundamental.assets",
        "fundamental.liabilities",
        "fundamental.stockholders_equity",
    }
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_record_id,
        asset_id=ASSET_ID,
        field_name=field_name,
        value=Decimal(value),
        unit="USD",
        frequency=DataFrequency.ANNUAL,
        observed_at=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        period_start=None if instant else datetime(2024, 9, 29, tzinfo=UTC),
        period_end=datetime.combine(period_end, datetime.min.time(), tzinfo=UTC),
        available_at=available_at,
        normalized_at=available_at + timedelta(days=30),
        source=SourceReference(
            source_id=COMPANYFACTS_SOURCE_ID,
            record_key=json.dumps(key, separators=(",", ":"), sort_keys=True),
            retrieved_at=available_at + timedelta(days=1),
        ),
        quality=DataQuality.VALID,
        transformation_version=TRANSFORMATION_VERSION,
    )


def test_persisted_observations_produce_deterministic_read_only_views(tmp_path) -> None:
    period_end = date(2025, 9, 27)
    accepted = datetime(2025, 10, 31, 20, tzinfo=UTC)
    observations = [
        _persisted_observation(
            field_name="fundamental.revenue",
            value="100",
            period_end=period_end,
            available_at=accepted,
            accession="original",
        ),
        _persisted_observation(
            field_name="fundamental.revenue",
            value="105",
            period_end=period_end,
            available_at=accepted + timedelta(days=5),
            accession="amendment",
        ),
        _persisted_observation(
            field_name="fundamental.assets",
            value="200",
            period_end=period_end,
            available_at=accepted,
            accession="original",
        ),
    ]
    paths = StoragePaths.from_root(tmp_path)
    request = SecFundamentalQuery(
        known_at=accepted + timedelta(days=5),
        frequency=DataFrequency.ANNUAL,
    )

    with LocalStorage(paths) as storage:
        for observation in observations:
            storage.observations.save(observation)
        before = storage.observations.list(asset_id=ASSET_ID)
        first = SecAaplFundamentalPointInTimeService(storage).query(request)
        second = SecAaplFundamentalPointInTimeService(storage).query(request)
        after = storage.observations.list(asset_id=ASSET_ID)

    assert first.to_json_dict() == second.to_json_dict()
    assert before == after
    assert first.periods[0].facts[1].value == Decimal("105")
    assert first.periods[0].facts[1].superseded_count == 1
    assert first.periods[0].available_fields == (
        "fundamental.assets",
        "fundamental.revenue",
    )
    assert first.traceability_verified
