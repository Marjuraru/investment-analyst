"""Unit tests for read-only incremental Coinbase edge planning."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from investment_analyst.application.btc_refresh_models import (
    BtcMarketDateInterval,
    BtcMarketRefreshMode,
    BtcRefreshMode,
)
from investment_analyst.application.btc_refresh_planner import BtcMarketRefreshPlanner
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID


class ObservationRepositoryDouble:
    """Return supplied observations without storage writes."""

    def __init__(self, observations: list[SimpleNamespace]) -> None:
        self.observations = observations

    def list(self, *, asset_id: str):
        assert asset_id == ASSET_ID
        return list(self.observations)


class StorageDouble:
    """Minimal open storage surface for the planner."""

    def __init__(self, observations: list[SimpleNamespace]) -> None:
        self.observations = ObservationRepositoryDouble(observations)

    def require_open(self) -> None:
        return None


def _observation(
    timestamp: datetime,
    *,
    asset_id: str = ASSET_ID,
    source_id: str = SOURCE_ID,
    frequency: DataFrequency = DataFrequency.DAY_1,
) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=asset_id,
        source=SimpleNamespace(source_id=source_id),
        frequency=frequency,
        observed_at=timestamp,
        available_at=timestamp,
    )


def _plan(
    timestamps: tuple[datetime, ...],
    *,
    start: date = date(2026, 7, 1),
    end: date = date(2026, 7, 10),
    mode: BtcRefreshMode = BtcRefreshMode.AUTO,
    extras: tuple[SimpleNamespace, ...] = (),
):
    storage = StorageDouble([*(_observation(timestamp) for timestamp in timestamps), *extras])
    return BtcMarketRefreshPlanner(storage).plan(
        requested_start=start,
        requested_end=end,
        refresh_mode=mode,
    )


def test_empty_workspace_plans_complete_initial_range() -> None:
    plan = _plan(())

    assert plan.mode is BtcMarketRefreshMode.INITIAL
    assert plan.fetch_intervals == (
        BtcMarketDateInterval(start=date(2026, 7, 1), end=date(2026, 7, 10)),
    )
    assert plan.persisted_earliest is None
    assert plan.persisted_latest_available_at is None
    assert plan.market_fetch_required is True


def test_covered_range_is_current_without_inferring_internal_gaps() -> None:
    plan = _plan(
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 5, tzinfo=UTC),
            datetime(2026, 7, 10, tzinfo=UTC),
        )
    )

    assert plan.mode is BtcMarketRefreshMode.ALREADY_CURRENT
    assert plan.fetch_intervals == ()
    assert plan.persisted_latest_available_at == datetime(2026, 7, 10, tzinfo=UTC)


def test_incremental_and_two_edge_plans_use_utc_calendar_days() -> None:
    incremental = _plan(
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 7, tzinfo=UTC),
        )
    )
    both = _plan(
        (
            datetime(2026, 7, 4, tzinfo=UTC),
            datetime(2026, 7, 7, tzinfo=UTC),
        )
    )

    assert incremental.mode is BtcMarketRefreshMode.INCREMENTAL
    assert incremental.fetch_intervals == (
        BtcMarketDateInterval(start=date(2026, 7, 8), end=date(2026, 7, 10)),
    )
    assert both.mode is BtcMarketRefreshMode.BACKFILL
    assert both.fetch_intervals == (
        BtcMarketDateInterval(start=date(2026, 7, 1), end=date(2026, 7, 3)),
        BtcMarketDateInterval(start=date(2026, 7, 8), end=date(2026, 7, 10)),
    )


def test_full_refetches_range_and_ignores_other_scopes() -> None:
    extras = (
        _observation(datetime(2020, 1, 1, tzinfo=UTC), asset_id="equity:us:aapl"),
        _observation(datetime(2020, 1, 2, tzinfo=UTC), source_id="other:source"),
        _observation(
            datetime(2020, 1, 3, tzinfo=UTC),
            frequency=DataFrequency.QUARTERLY,
        ),
    )
    plan = _plan(
        (datetime(2026, 7, 5, tzinfo=UTC),),
        mode=BtcRefreshMode.FULL,
        extras=extras,
    )

    assert plan.mode is BtcMarketRefreshMode.FULL
    assert plan.fetch_intervals == (
        BtcMarketDateInterval(start=date(2026, 7, 1), end=date(2026, 7, 10)),
    )
    assert plan.persisted_earliest == datetime(2026, 7, 5, tzinfo=UTC)
