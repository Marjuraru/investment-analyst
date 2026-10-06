"""Contract tests for the isolated historical analytical archive."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import duckdb
import pytest

from investment_analyst.core.models import (
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
)
from investment_analyst.storage.errors import RecordConflictError
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchive,
    HistoricalAnalyticalArchiveError,
    HistoricalAnalyticalCursor,
)
from investment_analyst.storage.metric_v2 import recalculate_metric_result_id

_NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _uuid(label: str) -> UUID:
    return uuid5(NAMESPACE_URL, label)


def _metric(
    index: int = 0,
    *,
    observations: tuple[UUID, ...] | None = None,
    metric_inputs: tuple[UUID, ...] = (),
) -> MetricResult:
    return MetricResult(
        result_id=_uuid(f"metric-{index}"),
        asset_id="equity:us:aapl",
        metric_key="price.return_1d",
        value=Decimal("1.2300"),
        unit="ratio",
        as_of=_NOW,
        available_at=_NOW + timedelta(seconds=index),
        computed_at=_NOW + timedelta(days=1, seconds=index),
        parameters={"known_at": "2026-01-02T00:00:00Z", "run_id": "run-unchanged"},
        input_observation_ids=list(
            observations if observations is not None else (_uuid("observation-base"),)
        ),
        input_metric_result_ids=list(metric_inputs),
        algorithm_version="test-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic(metric_id: UUID) -> DiagnosticResult:
    component = DiagnosticComponent(
        component_key="trend",
        score=Decimal("10.00"),
        weight=Decimal("1.0"),
        weighted_contribution=Decimal("10.000"),
        metric_result_ids=[metric_id],
        explanation="fixture component",
    )
    evidence = DiagnosticEvidence(
        metric_result_id=metric_id,
        direction=EvidenceDirection.SUPPORTS,
        contribution=Decimal("10.000"),
        reason="fixture evidence",
    )
    return DiagnosticResult(
        diagnostic_id=_uuid("diagnostic-main"),
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.POSITIVE,
        final_score=Decimal("10.000"),
        confidence=Decimal("0.8000"),
        as_of=_NOW,
        available_at=_NOW + timedelta(seconds=3),
        computed_at=_NOW + timedelta(days=1),
        components=[component],
        evidence=[evidence],
        algorithm_version="diagnostic-v1",
        summary="transparent fixture",
        quality=DataQuality.VALID,
    )


def test_metric_round_trip_preserves_uuid5_decimal_parameters_and_shared_720_links() -> None:
    connection = duckdb.connect(":memory:")
    archive = HistoricalAnalyticalArchive(connection)
    observation_ids = tuple(_uuid(f"shared-observation-{index}") for index in range(720))
    records = tuple(_metric(index, observations=observation_ids) for index in range(100))

    created, reused = archive.save_metrics(records)

    assert len(created) == 100
    assert reused == ()
    assert archive.get_metrics([records[0].result_id]) == {records[0].result_id: records[0]}
    assert (
        connection.execute("SELECT count(*) FROM historical_analytical_sequences").fetchone()[0]
        == 2
    )
    assert (
        connection.execute("SELECT count(*) FROM historical_analytical_segments").fetchone()[0] == 3
    )
    assert (
        connection.execute("SELECT count(*) FROM historical_analytical_segment_members").fetchone()[
            0
        ]
        == 720
    )
    assert connection.execute(
        "SELECT value_text, parameters_json, id_version "
        "FROM historical_metric_results WHERE result_id = ?",
        [str(records[0].result_id)],
    ).fetchone() == (
        "1.2300",
        '{"known_at":"2026-01-02T00:00:00Z","run_id":"run-unchanged"}',
        5,
    )


def test_keyset_pages_use_available_at_and_known_to_inclusive_cut() -> None:
    connection = duckdb.connect(":memory:")
    archive = HistoricalAnalyticalArchive(connection)
    records = tuple(_metric(index) for index in range(5))
    archive.save_metrics(records)

    first = archive.list_metrics_page(limit=2, known_to=_NOW + timedelta(seconds=3))
    cursor = HistoricalAnalyticalCursor(
        available_at=first[-1].available_at,
        identifier=first[-1].result_id,
    )
    second = archive.list_metrics_page(limit=2, known_to=_NOW + timedelta(seconds=3), after=cursor)

    assert [item.result_id for item in first + second] == [item.result_id for item in records[:4]]
    assert (
        archive.list_metrics_page(
            limit=2, asset_id="equity:us:missing", known_to=_NOW + timedelta(days=1)
        )
        == ()
    )
    with pytest.raises(ValueError):
        archive.list_metrics_page(limit=True)
    with pytest.raises(ValueError):
        archive.list_metrics_page(limit=257)


def test_conflict_in_computed_at_is_not_hidden_by_matching_result_id() -> None:
    connection = duckdb.connect(":memory:")
    archive = HistoricalAnalyticalArchive(connection)
    original = _metric()
    archive.save_metrics([original])
    changed = original.model_copy(
        update={"computed_at": original.computed_at + timedelta(seconds=1)}
    )

    with pytest.raises(RecordConflictError):
        archive.save_metrics([changed])


def test_uuid8_identity_uses_the_public_metric_v2_validator() -> None:
    connection = duckdb.connect(":memory:")
    archive = HistoricalAnalyticalArchive(connection)
    candidate = _metric().model_copy(update={"result_id": _uuid("placeholder-uuid8")})
    valid = candidate.model_copy(update={"result_id": recalculate_metric_result_id(candidate)})
    archive.save_metrics([valid])
    assert archive.get_metrics([valid.result_id]) == {valid.result_id: valid}

    invalid = valid.model_copy(update={"parameters": {"window": 99}})
    with pytest.raises(HistoricalAnalyticalArchiveError, match="UUIDv8 metric identity"):
        archive.save_metrics([invalid])


def test_diagnostic_round_trip_preserves_ordered_typed_children() -> None:
    connection = duckdb.connect(":memory:")
    archive = HistoricalAnalyticalArchive(connection)
    metric = _metric()
    archive.save_metrics([metric])
    diagnostic = _diagnostic(metric.result_id)

    assert archive.save_diagnostics([diagnostic]) == ((diagnostic.diagnostic_id,), ())
    assert archive.get_diagnostics([diagnostic.diagnostic_id]) == {
        diagnostic.diagnostic_id: diagnostic
    }


def test_sequence_corruption_is_rejected_on_hydration() -> None:
    connection = duckdb.connect(":memory:")
    archive = HistoricalAnalyticalArchive(connection)
    metric = _metric(observations=(_uuid("observation-corrupt"),))
    archive.save_metrics([metric])
    connection.execute(
        "UPDATE historical_analytical_segment_members SET identifier = ?",
        [str(_uuid("different-observation"))],
    )

    with pytest.raises(HistoricalAnalyticalArchiveError):
        archive.get_metrics([metric.result_id])


def test_archive_validation_checks_point_in_time_links_and_metric_graph() -> None:
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE normalized_observations_v2 "
        "(observation_id VARCHAR, asset_id VARCHAR, available_at VARCHAR)"
    )
    observation_id = _uuid("validation-observation")
    connection.execute(
        "INSERT INTO normalized_observations_v2 VALUES (?, ?, ?)",
        [str(observation_id), "equity:us:aapl", _NOW.isoformat()],
    )
    archive = HistoricalAnalyticalArchive(connection)
    first = _metric(0, observations=(observation_id,))
    second = _metric(1, observations=(observation_id,), metric_inputs=(first.result_id,))
    archive.save_metrics([first, second])
    diagnostic = _diagnostic(second.result_id)
    archive.save_diagnostics([diagnostic])

    summary = archive.verify_complete()

    assert summary.complete is True
    assert summary.metric_count == 2
    assert summary.diagnostic_count == 1
    assert summary.metric_observation_links == 2
    assert summary.metric_metric_links == 1
    assert summary.diagnostic_component_links == 1
    assert summary.diagnostic_evidence_links == 1


def test_archive_validation_rejects_metric_dependency_cycles() -> None:
    connection = duckdb.connect(":memory:")
    observation_id = _uuid("cycle-observation")
    connection.execute(
        "CREATE TABLE normalized_observations_v2 "
        "(observation_id VARCHAR, asset_id VARCHAR, available_at VARCHAR)"
    )
    connection.execute(
        "INSERT INTO normalized_observations_v2 VALUES (?, ?, ?)",
        [str(observation_id), "equity:us:aapl", _NOW.isoformat()],
    )
    first = _metric(0, observations=(observation_id,))
    second = _metric(1, observations=(observation_id,)).model_copy(
        update={"available_at": first.available_at}
    )
    first = first.model_copy(update={"input_metric_result_ids": [second.result_id]})
    second = second.model_copy(update={"input_metric_result_ids": [first.result_id]})
    archive = HistoricalAnalyticalArchive(connection)
    archive.save_metrics([first, second])

    with pytest.raises(HistoricalAnalyticalArchiveError, match="cycle"):
        archive.verify_complete()
