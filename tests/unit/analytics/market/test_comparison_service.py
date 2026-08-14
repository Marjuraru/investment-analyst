"""Read-only service tests for common-calendar and PIT comparison behavior."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

import pytest

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.comparison_models import (
    MarketComparisonAsset,
    MarketComparisonRequest,
)
from investment_analyst.analytics.market.comparison_service import MarketComparisonService
from investment_analyst.core.models import DataFrequency, DataQuality


class _History:
    def __init__(self, series: dict[str, MarketBarSeries]) -> None:
        self._series = series
        self.queries: list[HistoricalBarQuery] = []

    def query(self, query: HistoricalBarQuery) -> MarketBarSeries:
        self.queries.append(query)
        return self._series[query.asset_id]


def _series(
    asset_id: str,
    closes: tuple[Decimal, ...],
    *,
    available_at: datetime,
) -> MarketBarSeries:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    query = HistoricalBarQuery(
        asset_id=asset_id,
        source_id=f"source:{asset_id}",
        start=start,
        end=start + timedelta(days=40),
        known_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    bars = tuple(
        MarketBar(
            asset_id=asset_id,
            source_id=query.source_id,
            raw_record_id=uuid5(UUID(int=0), f"raw:{asset_id}:{index}"),
            frequency=DataFrequency.DAY_1,
            timestamp=start + timedelta(days=index),
            available_at=available_at,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=Decimal("1"),
            quality=DataQuality.VALID,
            observation_ids={
                field: uuid5(UUID(int=0), f"{asset_id}:{index}:{field}")
                for field in ("open", "high", "low", "close", "volume")
            },
        )
        for index, close in enumerate(closes)
    )
    return MarketBarSeries(
        query=query,
        bars=bars,
        coverage=MarketBarCoverage(
            candidate_versions=len(bars),
            selected_versions=len(bars),
            discarded_revisions=0,
            bar_count=len(bars),
            earliest_timestamp=bars[0].timestamp,
            latest_timestamp=bars[-1].timestamp,
        ),
        traceability_verified=True,
    )


def test_service_uses_one_history_instance_common_dates_and_deterministic_identity() -> None:
    known_at = datetime(2026, 3, 1, tzinfo=UTC)
    history = _History(
        {
            "equity:us:spy": _series(
                "equity:us:spy",
                tuple(Decimal("100") + Decimal(index) for index in range(22)),
                available_at=known_at,
            ),
            "equity:us:aapl": _series(
                "equity:us:aapl",
                tuple(Decimal("200") + Decimal(index) for index in range(21)),
                available_at=known_at,
            ),
        }
    )
    request = MarketComparisonRequest(
        asset_ids=("equity:us:aapl", "equity:us:spy"),
        benchmark_id="equity:us:spy",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 2, 10),
        known_at=known_at,
    )
    assets = {
        asset_id: MarketComparisonAsset(
            asset_id=asset_id, source_id=f"source:{asset_id}", quote_currency="USD"
        )
        for asset_id in request.asset_ids
    }

    result = MarketComparisonService(history).query(request, assets)
    repeated = MarketComparisonService(history).query(request, assets)

    assert len(history.queries) == 4
    assert result.asset_ids == ("equity:us:spy", "equity:us:aapl")
    assert len(result.common_dates) == 21
    assert result.coverage[0].discarded_dates == (date(2026, 1, 22),)
    assert result.comparison_id == repeated.comparison_id


def test_service_excludes_bars_not_available_at_known_cut() -> None:
    known_at = datetime(2026, 2, 1, tzinfo=UTC)
    history = _History(
        {
            "equity:us:spy": _series(
                "equity:us:spy", tuple(Decimal("100") for _ in range(21)), available_at=known_at
            ),
            "equity:us:aapl": _series(
                "equity:us:aapl",
                tuple(Decimal("100") for _ in range(21)),
                available_at=known_at + timedelta(days=1),
            ),
        }
    )
    request = MarketComparisonRequest(
        asset_ids=("equity:us:aapl", "equity:us:spy"),
        benchmark_id="equity:us:spy",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 2, 1),
        known_at=known_at,
    )
    assets = {
        asset_id: MarketComparisonAsset(
            asset_id=asset_id, source_id=f"source:{asset_id}", quote_currency="USD"
        )
        for asset_id in request.asset_ids
    }

    with pytest.raises(ValueError, match="available_at"):
        MarketComparisonService(history).query(request, assets)
