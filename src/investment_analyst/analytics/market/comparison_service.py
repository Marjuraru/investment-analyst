"""Read-only point-in-time multi-asset comparison over reconstructed daily bars."""

from collections.abc import Mapping
from datetime import date

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery, MarketBar
from investment_analyst.analytics.market.comparison_engine import (
    metrics,
    normalized_points,
    simple_returns,
)
from investment_analyst.analytics.market.comparison_identity import comparison_id
from investment_analyst.analytics.market.comparison_models import (
    MarketComparisonAsset,
    MarketComparisonCoverage,
    MarketComparisonPoint,
    MarketComparisonRequest,
    MarketComparisonSeries,
    MarketMultiAssetComparisonResult,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.core.models import DataFrequency


class MarketComparisonError(ValueError):
    """Base error for an unavailable or invalid local comparison."""


class AmbiguousDailyMarketBarError(MarketComparisonError):
    """Raised when one asset has more than one eligible bar on a UTC date."""


class InsufficientCommonMarketSampleError(MarketComparisonError):
    """Raised when the intersection cannot support the fixed comparison contract."""


class MarketComparisonService:
    """Build an ephemeral comparison from one shared historical-data service."""

    def __init__(self, history: HistoricalMarketDataService) -> None:
        self._history = history

    def query(
        self,
        request: MarketComparisonRequest,
        assets: Mapping[str, MarketComparisonAsset],
    ) -> MarketMultiAssetComparisonResult:
        """Read and compare canonical local daily closes without writes or providers."""
        canonical_ids = request.canonical_asset_ids
        resolved = self._resolve_assets(canonical_ids, assets)
        currencies = {item.quote_currency for item in resolved}
        if len(currencies) != 1:
            raise MarketComparisonError("comparison assets must share one quote currency")
        queried = {item.asset_id: self._daily_bars(request, item) for item in resolved}
        bars_by_asset = {asset_id: values[0] for asset_id, values in queried.items()}
        dates_by_asset = {asset_id: set(bars) for asset_id, bars in bars_by_asset.items()}
        common_dates = tuple(sorted(set.intersection(*dates_by_asset.values())))
        if not 21 <= len(common_dates) <= 1_300:
            raise InsufficientCommonMarketSampleError(
                "comparison requires between 21 and 1300 common daily closes"
            )
        points_by_asset: dict[str, tuple[MarketComparisonPoint, ...]] = {}
        for item in resolved:
            points = tuple(
                MarketComparisonPoint(
                    date=calendar_date,
                    close=bars_by_asset[item.asset_id][calendar_date].close,
                    normalized_close=bars_by_asset[item.asset_id][calendar_date].close,
                    close_observation_id=bars_by_asset[item.asset_id][
                        calendar_date
                    ].observation_ids["close"],
                    available_at=bars_by_asset[item.asset_id][calendar_date].available_at,
                )
                for calendar_date in common_dates
            )
            points_by_asset[item.asset_id] = normalized_points(points)
        benchmark_points = points_by_asset[request.benchmark_id]
        benchmark_returns = simple_returns(benchmark_points)
        series = tuple(
            MarketComparisonSeries(
                asset_id=item.asset_id,
                source_id=item.source_id,
                points=points_by_asset[item.asset_id],
                metrics=metrics(
                    points_by_asset[item.asset_id],
                    benchmark_returns,
                    is_benchmark=item.asset_id == request.benchmark_id,
                ),
            )
            for item in resolved
        )
        all_observation_ids = tuple(
            point.close_observation_id
            for item in resolved
            for point in points_by_asset[item.asset_id]
        )
        coverage = tuple(
            MarketComparisonCoverage(
                asset_id=item.asset_id,
                source_id=item.source_id,
                selected_bar_count=len(bars_by_asset[item.asset_id]),
                discarded_revisions=queried[item.asset_id][1],
                discarded_dates=tuple(
                    sorted(set(bars_by_asset[item.asset_id]) - set(common_dates))
                ),
            )
            for item in resolved
        )
        return MarketMultiAssetComparisonResult(
            comparison_id=comparison_id(request, all_observation_ids),
            benchmark_id=request.benchmark_id,
            asset_ids=canonical_ids,
            known_at=request.known_at,
            start_date=request.start_date,
            end_date=request.end_date,
            quote_currency=next(iter(currencies)),
            common_dates=common_dates,
            series=series,
            coverage=coverage,
            formulas={
                "normalized_close": "100 * close_t / close_0",
                "total_return": "close_last / close_0 - 1",
                "maximum_drawdown": "min(close_t / running_peak_t - 1)",
                "daily_volatility": "sample_stdev(simple_returns, ddof=1)",
                "correlation": (
                    "sample_covariance(peer, benchmark) / (stdev_peer * stdev_benchmark)"
                ),
                "beta": "sample_covariance(peer, benchmark) / sample_variance(benchmark)",
            },
            limits={"minimum_common_dates": 21, "maximum_common_dates": 1_300, "maximum_assets": 5},
            limitations=(
                "daily close comparison uses only dates available for every selected asset",
                (
                    "no forward filling, interpolation, exchange calendar, ranking, or "
                    "recommendation is applied"
                ),
            ),
        )

    @staticmethod
    def _resolve_assets(
        canonical_ids: tuple[str, ...], assets: Mapping[str, MarketComparisonAsset]
    ) -> tuple[MarketComparisonAsset, ...]:
        try:
            return tuple(assets[asset_id] for asset_id in canonical_ids)
        except KeyError as error:
            raise MarketComparisonError(
                f"asset_id is not eligible for comparison: {error.args[0]}"
            ) from error

    def _daily_bars(
        self, request: MarketComparisonRequest, asset: MarketComparisonAsset
    ) -> tuple[dict[date, MarketBar], int]:
        series = self._history.query(
            HistoricalBarQuery(
                asset_id=asset.asset_id,
                source_id=asset.source_id,
                start=request.start_instant,
                end=request.end_exclusive,
                known_at=request.known_at,
            )
        )
        by_date: dict[date, MarketBar] = {}
        for bar in series.bars:
            if bar.asset_id != asset.asset_id or bar.source_id != asset.source_id:
                raise MarketComparisonError("market bar asset or source does not match comparison")
            if bar.frequency is not DataFrequency.DAY_1:
                raise MarketComparisonError("comparison requires DAY_1 bars")
            if not request.start_instant <= bar.timestamp < request.end_exclusive:
                raise MarketComparisonError("market bar timestamp is outside the requested range")
            if bar.available_at > request.known_at:
                raise MarketComparisonError("market bar available_at is later than known_at")
            calendar_date = bar.timestamp.date()
            if calendar_date in by_date:
                raise AmbiguousDailyMarketBarError(
                    f"asset {asset.asset_id!r} has multiple eligible bars on "
                    f"{calendar_date.isoformat()}"
                )
            by_date[calendar_date] = bar
        return by_date, series.coverage.discarded_revisions
