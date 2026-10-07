"""Bounded integrity checks for optional workspace-v2 incremental artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceFieldGroup,
    make_daily_evidence_prefix,
    observation_rows_digest,
)
from investment_analyst.analytics.market.incremental_state import (
    EmaParameters,
    advance_checkpoint,
)
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.storage.workspace_incremental_v2 import (
    WorkspaceIncrementalV2Error,
    verify_workspace_incremental_v2,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_ASSET_ID = "equity:us:test"
_SOURCE_ID = "simulated:daily-bars"
_TIMESTAMP = datetime(2026, 1, 5, 16, tzinfo=UTC)


def test_workspace_incremental_verification_accepts_older_workspace(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=service)

    with runtime.open_storage(
        StorageLocationRequest(workspace=initialized.paths.root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        result = verify_workspace_incremental_v2(storage)

    assert result.daily_evidence_prefixes == 0
    assert result.market_recursive_checkpoints == 0
    assert result.analysis_snapshots == 0
    assert result.maximum_batch_size == 0


def test_workspace_incremental_verification_checks_prefix_and_checkpoint(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(tmp_path / "workspace", format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=service)

    with runtime.open_storage(
        StorageLocationRequest(workspace=initialized.paths.root),
        access_mode=WorkspaceAccessMode.READ_WRITE,
    ) as storage:
        available_at = _TIMESTAMP + timedelta(hours=1)
        raw = RawRecord(
            record_id=uuid4(),
            asset_id=_ASSET_ID,
            source=SourceReference(
                source_id=_SOURCE_ID,
                record_key="incremental-unit:bar-0",
                retrieved_at=available_at,
            ),
            event_time=_TIMESTAMP,
            available_at=available_at,
            received_at=available_at,
            payload={"close": "101.25"},
            schema_version="incremental-unit-v1",
        )
        storage.raw_records.save(raw)
        observation = NormalizedObservation(
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
            transformation_version="incremental-unit-normalizer-v1",
        )
        storage.observations.save(observation)
        prefix = make_daily_evidence_prefix(
            asset_id=_ASSET_ID,
            source_id=_SOURCE_ID,
            field_group=DailyEvidenceFieldGroup.CLOSE,
            timestamp=_TIMESTAMP,
            observation_ids=(observation.observation_id,),
            observation_digest=observation_rows_digest(
                [[str(observation.observation_id), str(observation.value)]]
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
                "close": observation.observation_id,
                "volume": uuid4(),
            },
        )
        storage.store.raw_staging.save_daily_evidence_prefixes([prefix])
        checkpoint = advance_checkpoint(None, bar, prefix, EmaParameters(window=3))
        storage.store.raw_staging.save_market_recursive_checkpoints([checkpoint])

        result = verify_workspace_incremental_v2(storage)
        assert result.daily_evidence_prefixes == 1
        assert result.market_recursive_checkpoints == 1
        assert result.maximum_batch_size == 1

        changed = checkpoint.state.model_dump(mode="json")
        changed["bars_seen"] = 99
        storage.store.connection.execute(
            "UPDATE market_recursive_checkpoints_v2 SET state_json = ? WHERE checkpoint_id = ?",
            [json.dumps(changed), str(checkpoint.checkpoint_id)],
        )
        with pytest.raises(WorkspaceIncrementalV2Error):
            verify_workspace_incremental_v2(storage)
