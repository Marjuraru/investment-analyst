"""Unit tests for read-only incremental Apple market refresh planning."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketDateInterval,
    AaplMarketRefreshMode,
    AaplMarketRefreshPlan,
    AaplRefreshMode,
)
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.market.alpaca_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.market.alpaca_pipeline import (
    AlpacaMarketFetchReceipt,
    alpaca_fetch_receipt_to_raw_record,
)


class ObservationRepositoryDouble:
    """Return explicitly supplied observations without writes."""

    def __init__(
        self,
        observations: list[SimpleNamespace],
        *,
        expected_asset_id: str = ASSET_ID,
    ) -> None:
        self.observations = observations
        self.expected_asset_id = expected_asset_id
        self.calls = 0

    def list(self, *, asset_id: str):
        self.calls += 1
        assert asset_id == self.expected_asset_id
        return list(self.observations)

    def observed_at_bounds(
        self,
        *,
        asset_id: str,
        source_id: str,
        frequency: DataFrequency,
    ) -> tuple[datetime | None, datetime | None]:
        self.calls += 1
        assert asset_id == self.expected_asset_id
        values = sorted(
            item.observed_at
            for item in self.observations
            if item.asset_id == asset_id
            and item.source.source_id == source_id
            and item.frequency is frequency
        )
        return (values[0], values[-1]) if values else (None, None)

    def maximum_available_at(
        self,
        *,
        asset_id: str,
        source_id: str,
    ) -> datetime | None:
        self.calls += 1
        assert asset_id == self.expected_asset_id
        values = [
            getattr(item, "available_at", item.observed_at)
            for item in self.observations
            if item.asset_id == asset_id and item.source.source_id == source_id
        ]
        return max(values, default=None)


class RawRecordRepositoryDouble:
    """Return supplied raw records without storage writes."""

    def __init__(self, records: list[object]) -> None:
        self.records = records
        self.calls = 0

    def list(
        self,
        *,
        asset_id: str | None = None,
        source_id: str | None = None,
        schema_version: str | None = None,
    ):
        if asset_id is None or source_id is None or schema_version is None:
            raise AssertionError("planner must not materialize an unfiltered raw-record list")
        self.calls += 1
        return [
            record
            for record in self.records
            if (asset_id is None or record.asset_id == asset_id)
            and (source_id is None or record.source.source_id == source_id)
            and (schema_version is None or record.schema_version == schema_version)
        ]

    def available_at_bounds(
        self,
        *,
        asset_id: str,
        source_id: str,
        schema_version: str,
    ) -> tuple[datetime | None, datetime | None]:
        records = self.list(
            asset_id=asset_id,
            source_id=source_id,
            schema_version=schema_version,
        )
        values = [record.available_at for record in records]
        return (min(values), max(values)) if values else (None, None)


class StorageDouble:
    """Minimal open storage surface for the planner."""

    def __init__(
        self,
        observations: list[SimpleNamespace],
        records: list[object] | None = None,
        *,
        expected_asset_id: str = ASSET_ID,
    ) -> None:
        self.observations = ObservationRepositoryDouble(
            observations,
            expected_asset_id=expected_asset_id,
        )
        self.raw_records = RawRecordRepositoryDouble(records or [])

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
    )


def _receipt(
    start: datetime,
    end: datetime,
    *,
    asset_id: str = ASSET_ID,
    source_id: str = SOURCE_ID,
    feed: str = "iex",
    adjustment: str = "all",
):
    receipt = AlpacaMarketFetchReceipt(
        asset_id=asset_id,
        source_id=source_id,
        feed=feed,
        adjustment=adjustment,
        requested_start=start,
        requested_end=end,
        retrieved_at=datetime(2026, 2, 1, tzinfo=UTC),
        bar_count=0,
        page_count=1,
        traceability_verified=True,
    )
    return alpaca_fetch_receipt_to_raw_record(receipt)


def _plan(
    timestamps: tuple[datetime, ...],
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 10),
    refresh_mode: AaplRefreshMode = AaplRefreshMode.AUTO,
    extras: tuple[SimpleNamespace, ...] = (),
    receipts: tuple[object, ...] = (),
) -> AaplMarketRefreshPlan:
    storage = StorageDouble(
        [*(_observation(item) for item in timestamps), *extras],
        list(receipts),
    )
    return AaplMarketRefreshPlanner(storage).plan(
        requested_start=start,
        requested_end=end,
        refresh_mode=refresh_mode,
    )


def test_empty_workspace_plans_initial_full_requested_interval() -> None:
    plan = _plan(())
    assert plan.mode is AaplMarketRefreshMode.INITIAL
    assert plan.fetch_intervals == (
        AaplMarketDateInterval(start=date(2026, 1, 1), end=date(2026, 1, 10)),
    )
    assert plan.market_fetch_required is True
    assert plan.persisted_earliest is None
    assert plan.traceability_verified is True


def test_planner_scopes_coverage_to_one_catalog_backed_alpaca_asset() -> None:
    asset_id = "equity:us:bvn"
    source_id = "alpaca-market-data:iex:bvn:daily-bars:adjustment-all"
    configuration = AlpacaAssetConfiguration(
        asset_id=asset_id,
        symbol="BVN",
        feed="iex",
        adjustment="all",
        source_id=source_id,
        name="Compañía de Minas Buenaventura S.A.A.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NYSE",
    )
    storage = StorageDouble(
        [
            _observation(
                datetime(2026, 1, 2, 5, tzinfo=UTC),
                asset_id=asset_id,
                source_id=source_id,
            ),
            _observation(datetime(2026, 1, 2, 5, tzinfo=UTC)),
        ],
        expected_asset_id=asset_id,
    )

    plan = AaplMarketRefreshPlanner(
        storage,
        configuration=configuration,
    ).plan(
        requested_start=date(2026, 1, 2),
        requested_end=date(2026, 1, 2),
        refresh_mode=AaplRefreshMode.AUTO,
    )

    assert plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
    assert plan.fetch_intervals == ()


def test_covered_range_is_current_without_interpreting_weekend_as_gap() -> None:
    plan = _plan(
        (
            datetime(2026, 1, 2, 5, tzinfo=UTC),
            datetime(2026, 1, 5, 5, tzinfo=UTC),
        ),
        start=date(2026, 1, 2),
        end=date(2026, 1, 5),
    )
    assert plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
    assert plan.fetch_intervals == ()
    assert plan.market_fetch_required is False


@pytest.mark.parametrize("hour", [4, 5])
def test_incremental_uses_calendar_day_after_latest_timestamp(hour: int) -> None:
    plan = _plan(
        (
            datetime(2026, 1, 1, hour, tzinfo=UTC),
            datetime(2026, 1, 7, hour, tzinfo=UTC),
        )
    )
    assert plan.mode is AaplMarketRefreshMode.INCREMENTAL
    assert plan.persisted_latest == datetime(2026, 1, 7, hour, tzinfo=UTC)
    assert plan.fetch_intervals == (
        AaplMarketDateInterval(start=date(2026, 1, 8), end=date(2026, 1, 10)),
    )


def test_backfill_and_two_edge_intervals_are_ordered_and_non_overlapping() -> None:
    prefix = _plan(
        (
            datetime(2026, 1, 4, 5, tzinfo=UTC),
            datetime(2026, 1, 10, 5, tzinfo=UTC),
        )
    )
    assert prefix.mode is AaplMarketRefreshMode.BACKFILL
    assert prefix.fetch_intervals == (
        AaplMarketDateInterval(start=date(2026, 1, 1), end=date(2026, 1, 3)),
    )

    both = _plan(
        (
            datetime(2026, 1, 4, 5, tzinfo=UTC),
            datetime(2026, 1, 7, 5, tzinfo=UTC),
        )
    )
    assert both.mode is AaplMarketRefreshMode.BACKFILL
    assert both.fetch_intervals == (
        AaplMarketDateInterval(start=date(2026, 1, 1), end=date(2026, 1, 3)),
        AaplMarketDateInterval(start=date(2026, 1, 8), end=date(2026, 1, 10)),
    )
    assert both.fetch_intervals[0].end < both.fetch_intervals[1].start


def test_full_ignores_coverage_and_other_scopes_are_ignored() -> None:
    extras = (
        _observation(
            datetime(2025, 1, 1, tzinfo=UTC),
            asset_id="crypto:btc-usd",
        ),
        _observation(
            datetime(2025, 1, 2, tzinfo=UTC),
            source_id="coinbase-exchange:btc-usd:daily-candles",
        ),
        _observation(
            datetime(2025, 1, 3, tzinfo=UTC),
            frequency=DataFrequency.QUARTERLY,
        ),
    )
    plan = _plan(
        (datetime(2026, 1, 5, 5, tzinfo=UTC),),
        refresh_mode=AaplRefreshMode.FULL,
        extras=extras,
    )
    assert plan.mode is AaplMarketRefreshMode.FULL
    assert plan.fetch_intervals == (
        AaplMarketDateInterval(start=date(2026, 1, 1), end=date(2026, 1, 10)),
    )
    assert plan.persisted_earliest == datetime(2026, 1, 5, 5, tzinfo=UTC)


def test_empty_prefix_receipt_completes_existing_bar_coverage() -> None:
    plan = _plan(
        (
            datetime(2026, 1, 2, 5, tzinfo=UTC),
            datetime(2026, 1, 10, 5, tzinfo=UTC),
        ),
        receipts=(
            _receipt(
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )
    assert plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
    assert plan.fetch_intervals == ()


def test_receipt_intervals_merge_and_wrong_scopes_are_ignored() -> None:
    valid = (
        _receipt(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 4, tzinfo=UTC),
        ),
        _receipt(
            datetime(2026, 1, 8, tzinfo=UTC),
            datetime(2026, 1, 11, tzinfo=UTC),
        ),
    )
    ignored = (
        _receipt(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            asset_id="crypto:btc-usd",
        ),
        _receipt(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            source_id="other:source",
        ),
        _receipt(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            feed="sip",
        ),
        _receipt(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
            adjustment="raw",
        ),
    )
    plan = _plan(
        (
            datetime(2026, 1, 4, 5, tzinfo=UTC),
            datetime(2026, 1, 7, 5, tzinfo=UTC),
        ),
        receipts=(*valid, *ignored),
    )
    assert plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
    assert plan.persisted_earliest == datetime(2026, 1, 4, 5, tzinfo=UTC)
    assert plan.persisted_latest == datetime(2026, 1, 7, 5, tzinfo=UTC)


def test_refresh_models_reject_more_than_two_or_incoherent_intervals() -> None:
    intervals = tuple(
        AaplMarketDateInterval(start=date(2026, 1, day), end=date(2026, 1, day))
        for day in (1, 3, 5)
    )
    with pytest.raises(ValidationError, match="at most two"):
        AaplMarketRefreshPlan(
            requested_start=date(2026, 1, 1),
            requested_end=date(2026, 1, 10),
            persisted_earliest=datetime(2026, 1, 2, 5, tzinfo=UTC),
            persisted_latest=datetime(2026, 1, 4, 5, tzinfo=UTC),
            fetch_intervals=intervals,
            mode=AaplMarketRefreshMode.BACKFILL,
            market_fetch_required=True,
            reason="Invalid three-interval plan.",
            traceability_verified=True,
        )

    with pytest.raises(ValidationError, match="market_fetch_required"):
        AaplMarketRefreshPlan(
            requested_start=date(2026, 1, 1),
            requested_end=date(2026, 1, 10),
            persisted_earliest=datetime(2026, 1, 1, 5, tzinfo=UTC),
            persisted_latest=datetime(2026, 1, 10, 5, tzinfo=UTC),
            fetch_intervals=(),
            mode=AaplMarketRefreshMode.ALREADY_CURRENT,
            market_fetch_required=True,
            reason="Invalid fetch flag.",
            traceability_verified=True,
        )
