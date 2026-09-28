"""Unit tests for the typed observation v2 staging table."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.errors import RecordConflictError, RecordNotFoundError
from investment_analyst.storage.observation_v2 import (
    ObservationV2Error,
    ensure_observation_v2_table,
    observation_to_row,
    row_to_observation,
)
from investment_analyst.storage.raw_v2 import RawV2Staging

_AVAILABLE = datetime(2026, 7, 10, 16, 1, tzinfo=UTC)
_RECEIVED = datetime(2026, 7, 10, 16, 3, tzinfo=UTC)


def _raw_record() -> RawRecord:
    return RawRecord(
        record_id=uuid4(),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="test:observations",
            record_key="obs-fixture",
            retrieved_at=_RECEIVED,
        ),
        event_time=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        available_at=_AVAILABLE,
        received_at=_RECEIVED,
        payload={"close": "210.50"},
        schema_version="obs-v1",
    )


def _observation(raw_record_id=None, value=Decimal("210.50")) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw_record_id or uuid4(),
        asset_id="equity:us:aapl",
        field_name="close",
        value=value,
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=datetime(2026, 7, 10, 16, 0, tzinfo=UTC),
        available_at=_AVAILABLE,
        normalized_at=datetime(2026, 7, 10, 16, 4, tzinfo=UTC),
        source=SourceReference(
            source_id="test:observations",
            record_key="obs-fixture",
            retrieved_at=_RECEIVED,
        ),
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )


def _staging(tmp_path: Path, name: str = "staging") -> tuple[RawV2Staging, object]:
    destination = (tmp_path / name).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(destination / "obs-v2-index.duckdb"))
    return RawV2Staging(destination, connection), connection


def test_observation_roundtrip_preserves_decimal_identity_and_pit(tmp_path: Path) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        raw_record = _raw_record()
        staging.save(raw_record)
        observation = _observation(raw_record_id=raw_record.record_id)
        receipt = staging.save_observations([observation])
        assert receipt.created_count == 1
        assert receipt.reused_count == 0
        hydrated = staging.get_observations([observation.observation_id])
        assert hydrated[observation.observation_id] == observation
        assert hydrated[observation.observation_id].value == Decimal("210.50")
        assert hydrated[observation.observation_id].available_at.tzinfo is UTC
        repeated = staging.save_observations([observation])
        assert repeated.created_count == 0
        assert repeated.reused_count == 1
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'normalized_observations_v2'"
            ).fetchall()
        }
        assert "document_json" not in columns
        pit = staging.list_observations(asset_id="equity:us:aapl", available_to=_AVAILABLE)
        assert [item.observation_id for item in pit] == [observation.observation_id]
        empty_cut = staging.list_observations(
            asset_id="equity:us:aapl",
            available_to=datetime(2026, 7, 9, tzinfo=UTC),
        )
        assert empty_cut == []
    staging.close()


def test_observation_v2_rejects_same_id_different_content(tmp_path: Path) -> None:
    staging, _ = _staging(tmp_path)
    with staging:
        raw_record = _raw_record()
        staging.save(raw_record)
        observation = _observation(raw_record_id=raw_record.record_id)
        staging.save_observations([observation])
        conflicting = observation.model_copy(update={"value": Decimal("999.00")})
        with pytest.raises(RecordConflictError, match="different content"):
            staging.save_observations([conflicting])
        hydrated = staging.get_observations([observation.observation_id])
        assert hydrated[observation.observation_id] == observation
    staging.close()


def test_observation_v2_row_rejects_invalid_decimal_and_missing_raw(tmp_path: Path) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        raw_record = _raw_record()
        staging.save(raw_record)
        observation = _observation(raw_record_id=raw_record.record_id)
        row = observation_to_row(observation)
        assert row_to_observation(tuple(row)) == observation
        tampered = list(row)
        tampered[4] = "not-a-decimal"
        with pytest.raises(ObservationV2Error, match="Decimal"):
            row_to_observation(tuple(tampered))
        foreign = _observation()
        with pytest.raises(ObservationV2Error, match="missing raw"):
            staging.save_observations([foreign])
        with pytest.raises(RecordNotFoundError, match="was not found"):
            staging.get_observations([uuid4()])
        ensure_observation_v2_table(connection, create=False)
    staging.close()
