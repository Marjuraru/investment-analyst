"""Unit tests for configured Coinbase daily-series edge planning."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from investment_analyst.application.btc_refresh_models import (
    BtcMarketDateInterval,
    BtcMarketRefreshMode,
    BtcRefreshMode,
)
from investment_analyst.application.crypto_spot_daily_planner import (
    CryptoSpotDailyRefreshPlanner,
)
from investment_analyst.core.models import DataFrequency

ASSET_ID = "crypto:eth-usd"
SOURCE_ID = "coinbase-exchange:eth-usd:daily-candles"


class ObservationRepositoryDouble:
    """Return supplied observations while recording the repository projection."""

    def __init__(self, observations: list[SimpleNamespace]) -> None:
        self.observations = observations
        self.calls: list[dict[str, object]] = []

    def list(
        self,
        *,
        asset_id: str,
        source_id: str | None = None,
        frequency: DataFrequency | None = None,
    ) -> list[SimpleNamespace]:
        self.calls.append(
            {
                "asset_id": asset_id,
                "source_id": source_id,
                "frequency": frequency,
            }
        )
        assert asset_id == ASSET_ID
        assert source_id == SOURCE_ID
        assert frequency is DataFrequency.DAY_1
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
    plan = CryptoSpotDailyRefreshPlanner(
        storage,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
    ).plan(
        requested_start=start,
        requested_end=end,
        refresh_mode=mode,
    )
    return plan


def test_crypto_spot_planner_projects_source_and_daily_frequency_in_sql() -> None:
    storage = StorageDouble([_observation(datetime(2026, 7, 5, tzinfo=UTC))])

    plan = CryptoSpotDailyRefreshPlanner(
        storage,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
    ).plan(
        requested_start=date(2026, 7, 1),
        requested_end=date(2026, 7, 10),
        refresh_mode=BtcRefreshMode.AUTO,
    )

    assert plan.persisted_earliest == datetime(2026, 7, 5, tzinfo=UTC)
    assert storage.observations.calls == [
        {
            "asset_id": ASSET_ID,
            "source_id": SOURCE_ID,
            "frequency": DataFrequency.DAY_1,
        }
    ]


def test_crypto_spot_planner_ignores_intraday_and_derivative_rows_of_the_same_asset() -> None:
    plan = _plan(
        (datetime(2026, 7, 5, tzinfo=UTC),),
        extras=(
            _observation(
                datetime(2020, 1, 1, tzinfo=UTC),
                source_id="coinbase-exchange:eth-usd:minute-1-candles",
                frequency=DataFrequency.MINUTE_1,
            ),
            _observation(
                datetime(2020, 1, 2, tzinfo=UTC),
                source_id="deribit:eth-usd:dvol",
                frequency=DataFrequency.DAY_1,
            ),
        ),
    )

    assert plan.persisted_earliest == datetime(2026, 7, 5, tzinfo=UTC)
    assert plan.persisted_latest == datetime(2026, 7, 5, tzinfo=UTC)


def test_redundant_python_guards_are_preserved() -> None:
    naive_observed_at = _observation(datetime(2026, 7, 5))
    naive_available_at = _observation(datetime(2026, 7, 6, tzinfo=UTC))
    naive_available_at.available_at = datetime(2026, 7, 6)

    plan = _plan((), extras=(naive_observed_at, naive_available_at))

    assert plan.mode is BtcMarketRefreshMode.INITIAL
    assert plan.persisted_earliest is None


@pytest.mark.parametrize(
    ("timestamps", "mode", "expected_mode", "expected_intervals"),
    (
        ((), BtcRefreshMode.AUTO, BtcMarketRefreshMode.INITIAL, (("2026-07-01", "2026-07-10"),)),
        (
            (("2026-07-01", "2026-07-07"),),
            BtcRefreshMode.AUTO,
            BtcMarketRefreshMode.INCREMENTAL,
            (("2026-07-08", "2026-07-10"),),
        ),
        (
            (("2026-07-01", "2026-07-10"),),
            BtcRefreshMode.AUTO,
            BtcMarketRefreshMode.ALREADY_CURRENT,
            (),
        ),
        (
            (("2026-07-04", "2026-07-07"),),
            BtcRefreshMode.AUTO,
            BtcMarketRefreshMode.BACKFILL,
            (("2026-07-01", "2026-07-03"), ("2026-07-08", "2026-07-10")),
        ),
        (
            (("2026-07-05",),),
            BtcRefreshMode.FULL,
            BtcMarketRefreshMode.FULL,
            (("2026-07-01", "2026-07-10"),),
        ),
    ),
)
def test_crypto_spot_plan_is_identical_to_base_for_every_refresh_mode(
    timestamps: tuple[tuple[str, ...], ...],
    mode: BtcRefreshMode,
    expected_mode: BtcMarketRefreshMode,
    expected_intervals: tuple[tuple[str, str], ...],
) -> None:
    parsed_timestamps = tuple(
        datetime.fromisoformat(f"{timestamp}T00:00:00+00:00")
        for group in timestamps
        for timestamp in group
    )
    plan = _plan(parsed_timestamps, mode=mode)

    assert plan.mode is expected_mode
    assert plan.fetch_intervals == tuple(
        BtcMarketDateInterval(
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
        )
        for start, end in expected_intervals
    )


def test_crypto_spot_planner_still_refuses_to_infer_internal_gaps() -> None:
    plan = _plan(
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 5, tzinfo=UTC),
            datetime(2026, 7, 10, tzinfo=UTC),
        )
    )

    assert plan.mode is BtcMarketRefreshMode.ALREADY_CURRENT
    assert plan.fetch_intervals == ()
