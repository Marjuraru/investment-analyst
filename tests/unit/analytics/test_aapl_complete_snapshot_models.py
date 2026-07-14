"""Tests for strict Apple complete-snapshot models."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.aapl_snapshot_models import (
    AaplCompleteSnapshotRequest,
    AaplSnapshotStage,
    AaplSnapshotStageStatus,
    AaplSnapshotStageSummary,
    FundamentalRefreshStageDetails,
)
from investment_analyst.core.models import DataFrequency


def _request(**overrides: object) -> AaplCompleteSnapshotRequest:
    values: dict[str, object] = {
        "known_at": datetime(2026, 7, 14, tzinfo=UTC),
        "market_start": date(2026, 6, 1),
        "market_end": date(2026, 7, 1),
        "fundamental_frequency": DataFrequency.QUARTERLY,
    }
    values.update(overrides)
    return AaplCompleteSnapshotRequest.model_validate(values)


def test_request_normalizes_offset_and_keeps_fixed_scope() -> None:
    request = _request(known_at=datetime(2026, 7, 14, 1, tzinfo=timezone(timedelta(hours=-5))))
    assert request.known_at == datetime(2026, 7, 14, 6, tzinfo=UTC)
    assert request.asset_id == "equity:us:aapl"
    assert request.require_complete is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("known_at", datetime(2026, 7, 14)),
        ("asset_id", "equity:us:msft"),
        ("market_start", date(2026, 7, 2)),
        ("market_end", date(2026, 7, 15)),
        ("fundamental_frequency", DataFrequency.DAY_1),
        ("market_as_of", date(2026, 7, 15)),
        ("fundamental_as_of", date(2026, 7, 15)),
        ("require_complete", 1),
    ],
)
def test_request_rejects_invalid_scope(field: str, value: object) -> None:
    overrides = {field: value}
    if field == "market_start":
        overrides["market_end"] = date(2026, 7, 1)
    with pytest.raises(ValidationError):
        _request(**overrides)


def test_request_rejects_strings_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _request(market_start="2026-06-01")
    with pytest.raises(ValidationError):
        _request(fundamental_frequency="quarterly")
    with pytest.raises(ValidationError):
        _request(unexpected=True)


def test_skipped_stage_is_compact_and_rejects_boolean_counts() -> None:
    timestamp = datetime(2026, 7, 14, tzinfo=UTC)
    stage = AaplSnapshotStageSummary(
        stage=AaplSnapshotStage.FUNDAMENTAL_REFRESH,
        status=AaplSnapshotStageStatus.SKIPPED,
        records_generated=0,
        records_created=0,
        records_reused=0,
        started_at=timestamp,
        completed_at=timestamp,
        details=FundamentalRefreshStageDetails(),
        traceability_verified=True,
    )
    payload = stage.to_json_dict()
    assert payload["status"] == "skipped"
    assert payload["details"]["reason"].startswith("existing local")

    with pytest.raises(ValidationError):
        AaplSnapshotStageSummary(
            stage=AaplSnapshotStage.FUNDAMENTAL_REFRESH,
            status=AaplSnapshotStageStatus.SKIPPED,
            records_generated=True,
            records_created=0,
            records_reused=0,
            started_at=timestamp,
            completed_at=timestamp,
            details=FundamentalRefreshStageDetails(),
            traceability_verified=True,
        )
