"""Integration coverage for the direct v1-to-v2 migration path."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.historical_analytical_archive import (
    historical_analytical_archive_exists,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import migrate_workspace_v1_to_v2

_MOMENT = datetime(2026, 10, 7, tzinfo=UTC)


def test_direct_migration_preserves_models_and_document_bytes(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source_root = tmp_path / "source"
    source = service.initialize(source_root)
    reference = SourceReference(
        source_id="fixture:integration-migration",
        record_key="one",
        retrieved_at=_MOMENT,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "integration-migration-raw"),
        asset_id="equity:us:aapl",
        source=reference,
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"value": "1"},
        schema_version="integration-migration-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, "integration-migration-observation"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="close",
        value=Decimal("1.2300"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_MOMENT,
        available_at=_MOMENT,
        normalized_at=_MOMENT,
        source=reference,
        quality=DataQuality.VALID,
        transformation_version="integration-migration-v1",
    )
    metric = MetricResult(
        result_id=uuid5(NAMESPACE_URL, "integration-migration-metric"),
        asset_id=raw.asset_id,
        metric_key="market.close_copy",
        value=Decimal("1.2300"),
        unit="USD",
        as_of=_MOMENT,
        available_at=_MOMENT,
        computed_at=_MOMENT,
        parameters={"known_at": _MOMENT.isoformat()},
        input_observation_ids=[observation.observation_id],
        algorithm_version="integration-migration-v1",
        quality=DataQuality.VALID,
    )
    with service.open_storage(source.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
        storage.metric_results.save(metric)
        document = storage.documents.put(b"immutable document")

    interrupted = False

    def interrupt_after_direct_commit(point: str) -> None:
        nonlocal interrupted
        if point == "after_metric_page_write" and not interrupted:
            interrupted = True
            raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError, match="fixture interruption"):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source_root,
            destination_root=tmp_path / "destination",
            page_limit=1,
            reserve_bytes=0,
            host_free_bytes=lambda: 30_000_000_000,
            analytical_failure_injector=interrupt_after_direct_commit,
        )

    receipt = migrate_workspace_v1_to_v2(
        service=service,
        source_root=source_root,
        destination_root=tmp_path / "destination",
        page_limit=1,
        reserve_bytes=0,
        host_free_bytes=lambda: 30_000_000_000,
    )

    with service.open_storage(
        service.resolve(receipt.destination_root), WorkspaceAccessMode.READ_ONLY
    ) as target:
        assert target.raw_records.get(raw.record_id) == raw
        assert target.observations.get(observation.observation_id) == observation
        assert target.metric_results.get(metric.result_id) == metric
        assert target.documents.read(document.sha256) == b"immutable document"
        assert historical_analytical_archive_exists(target.store.connection) is False
    assert receipt.document_count == 1
