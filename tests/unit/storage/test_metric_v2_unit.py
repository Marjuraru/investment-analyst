"""Unit tests for the typed metric v2 staging table."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.errors import RecordConflictError
from investment_analyst.storage.metric_v2 import (
    METRIC_V2_TABLE,
    MetricV2Error,
    MetricV2Store,
    ensure_metric_v2_tables,
    metric_to_row,
    row_to_metric,
)

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _observation_pair(index: int) -> tuple[RawRecord, NormalizedObservation]:
    moment = _BASE + timedelta(hours=index)
    raw = RawRecord(
        record_id=uuid4(),
        asset_id="crypto:btc-usd",
        source=SourceReference(
            source_id="deribit:funding", record_key=f"m-{index}", retrieved_at=moment
        ),
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"v": str(index)},
        schema_version="metric-v2-unit",
    )
    observation = NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=raw.record_id,
        asset_id="crypto:btc-usd",
        field_name="funding_rate",
        value=Decimal(f"0.0{index % 10}"),
        unit="rate",
        frequency=DataFrequency.HOUR_1,
        observed_at=moment,
        available_at=moment,
        normalized_at=moment,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    return raw, observation


def _connection(tmp_path: Path) -> object:
    connection = duckdb.connect(str(tmp_path / "metric-v2.duckdb"))
    connection.execute(
        "CREATE TABLE raw_v2_index (record_id VARCHAR PRIMARY KEY, source_id VARCHAR)"
    )
    connection.execute(
        "CREATE TABLE normalized_observations_v2 ("
        "observation_id VARCHAR PRIMARY KEY, asset_id VARCHAR, available_at VARCHAR)"
    )
    ensure_metric_v2_tables(connection, create=True)
    return connection


def _seed_observations(connection: object, count: int) -> list[NormalizedObservation]:
    observations: list[NormalizedObservation] = []
    for index in range(count):
        raw, observation = _observation_pair(index)
        connection.execute(
            "INSERT INTO raw_v2_index VALUES (?, ?)",
            [str(raw.record_id), raw.source.source_id],
        )
        connection.execute(
            "INSERT INTO normalized_observations_v2 VALUES (?, ?, ?)",
            [str(observation.observation_id), observation.asset_id, moment_text(observation)],
        )
        observations.append(observation)
    return observations


def moment_text(observation: NormalizedObservation) -> str:
    from datetime import UTC as _UTC

    assert observation.available_at.tzinfo is not None
    return observation.available_at.astimezone(_UTC).isoformat()


def _metric(
    observations: list[NormalizedObservation], *, key: str = "funding.sum_1h"
) -> MetricResult:
    latest = max(observations, key=lambda item: item.available_at)
    candidate = MetricResult(
        result_id=uuid4(),
        asset_id="crypto:btc-usd",
        metric_key=key,
        value=Decimal("1.5"),
        unit="rate",
        as_of=_BASE + timedelta(hours=len(observations)),
        available_at=latest.available_at,
        computed_at=latest.available_at,
        parameters={"window": len(observations)},
        input_observation_ids=[item.observation_id for item in observations],
        algorithm_version="metric-v2-unit",
        quality=DataQuality.VALID,
    )
    return candidate.model_copy(update={"result_id": metric_result_id_from_model_v2(candidate)})


def test_metric_identity_decimal_lineage_and_reuse(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    observations = _seed_observations(connection, 3)
    store = MetricV2Store(connection)
    result = _metric(observations)
    receipt = store.save_many([result])
    assert receipt.created_count == 1
    assert receipt.reused_count == 0
    hydrated = store.get_many([result.result_id])[result.result_id]
    assert hydrated == result
    assert str(hydrated.value) == "1.5"
    columns = {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{METRIC_V2_TABLE}'"
        ).fetchall()
    }
    assert "document_json" not in columns
    rerun = result.model_copy(update={"computed_at": result.computed_at + timedelta(hours=1)})
    rerun = rerun.model_copy(update={"result_id": metric_result_id_from_model_v2(rerun)})
    assert rerun.result_id == result.result_id
    repeated = store.save_many([rerun])
    assert repeated.created_count == 0
    assert repeated.reused_count == 1
    row, obs_ids, met_ids = metric_to_row(result)
    assert row_to_metric(tuple(row), observation_ids=obs_ids, metric_ids=met_ids) == result
    connection.close()


def test_same_identity_different_value_or_order_fails_closed(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    observations = _seed_observations(connection, 3)
    store = MetricV2Store(connection)
    result = _metric(observations)
    assert store.save_many([result]).created_count == 1
    conflict = result.model_copy(update={"value": result.value + Decimal("1")})
    with pytest.raises(RecordConflictError, match="different content"):
        store.save_many([conflict])
    reordered = result.model_copy(
        update={"input_observation_ids": list(reversed(result.input_observation_ids))}
    )
    with pytest.raises((MetricV2Error, RecordConflictError), match="preimage|order|content"):
        store.save_many([reordered])
    assert store.get_many([result.result_id])[result.result_id] == result
    connection.close()


def test_missing_future_or_foreign_reference_fails_closed(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    observations = _seed_observations(connection, 2)
    store = MetricV2Store(connection)
    ghost = _metric(observations).model_copy(
        update={"input_observation_ids": [uuid4(), *observations[1:]]}
    )
    ghost = ghost.model_copy(update={"result_id": metric_result_id_from_model_v2(ghost)})
    with pytest.raises(MetricV2Error, match="missing observation"):
        store.save_many([ghost])
    future_id = uuid4()
    future_moment = observations[0].available_at + timedelta(days=30)
    connection.execute(
        "INSERT INTO normalized_observations_v2 VALUES (?, ?, ?)",
        [str(future_id), "crypto:btc-usd", future_moment.astimezone(UTC).isoformat()],
    )
    candidate = _metric(observations).model_copy(
        update={"input_observation_ids": [future_id, observations[1].observation_id]}
    )
    candidate = candidate.model_copy(
        update={"result_id": metric_result_id_from_model_v2(candidate)}
    )
    with pytest.raises(MetricV2Error, match="future observation"):
        store.save_many([candidate])
    connection.close()
