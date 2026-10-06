"""Unit coverage for workspace v2 selection and repository compatibility."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.core.models import (
    Asset,
    AssetClass,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.storage.paths import StoragePaths
from investment_analyst.storage.raw_v2 import RawV2StagingError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import (
    WorkspaceAccessError,
    WorkspaceService,
)

_BASE = datetime(2026, 8, 1, tzinfo=UTC)
_ASSETS = ("equity:us:aapl", "crypto:btc-usd")


def _pair(index: int) -> tuple[RawRecord, NormalizedObservation]:
    moment = _BASE + timedelta(minutes=index)
    asset_id = _ASSETS[index % len(_ASSETS)]
    source_id = "sec:filings" if index == 0 else "test:prices"
    payload: dict[str, object] = {"value": index}
    if index == 0:
        payload = {"report": {"manager_cik": "0000123456"}}
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"workspace-v2-raw-{index}"),
        asset_id=asset_id,
        source=SourceReference(
            source_id=source_id,
            record_key=f"record-{index}",
            retrieved_at=moment,
        ),
        event_time=moment,
        available_at=moment,
        received_at=moment,
        payload=payload,
        schema_version="workspace-v2-unit-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"workspace-v2-observation-{index}"),
        raw_record_id=raw.record_id,
        asset_id=asset_id,
        field_name="close",
        value=Decimal(f"{index}.2300"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=moment,
        period_start=moment,
        period_end=moment,
        available_at=moment,
        normalized_at=moment,
        source=raw.source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    return raw, observation


def _tree_digest(root: Path) -> tuple[tuple[str, int, str], ...]:
    output: list[tuple[str, int, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        output.append(
            (path.relative_to(root).as_posix(), len(payload), hashlib.sha256(payload).hexdigest())
        )
    return tuple(output)


def test_workspace_v2_batches_queries_and_inspects_without_writes(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root = tmp_path / "workspace-v2"
    initialized = service.initialize(root, format_version=2)
    pairs = [_pair(index) for index in range(260)]

    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.assets.upsert(
            Asset(
                asset_id=_ASSETS[0],
                symbol="AAPL",
                name="Apple Inc.",
                asset_class=AssetClass.EQUITY,
                quote_currency="USD",
                exchange="NASDAQ",
            )
        )
        storage.assets.upsert(
            Asset(
                asset_id=_ASSETS[1],
                symbol="BTC-USD",
                name="Bitcoin",
                asset_class=AssetClass.CRYPTO,
                quote_currency="USD",
            )
        )
        storage.sources.upsert(
            SourceDefinition(
                source_id="test:prices",
                provider_name="fixture",
                dataset_name="daily prices",
                source_type=SourceType.MARKET,
                is_official=False,
            )
        )
        storage.sources.upsert(
            SourceDefinition(
                source_id="sec:filings",
                provider_name="fixture",
                dataset_name="filings",
                source_type=SourceType.REGISTRY,
                is_official=True,
            )
        )
        storage.raw_records.save_many([raw for raw, _ in pairs])
        storage.observations.save_many([observation for _, observation in pairs])
        assert storage.raw_records.count() == 260
        assert storage.observations.count(asset_id=_ASSETS[0]) == 130
        assert storage.observations.count(asset_id=_ASSETS[1]) == 130

        listed = storage.observations.list(
            asset_id=_ASSETS[0],
            field_names=("close",),
            frequency=DataFrequency.DAY_1,
            quality=DataQuality.VALID,
            available_from=_BASE,
            available_to=_BASE + timedelta(minutes=258),
            period_end_from=_BASE.date(),
            period_end_to=_BASE.date(),
        )
        assert len(listed) == 130
        assert all(item.asset_id == _ASSETS[0] for item in listed)
        assert storage.observations.observed_at_bounds(asset_id=_ASSETS[0]) == (
            _BASE,
            _BASE + timedelta(minutes=258),
        )

        raw_repository = storage.raw_records
        staging = storage.store.raw_staging
        original_get_many = staging.get_many
        hydration_calls: list[int] = []

        def tracked_get_many(record_ids):
            hydration_calls.append(len(record_ids))
            return original_get_many(record_ids)

        staging.get_many = tracked_get_many
        selected = raw_repository.select_record_ids_by_json_field(
            field="report_manager",
            values=("0000123456",),
            source_id="sec:filings",
            schema_version="workspace-v2-unit-v1",
        )
        assert selected == [pairs[0][0].record_id]
        assert hydration_calls == []
        storage.store.connection.execute(
            "UPDATE workspace_raw_json_projections_v2 SET field_value = ? "
            "WHERE record_id = ? AND field_name = 'report_manager'",
            ["0000999999", str(selected[0])],
        )
        with pytest.raises(RawV2StagingError, match="projection does not match"):
            raw_repository.get(selected[0])
        storage.store.connection.execute(
            "UPDATE workspace_raw_json_projections_v2 SET field_value = ? "
            "WHERE record_id = ? AND field_name = 'report_manager'",
            ["0000123456", str(selected[0])],
        )

    before_read = _tree_digest(root)
    inspection = service.inspect(root)
    assert inspection.format_version == 2
    assert inspection.status == "ready"
    assert inspection.raw_record_count == 260
    assert inspection.observation_count == 260
    assert _tree_digest(root) == before_read


def test_workspace_v1_remains_default_and_v2_writer_is_exclusive(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    v1 = service.initialize(tmp_path / "workspace-v1")
    assert v1.manifest.format_version == 1

    v2 = service.initialize(tmp_path / "workspace-v2", format_version=2)
    writer = service.open_storage(v2.paths, WorkspaceAccessMode.READ_WRITE)
    try:
        with pytest.raises(WorkspaceAccessError):
            service.open_storage(v2.paths, WorkspaceAccessMode.READ_WRITE)
    finally:
        writer.close()


def test_workspace_v2_paths_include_versioned_index_and_raw_root(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    paths = StoragePaths.from_workspace_root(
        initialized.paths.root,
        format_version=2,
        workspace_id=initialized.manifest.workspace_id,
    )
    assert paths.database_path == initialized.paths.root / "storage/v2/index.duckdb"
    assert paths.raw_dir.is_relative_to(initialized.paths.root / "storage/v2")
