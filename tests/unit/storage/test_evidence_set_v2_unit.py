"""Unit tests for the typed evidence set v2 staging tables."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest

from investment_analyst.analytics.evidence_set import (
    build_evidence_segments,
    build_evidence_set,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    SourceReference,
)
from investment_analyst.storage.errors import RecordConflictError
from investment_analyst.storage.evidence_set_v2 import (
    EVIDENCE_SEGMENT_V2_TABLE,
    EVIDENCE_SET_V2_TABLE,
    EvidenceSetV2Error,
    EvidenceSetV2Store,
    ensure_evidence_v2_tables,
    row_to_evidence_set,
    row_to_segment,
)

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _observations(count: int) -> list[NormalizedObservation]:
    observations: list[NormalizedObservation] = []
    for index in range(count):
        moment = _BASE + timedelta(hours=index)
        source = SourceReference(
            source_id="deribit:funding", record_key=f"e-{index}", retrieved_at=moment
        )
        observations.append(
            NormalizedObservation(
                observation_id=uuid4(),
                raw_record_id=uuid4(),
                asset_id="crypto:btc-usd",
                field_name="funding_rate",
                value=Decimal(f"0.00{index % 10}"),
                unit="rate",
                frequency=DataFrequency.HOUR_1,
                observed_at=moment,
                available_at=moment,
                normalized_at=moment,
                source=source,
                quality=DataQuality.VALID,
                transformation_version="1.0.0",
            )
        )
    return observations


def _connection(tmp_path: Path, observations: list[NormalizedObservation]) -> object:
    connection = duckdb.connect(str(tmp_path / "evidence-v2.duckdb"))
    connection.execute(
        "CREATE TABLE normalized_observations_v2 ("
        "observation_id VARCHAR PRIMARY KEY, asset_id VARCHAR, source_id VARCHAR, "
        "field_name VARCHAR, available_at VARCHAR)"
    )
    for observation in observations:
        connection.execute(
            "INSERT INTO normalized_observations_v2 VALUES (?, ?, ?, ?, ?)",
            [
                str(observation.observation_id),
                observation.asset_id,
                observation.source.source_id,
                observation.field_name,
                observation.available_at.astimezone(UTC).isoformat(),
            ],
        )
    ensure_evidence_v2_tables(connection, create=True)
    return connection


def test_shared_hourly_lineage_roundtrip_without_repeated_arrays(tmp_path: Path) -> None:
    observations = _observations(48)
    connection = _connection(tmp_path, observations)
    store = EvidenceSetV2Store(connection)
    segments = build_evidence_segments(observations)
    assert len(segments) == 2
    assert store.save_segments(segments) == 2
    assert store.save_segments(segments) == 0
    evidence_set = build_evidence_set(observations, segments=segments)
    assert store.save_set(evidence_set) is True
    assert store.save_set(evidence_set) is False
    hydrated = store.get_set(evidence_set.evidence_set_id)
    assert hydrated == evidence_set
    resolved = store.verify_set_lineage(evidence_set)
    assert [str(item) for item in resolved] == [str(item.observation_id) for item in observations]
    columns = {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SET_V2_TABLE}'"
        ).fetchall()
    }
    assert "document_json" not in columns
    segment_columns = {
        row[0]
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{EVIDENCE_SEGMENT_V2_TABLE}'"
        ).fetchall()
    }
    assert "document_json" not in segment_columns
    connection.close()


def test_corrupt_segment_or_set_identity_fails_closed(tmp_path: Path) -> None:
    observations = _observations(48)
    connection = _connection(tmp_path, observations)
    store = EvidenceSetV2Store(connection)
    segments = list(build_evidence_segments(observations))
    assert store.save_segments(segments) == 2
    tampered = segments[0].model_copy(update={"canonical_hash": "0" * 64})
    with pytest.raises((RecordConflictError, EvidenceSetV2Error)):
        store.save_segments([tampered])
    evidence_set = build_evidence_set(observations, segments=segments)
    assert store.save_set(evidence_set) is True
    stored_row = connection.execute(
        "SELECT evidence_set_id, asset_id, source_id, field_name, input_count, head_offset, "
        "inline_observation_ids_json, inline_available_at, first_observed_at, "
        "first_observation_id, last_observed_at, last_observation_id, available_at, "
        "canonical_hash FROM evidence_sets_v2"
    ).fetchone()
    members = connection.execute(
        "SELECT segment_id FROM evidence_set_v2_members ORDER BY position"
    ).fetchall()
    from uuid import UUID as _UUID

    assert (
        row_to_evidence_set(
            tuple(stored_row), segment_ids=[_UUID(str(item[0])) for item in members]
        )
        == evidence_set
    )
    stored_segment = connection.execute(
        "SELECT segment_id, asset_id, source_id, field_name, day, observation_ids_json, "
        "available_at, canonical_hash FROM evidence_segments_v2 ORDER BY segment_id LIMIT 1"
    ).fetchone()
    assert row_to_segment(tuple(stored_segment)) in segments
    connection.close()
