"""Focused persistence checks for daily market recursive checkpoints."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
    make_daily_evidence_prefix,
    observation_rows_digest,
)
from investment_analyst.analytics.market.incremental_state import (
    EmaParameters,
    advance_checkpoint,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.market_checkpoint_v2 import MarketCheckpointV2Error
from investment_analyst.storage.raw_v2 import RawV2Staging

_ASSET_ID = "equity:us:test"
_SOURCE_ID = "simulated:daily-bars"
_TIMESTAMP = datetime(2026, 1, 5, 16, tzinfo=UTC)


def _stage_bar(staging: RawV2Staging) -> tuple[MarketBar, DailyEvidencePrefix]:
    available_at = _TIMESTAMP + timedelta(hours=1)
    raw = RawRecord(
        record_id=uuid4(),
        asset_id=_ASSET_ID,
        source=SourceReference(
            source_id=_SOURCE_ID,
            record_key="checkpoint-unit:bar-0",
            retrieved_at=available_at,
        ),
        event_time=_TIMESTAMP,
        available_at=available_at,
        received_at=available_at,
        payload={"close": "101.25"},
        schema_version="checkpoint-unit-v1",
    )
    staging.save(raw)
    close_observation = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw.record_id,
        asset_id=_ASSET_ID,
        field_name="close",
        value=Decimal("101.25"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_TIMESTAMP,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="checkpoint-unit-normalizer-v1",
    )
    staging.save_observations([close_observation])
    prefix = make_daily_evidence_prefix(
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        field_group=DailyEvidenceFieldGroup.CLOSE,
        timestamp=_TIMESTAMP,
        observation_ids=(close_observation.observation_id,),
        observation_digest=observation_rows_digest(
            [[str(close_observation.observation_id), str(close_observation.value)]]
        ),
        current_available_at=available_at,
        quality=DataQuality.VALID,
    )
    bar = MarketBar(
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        raw_record_id=raw.record_id,
        frequency=DataFrequency.DAY_1,
        timestamp=_TIMESTAMP,
        available_at=available_at,
        open=Decimal("100.00"),
        high=Decimal("103.00"),
        low=Decimal("99.00"),
        close=Decimal("101.25"),
        volume=Decimal("1200"),
        quality=DataQuality.VALID,
        observation_ids={
            "open": uuid4(),
            "high": uuid4(),
            "low": uuid4(),
            "close": close_observation.observation_id,
            "volume": uuid4(),
        },
    )
    return bar, prefix


def _staging(tmp_path: Path) -> tuple[RawV2Staging, duckdb.DuckDBPyConnection]:
    destination = (tmp_path / "raw-v2").absolute()
    destination.mkdir()
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection), connection


def test_checkpoint_store_round_trip_is_idempotent_and_detects_tampering(
    tmp_path: Path,
) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        bar, prefix = _stage_bar(staging)
        staging.save_daily_evidence_prefixes([prefix])
        checkpoint = advance_checkpoint(None, bar, prefix, EmaParameters(window=3))

        created = staging.save_market_recursive_checkpoints([checkpoint])
        reused = staging.save_market_recursive_checkpoints([checkpoint])
        assert created.created_ids == (checkpoint.checkpoint_id,)
        assert reused.reused_ids == (checkpoint.checkpoint_id,)
        assert staging.get_market_recursive_checkpoints([checkpoint.checkpoint_id]) == {
            checkpoint.checkpoint_id: checkpoint
        }

        tampered_state = checkpoint.state.model_dump(mode="json")
        tampered_state["bars_seen"] = 2
        connection.execute(
            "UPDATE market_recursive_checkpoints_v2 SET state_json = ? WHERE checkpoint_id = ?",
            [json.dumps(tampered_state), str(checkpoint.checkpoint_id)],
        )
        with pytest.raises(MarketCheckpointV2Error, match="does not validate"):
            staging.get_market_recursive_checkpoints([checkpoint.checkpoint_id])
    connection.close()


def test_checkpoint_store_rejects_missing_daily_prefix(tmp_path: Path) -> None:
    staging, connection = _staging(tmp_path)
    with staging:
        bar, prefix = _stage_bar(staging)
        staging.save_daily_evidence_prefixes([])
        checkpoint = advance_checkpoint(None, bar, prefix, EmaParameters(window=3))
        with pytest.raises(MarketCheckpointV2Error, match="missing daily evidence prefix"):
            staging.save_market_recursive_checkpoints([checkpoint])
    connection.close()
