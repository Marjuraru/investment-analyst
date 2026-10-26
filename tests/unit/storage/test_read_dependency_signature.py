"""Unit tests for content-bound read dependency signatures."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.read_dependency_signature import (
    ReadDependencyFamily,
    ReadDependencyQuery,
    ReadSignatureUnavailable,
    build_read_dependency_signature,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
_ASSET = "equity:us:amd"
_SOURCE = "fixture:market"


def _workspace(tmp_path: Path) -> tuple[WorkspaceService, LocalStorage]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "v2", format_version=2)
    storage = service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE)
    storage.open()
    storage.assets.upsert(
        Asset(
            asset_id=_ASSET,
            symbol="AMD",
            name="Advanced Micro Devices",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            exchange="NASDAQ",
            provider_symbols={"fixture": "AMD"},
            is_active=True,
        )
    )
    storage.sources.upsert(
        SourceDefinition(
            source_id=_SOURCE,
            provider_name="Fixture",
            dataset_name="Daily bars",
            source_type=SourceType.MARKET,
            base_url="https://example.invalid",
            is_official=True,
            coverage_notes="Fixture market bars.",
        )
    )
    return service, storage


def _record(value: str, *, record_id=None, available_at: datetime = _NOW) -> RawRecord:
    return RawRecord(
        record_id=record_id or uuid4(),
        asset_id=_ASSET,
        source=SourceReference(source_id=_SOURCE, record_key=f"AMD:{value}", retrieved_at=_NOW),
        event_time=_NOW - timedelta(hours=2),
        available_at=available_at,
        received_at=_NOW,
        payload={"close": value},
        schema_version="1",
    )


def _observation(
    record: RawRecord,
    value: str,
    *,
    observed_at: datetime = _NOW - timedelta(days=1),
    available_at: datetime = _NOW,
    field_name: str = "close",
) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=record.record_id,
        asset_id=_ASSET,
        field_name=field_name,
        value=Decimal(value),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=observed_at,
        available_at=available_at,
        normalized_at=_NOW,
        source=SourceReference(source_id=_SOURCE, record_key=f"AMD:{value}", retrieved_at=_NOW),
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )


def _query(*, known_at: datetime = _NOW, frequency: str = "day_1") -> ReadDependencyQuery:
    return ReadDependencyQuery(
        family=ReadDependencyFamily.MARKET_BARS,
        endpoint="/api/market-chart",
        asset_ids=(_ASSET,),
        source_ids=(_SOURCE,),
        frequency=frequency,
        known_at=known_at,
        selector="fixture",
    )


def test_signature_is_stable_and_content_bound(tmp_path: Path) -> None:
    _service, storage = _workspace(tmp_path)
    try:
        record = _record("210.50")
        storage.raw_records.save(record)
        storage.observations.save(_observation(record, "210.50"))
        first = build_read_dependency_signature(storage, _query())
        second = build_read_dependency_signature(storage, _query())
        assert first.digest == second.digest
        assert first.workspace_id == storage.paths.workspace_id
        assert any(item.dependency_id == str(record.record_id) for item in first.dependencies)
        assert any(item.dependency_id == str(record.record_id) for item in first.dependencies)
    finally:
        storage.close()


def test_equal_counts_with_different_content_change_the_signature(tmp_path: Path) -> None:
    _service, storage = _workspace(tmp_path)
    try:
        record = _record("210.50")
        observation = _observation(record, "210.50")
        storage.raw_records.save(record)
        storage.observations.save(observation)
        baseline = build_read_dependency_signature(storage, _query())

        storage.observations.save(
            observation.model_copy(update={"value": Decimal("211.00")}).model_copy(
                update={"observation_id": uuid4()}
            )
        )
        changed = build_read_dependency_signature(storage, _query())
        assert changed.digest != baseline.digest
        assert len(changed.dependencies) != len(baseline.dependencies)
    finally:
        storage.close()


def test_known_at_and_alien_asset_scope_change_the_signature(tmp_path: Path) -> None:
    _service, storage = _workspace(tmp_path)
    try:
        record = _record("210.50")
        storage.raw_records.save(record)
        storage.observations.save(_observation(record, "210.50", available_at=_NOW))
        before = build_read_dependency_signature(storage, _query(known_at=_NOW))
        after = build_read_dependency_signature(storage, _query(known_at=_NOW + timedelta(days=1)))
        assert before.digest != after.digest
        other_asset = _query().model_copy(update={"asset_ids": ("equity:us:aapl",)})
        alien = build_read_dependency_signature(storage, other_asset)
        assert alien.digest != before.digest
    finally:
        storage.close()


def test_coverage_range_excludes_out_of_range_observation(tmp_path: Path) -> None:
    _service, storage = _workspace(tmp_path)
    try:
        record = _record("210.50")
        observation = _observation(record, "210.50", observed_at=_NOW - timedelta(days=1))
        storage.raw_records.save(record)
        storage.observations.save(observation)
        wide = ReadDependencyQuery(
            family=ReadDependencyFamily.COVERAGE,
            endpoint="/api/v1/universe-coverage",
            asset_ids=(_ASSET,),
            source_ids=(_SOURCE,),
            known_at=_NOW,
            start=_NOW - timedelta(days=2),
            end=_NOW,
            selector="coverage",
        )
        narrow = wide.model_copy(update={"start": _NOW - timedelta(hours=12)})
        wide_signature = build_read_dependency_signature(storage, wide)
        narrow_signature = build_read_dependency_signature(storage, narrow)
        assert wide_signature.digest != narrow_signature.digest
        assert str(observation.observation_id) in {
            item.dependency_id for item in wide_signature.dependencies
        }
        assert str(observation.observation_id) not in {
            item.dependency_id for item in narrow_signature.dependencies
        }
    finally:
        storage.close()


def test_v1_storage_fails_closed_without_a_snapshot_binding(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "v1", format_version=1)
    with (
        service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage,
        pytest.raises(ReadSignatureUnavailable),
    ):
        build_read_dependency_signature(storage, _query())
