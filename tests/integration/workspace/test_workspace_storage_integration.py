"""Integration tests for real workspace storage in read-write and read-only modes."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticComponent,
    DiagnosticEvidence,
    DiagnosticMode,
    DiagnosticResult,
    DiagnosticVerdict,
    EvidenceDirection,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_RECORD_ID = UUID("10000000-0000-4000-8000-000000000001")
_OBSERVATION_ID = UUID("20000000-0000-4000-8000-000000000002")
_METRIC_ID = UUID("30000000-0000-4000-8000-000000000003")
_DIAGNOSTIC_ID = UUID("40000000-0000-4000-8000-000000000004")
_EVENT_TIME = datetime(2026, 7, 10, 16, 0, tzinfo=UTC)
_AVAILABLE_AT = datetime(2026, 7, 10, 16, 1, tzinfo=UTC)
_COMPUTED_AT = datetime(2026, 7, 10, 16, 5, tzinfo=UTC)


def _source() -> SourceReference:
    return SourceReference(
        source_id="test:workspace",
        record_key="workspace-fixture",
        retrieved_at=_AVAILABLE_AT,
    )


def _raw_record() -> RawRecord:
    return RawRecord(
        record_id=_RECORD_ID,
        asset_id="equity:us:aapl",
        source=_source(),
        event_time=_EVENT_TIME,
        available_at=_AVAILABLE_AT,
        received_at=_AVAILABLE_AT,
        payload={"close": "210.50"},
        schema_version="workspace-test-v1",
    )


def _observation() -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=_OBSERVATION_ID,
        raw_record_id=_RECORD_ID,
        asset_id="equity:us:aapl",
        field_name="close",
        value=Decimal("210.50"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_EVENT_TIME,
        available_at=_AVAILABLE_AT,
        normalized_at=_COMPUTED_AT,
        source=_source(),
        quality=DataQuality.VALID,
        transformation_version="workspace-test-v1",
    )


def _metric() -> MetricResult:
    return MetricResult(
        result_id=_METRIC_ID,
        asset_id="equity:us:aapl",
        metric_key="test.workspace.metric",
        value=Decimal("1"),
        unit="ratio",
        as_of=_EVENT_TIME,
        available_at=_AVAILABLE_AT,
        computed_at=_COMPUTED_AT,
        parameters={"fixture": "workspace"},
        input_observation_ids=[_OBSERVATION_ID],
        algorithm_version="workspace-test-v1",
        quality=DataQuality.VALID,
    )


def _diagnostic() -> DiagnosticResult:
    component = DiagnosticComponent(
        component_key="workspace_component",
        score=Decimal("60"),
        weight=Decimal("1"),
        weighted_contribution=Decimal("60"),
        metric_result_ids=[_METRIC_ID],
        explanation="Workspace integration fixture.",
    )
    evidence = DiagnosticEvidence(
        metric_result_id=_METRIC_ID,
        direction=EvidenceDirection.SUPPORTS,
        contribution=Decimal("0.2"),
        reason="Workspace integration fixture.",
    )
    return DiagnosticResult(
        diagnostic_id=_DIAGNOSTIC_ID,
        asset_id="equity:us:aapl",
        mode=DiagnosticMode.MARKET,
        verdict=DiagnosticVerdict.NEUTRAL,
        final_score=Decimal("60"),
        confidence=Decimal("0.5"),
        as_of=_EVENT_TIME,
        available_at=_AVAILABLE_AT,
        computed_at=_COMPUTED_AT,
        components=[component],
        evidence=[evidence],
        algorithm_version="workspace-test-v1",
        summary="Workspace integration fixture.",
        quality=DataQuality.VALID,
    )


def _file_state(root: Path) -> dict[str, tuple[int, int, str]]:
    state: dict[str, tuple[int, int, str]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        stat = path.stat()
        state[str(path.relative_to(root))] = (
            len(data),
            stat.st_mtime_ns,
            hashlib.sha256(data).hexdigest(),
        )
    return state


def test_workspace_storage_round_trip_and_read_only_inspection(tmp_path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialization = service.initialize(tmp_path / "workspace")

    writer = service.open_storage(initialization.paths, WorkspaceAccessMode.READ_WRITE)
    try:
        writer.raw_records.save(_raw_record())
        writer.observations.save(_observation())
        writer.metric_results.save(_metric())
        writer.diagnostics.save(_diagnostic())
    finally:
        writer.close()

    before = _file_state(initialization.paths.root)
    inspection = service.inspect(initialization.paths.root)
    after = _file_state(initialization.paths.root)

    assert inspection.status == "ready"
    assert inspection.raw_record_count == 1
    assert inspection.observation_count == 1
    assert inspection.metric_result_count == 1
    assert inspection.diagnostic_result_count == 1
    assert before == after

    first_reader = service.open_storage(initialization.paths, WorkspaceAccessMode.READ_ONLY)
    second_reader = service.open_storage(initialization.paths, WorkspaceAccessMode.READ_ONLY)
    try:
        assert first_reader.raw_records.get(_RECORD_ID) == _raw_record()
        assert second_reader.metric_results.get(_METRIC_ID) == _metric()
    finally:
        second_reader.close()
        first_reader.close()

    repeated = service.initialize(initialization.paths.root)
    assert repeated.reused
    assert repeated.manifest.workspace_id == initialization.manifest.workspace_id
    assert service.inspect(initialization.paths.root).to_json_dict() == inspection.to_json_dict()
