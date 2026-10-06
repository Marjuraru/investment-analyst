"""End-to-end coverage of v2 repositories through the application runtime."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from investment_analyst.analytics.cazatiburones.institutional_event_service import (
    InstitutionalEventService,
)
from investment_analyst.analytics.metric_identity_v2 import metric_result_id_from_model_v2
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import SOURCE_ID
from investment_analyst.evidence.sec_institutional_observations.service import (
    InstitutionalObservationService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET = "equity:us:aapl"
_BASE = datetime(2026, 8, 1, tzinfo=UTC)


def _observation(index: int, *, manager: str | None) -> tuple[RawRecord, NormalizedObservation]:
    moment = _BASE + timedelta(minutes=index)
    record_key = json.dumps(
        {"manager_cik": manager, "artifact_id": f"artifact-{index}"},
        separators=(",", ":"),
        sort_keys=True,
    )
    source = SourceReference(
        source_id=SOURCE_ID,
        record_key=record_key,
        retrieved_at=moment,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"workspace-v2-consumer-raw-{index}"),
        asset_id=_ASSET,
        source=source,
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload={"manager_cik": manager, "index": index},
        schema_version="workspace-v2-consumer-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"workspace-v2-consumer-observation-{index}"),
        raw_record_id=raw.record_id,
        asset_id=_ASSET,
        field_name="institutional_reported_shares",
        value=Decimal(f"{100 + index}.0000"),
        unit="shares",
        frequency=DataFrequency.QUARTERLY,
        observed_at=moment,
        period_end=moment,
        available_at=moment,
        normalized_at=moment,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="workspace-v2-consumer-v1",
    )
    return raw, observation


def test_application_runtime_and_institutional_consumers_use_v2(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace_root = tmp_path / "workspace"
    initialized = service.initialize(workspace_root, format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=service)
    request = StorageLocationRequest(workspace=workspace_root)
    pairs = [_observation(0, manager="0001350694"), _observation(1, manager="0009999999")]
    observations = [item for _, item in pairs]
    known_at = _BASE + timedelta(hours=1)

    with runtime.open_storage(request, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save_many([raw for raw, _ in pairs])
        storage.observations.save_many(observations)
        consumer = InstitutionalObservationService(storage)
        selected = consumer.list_for_manager(
            asset_id=_ASSET,
            manager_cik="0001350694",
            known_at=known_at,
            field_name="institutional_reported_shares",
        )
        assert len(selected) == 1
        assert selected[0].observation_id == observations[0].observation_id

        metric = MetricResult(
            result_id=uuid4(),
            asset_id=_ASSET,
            metric_key="cazatiburones.institutional.delta_reported_shares",
            value=Decimal("500.00"),
            unit="shares",
            as_of=observations[0].available_at,
            available_at=observations[0].available_at,
            computed_at=observations[0].available_at,
            parameters={
                "manager_cik": "0001350694",
                "cusip": "037833100",
                "title_of_class": "COM",
                "put_call": None,
                "report_period": "2024-12-31",
                "prior_report_period": "2024-09-30",
            },
            input_observation_ids=[observations[0].observation_id],
            algorithm_version="cazatiburones-institutional-metrics-v1",
            quality=DataQuality.VALID,
        )
        metric = metric.model_copy(update={"result_id": metric_result_id_from_model_v2(metric)})
        storage.metric_results.save(metric)
        event_service = InstitutionalEventService(storage, clock=lambda: known_at)
        summary = event_service.materialize(
            asset_id=_ASSET,
            manager_cik="0001350694",
            known_at=known_at,
        )
        assert summary.created
        assert summary.events == 1
        snapshot_id = summary.snapshot_id

    with runtime.open_storage(request, access_mode=WorkspaceAccessMode.READ_ONLY) as storage:
        read_events = InstitutionalEventService(storage).query(
            asset_id=_ASSET,
            manager_cik="0001350694",
            known_at=known_at,
            snapshot_id_value=snapshot_id,
        )
        assert read_events is not None
        assert len(read_events.events) == 1
        assert read_events.events[0].metric_result_id == metric.result_id
        assert storage.metric_results.get(metric.result_id) == metric

    inspection = service.inspect(workspace_root)
    assert inspection.status == "ready"
    assert inspection.format_version == initialized.manifest.format_version == 2
    assert inspection.raw_record_count == 2
    assert inspection.observation_count == 2
    assert inspection.metric_result_count == 1
