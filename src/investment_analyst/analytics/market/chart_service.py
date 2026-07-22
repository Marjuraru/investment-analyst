"""Read-only construction of the bounded point-in-time Apple market chart."""

from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from typing import Protocol
from uuid import UUID

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChart,
    AaplMarketChartCoverage,
    AaplMarketChartInterval,
    AaplMarketChartPoint,
    AaplMarketChartRangeStatistics,
    AaplMarketChartRequest,
    AaplMarketChartResolution,
    AaplMarketChartSma,
)
from investment_analyst.analytics.market.history_service import MarketHistoryError
from investment_analyst.analytics.market.statistics_definitions import (
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsError
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsComputation,
    MarketStatisticsRequest,
    MetricCalculation,
)
from investment_analyst.core.models import DataQuality

_AAPL_ASSET_ID = "equity:us:aapl"
_HISTORY_START = datetime(1970, 1, 1, tzinfo=UTC)
_DAYS_PER_YEAR = Decimal("365.2425")
_SMA_ALGORITHM = "market-chart-sma-v2-decimal34"
_RANGE_ALGORITHM = "market-chart-range-statistics-v3-decimal34"
_AGGREGATION_ALGORITHM = "market-chart-ohlcv-v1-decimal34"
_LATEST_STATISTICS = {
    SIMPLE_RETURN_KEY: ("market-simple-return-1d-v1-decimal34", None),
    VOLATILITY_KEY: ("market-rolling-daily-volatility-v1-decimal34", 20),
    RELATIVE_VOLUME_KEY: ("market-relative-volume-v1-decimal34", 20),
}
_QUALITY_PRIORITY = {
    DataQuality.VALID: 0,
    DataQuality.DELAYED: 1,
    DataQuality.PARTIAL: 2,
    DataQuality.SUSPECT: 3,
}
_BASE_LIMITATIONS = (
    "Alpaca Market Data IEX covers one exchange and is not consolidated SIP coverage.",
    (
        "Aggregated open and close use the first and last source sessions; high and low "
        "use exact extrema; volume and complete trade counts are summed."
    ),
    (
        "Aggregated VWAP is volume-weighted from daily VWAP inputs only when every source "
        "session supplies VWAP and total volume is positive."
    ),
    "Ranges are bounded by available source trading sessions, not calendar-day estimates.",
    "Range CAGR is shown only when displayed endpoints span at least 365 calendar days.",
    (
        "Maximum drawdown uses displayed-resolution closes and retains its peak and trough "
        "observations."
    ),
    "Daily volatility is the non-annualized sample volatility of 20 simple daily returns.",
    (
        "Relative volume compares the latest daily session with the previous 20 available "
        "daily sessions."
    ),
    "The chart is descriptive analytical output, not financial advice or a recommendation.",
)


def _limitations(request: AaplMarketChartRequest) -> tuple[str, ...]:
    if request.interval is AaplMarketChartInterval.AUTOMATIC:
        resolution_limitation = (
            "Automatic interval uses daily points through two years, ISO-calendar weeks for "
            "five years, and UTC-calendar months for maximum."
        )
    else:
        resolution_limitation = (
            f"The requested {request.interval.value} interval uses complete "
            f"{request.resolution.value} UTC-calendar groups built from stored daily bars. "
            "Earlier groups are not truncated by the range; the current calendar group contains "
            "only evidence available at known_at and is identified as ongoing."
        )
    return (
        _BASE_LIMITATIONS[0],
        resolution_limitation,
        *_BASE_LIMITATIONS[1:3],
        (
            f"SMA {request.short_sma_window}, SMA {request.long_sma_window}, and SMA "
            f"{request.third_sma_window} use "
            "displayed-resolution closes and include the current point."
        ),
        *_BASE_LIMITATIONS[3:],
    )


class AaplMarketChartQueryError(RuntimeError):
    """Raised when stored evidence cannot produce a valid chart contract."""


class _HistoryOperations(Protocol):
    def query(self, query: HistoricalBarQuery) -> MarketBarSeries:
        """Return one verified point-in-time series."""
        ...


class _StatisticsOperations(Protocol):
    def compute(
        self,
        series: MarketBarSeries,
        request: MarketStatisticsRequest,
    ) -> MarketStatisticsComputation:
        """Compute deterministic market statistics."""
        ...


class AaplMarketChartService:
    """Compose stored bars and canonical daily statistics without writes."""

    def __init__(
        self,
        history: _HistoryOperations,
        statistics: _StatisticsOperations,
    ) -> None:
        self._history = history
        self._statistics = statistics

    def query(self, request: AaplMarketChartRequest) -> AaplMarketChart:
        """Return a bounded chart at the deterministic resolution for its range."""
        query = HistoricalBarQuery(
            asset_id=_AAPL_ASSET_ID,
            source_id=ALPACA_SOURCE_ID,
            start=_HISTORY_START,
            end=request.known_at,
            known_at=request.known_at,
        )
        try:
            series = self._history.query(query)
            statistics_series = self._statistics_tail(series)
            computation = self._statistics.compute(
                statistics_series,
                MarketStatisticsRequest(
                    query=statistics_series.query,
                    sma_windows=(5, 20),
                    volatility_window=20,
                    relative_volume_window=20,
                ),
            )
            latest_statistics = self._latest_statistics(computation, series)
        except (MarketHistoryError, MarketStatisticsError) as error:
            raise AaplMarketChartQueryError("stored market history could not be charted") from error

        groups = self._group_bars(series.bars, request.resolution)
        selected_group_start = self._selected_group_start(groups, request.session_limit)
        selected_groups = groups[selected_group_start:]
        maximum_sma_window = max(
            request.short_sma_window,
            request.long_sma_window,
            request.third_sma_window,
        )
        context_groups = groups[max(0, selected_group_start - (maximum_sma_window - 1)) :]
        context_points = self._build_points(
            context_groups,
            request.resolution,
            request.short_sma_window,
            request.long_sma_window,
            request.third_sma_window,
            request.known_at,
        )
        points = context_points[-len(selected_groups) :] if selected_groups else ()
        selected_bars = tuple(bar for group in selected_groups for bar in group)
        daily_tail = self._build_points(
            self._group_bars(
                series.bars[-maximum_sma_window:],
                AaplMarketChartResolution.DAILY,
            ),
            AaplMarketChartResolution.DAILY,
            request.short_sma_window,
            request.long_sma_window,
            request.third_sma_window,
            request.known_at,
        )
        latest_session = daily_tail[-1] if daily_tail else None
        total = len(series.bars)
        selected = len(selected_bars)
        return AaplMarketChart(
            known_at=request.known_at,
            period=request.period,
            interval=request.interval,
            session_limit=request.session_limit,
            resolution=request.resolution,
            sma_windows=(
                request.short_sma_window,
                request.long_sma_window,
                request.third_sma_window,
            ),
            points=points,
            latest_session=latest_session,
            range_statistics=self._range_statistics(points, request.resolution),
            latest_statistics=latest_statistics,
            coverage=AaplMarketChartCoverage(
                candidate_versions=series.coverage.candidate_versions,
                total_available_sessions=total,
                discarded_revisions=series.coverage.discarded_revisions,
                selected_sessions=selected,
                omitted_sessions=total - selected,
                displayed_points=len(points),
                earliest_available_timestamp=series.coverage.earliest_timestamp,
                latest_available_timestamp=series.coverage.latest_timestamp,
                earliest_selected_timestamp=selected_bars[0].timestamp if selected_bars else None,
                latest_selected_timestamp=selected_bars[-1].timestamp if selected_bars else None,
            ),
            limitations=_limitations(request),
        )

    @staticmethod
    def _statistics_tail(series: MarketBarSeries) -> MarketBarSeries:
        """Bound transient latest-statistic work to the 21 required daily sessions."""
        bars = series.bars[-21:]
        if bars == series.bars:
            return series
        query = HistoricalBarQuery(
            asset_id=series.query.asset_id,
            source_id=series.query.source_id,
            start=bars[0].timestamp,
            end=series.query.end,
            known_at=series.query.known_at,
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

    @classmethod
    def _build_points(
        cls,
        groups: tuple[tuple[MarketBar, ...], ...],
        resolution: AaplMarketChartResolution,
        short_sma_window: int,
        long_sma_window: int,
        third_sma_window: int,
        known_at: datetime,
    ) -> tuple[AaplMarketChartPoint, ...]:
        points: list[AaplMarketChartPoint] = []
        for group in groups:
            first = group[0]
            latest = group[-1]
            highest = max(group, key=lambda bar: bar.high)
            lowest = min(group, key=lambda bar: bar.low)
            volume = sum((bar.volume for bar in group), Decimal("0"))
            trade_values = tuple(bar.trade_count for bar in group)
            trade_count: Decimal | None = None
            if all(value is not None for value in trade_values):
                trade_count = sum(
                    (value for value in trade_values if value is not None),
                    Decimal("0"),
                )
            vwap_values = tuple(bar.vwap for bar in group)
            vwap: Decimal | None = None
            if volume > 0 and all(value is not None for value in vwap_values):
                with localcontext(Context(prec=34)):
                    vwap = (
                        sum(
                            (
                                value * bar.volume
                                for bar, value in zip(group, vwap_values, strict=True)
                                if value is not None
                            ),
                            Decimal("0"),
                        )
                        / volume
                    )
            available_at = max(bar.available_at for bar in group)
            close_observation_id = latest.observation_ids["close"]
            point = AaplMarketChartPoint(
                resolution=resolution,
                period_start_timestamp=first.timestamp,
                timestamp=latest.timestamp,
                bar_available_at=available_at,
                source_session_count=len(group),
                calendar_interval_closed=cls._calendar_interval_closed(
                    resolution,
                    latest.timestamp,
                    known_at,
                ),
                open=first.open,
                high=highest.high,
                low=lowest.low,
                close=latest.close,
                volume=volume,
                trade_count=trade_count,
                vwap=vwap,
                quality=max(group, key=lambda bar: _QUALITY_PRIORITY[bar.quality]).quality,
                raw_record_ids=tuple(bar.raw_record_id for bar in group),
                open_observation_id=first.observation_ids["open"],
                high_observation_id=highest.observation_ids["high"],
                low_observation_id=lowest.observation_ids["low"],
                close_observation_id=close_observation_id,
                volume_input_observation_ids=tuple(bar.observation_ids["volume"] for bar in group),
                trade_count_input_observation_ids=(
                    tuple(bar.observation_ids["trade_count"] for bar in group)
                    if trade_count is not None
                    else ()
                ),
                vwap_input_observation_ids=(
                    tuple(bar.observation_ids["vwap"] for bar in group) if vwap is not None else ()
                ),
                short_sma=cls._sma(
                    points,
                    latest.close,
                    close_observation_id,
                    available_at,
                    resolution,
                    short_sma_window,
                ),
                long_sma=cls._sma(
                    points,
                    latest.close,
                    close_observation_id,
                    available_at,
                    resolution,
                    long_sma_window,
                ),
                third_sma=cls._sma(
                    points,
                    latest.close,
                    close_observation_id,
                    available_at,
                    resolution,
                    third_sma_window,
                ),
                aggregation_algorithm_version=_AGGREGATION_ALGORITHM,
            )
            points.append(point)
        return tuple(points)

    @staticmethod
    def _calendar_interval_closed(
        resolution: AaplMarketChartResolution,
        timestamp: datetime,
        known_at: datetime,
    ) -> bool:
        """Distinguish an ongoing weekly/monthly candle from a range truncation."""
        if resolution is AaplMarketChartResolution.DAILY:
            return True
        if resolution is AaplMarketChartResolution.WEEKLY:
            return timestamp.date().isocalendar()[:2] != known_at.date().isocalendar()[:2]
        return (timestamp.year, timestamp.month) != (known_at.year, known_at.month)

    @staticmethod
    def _selected_group_start(
        groups: tuple[tuple[MarketBar, ...], ...],
        session_limit: int,
    ) -> int:
        """Select the greatest whole-bucket suffix within the source-session limit."""
        selected_sessions = 0
        for index in range(len(groups) - 1, -1, -1):
            next_count = selected_sessions + len(groups[index])
            if next_count > session_limit:
                if selected_sessions == 0:
                    return index
                return index + 1
            selected_sessions = next_count
        return 0

    @staticmethod
    def _group_bars(
        bars: tuple[MarketBar, ...],
        resolution: AaplMarketChartResolution,
    ) -> tuple[tuple[MarketBar, ...], ...]:
        groups: list[list[MarketBar]] = []
        previous_key: tuple[int, int, int] | None = None
        for bar in bars:
            date = bar.timestamp.date()
            if resolution is AaplMarketChartResolution.DAILY:
                key = (date.year, date.month, date.day)
            elif resolution is AaplMarketChartResolution.WEEKLY:
                iso = date.isocalendar()
                key = (iso.year, iso.week, 0)
            else:
                key = (date.year, date.month, 0)
            if key != previous_key:
                groups.append([])
                previous_key = key
            groups[-1].append(bar)
        return tuple(tuple(group) for group in groups)

    @staticmethod
    def _sma(
        prior_points: list[AaplMarketChartPoint],
        current_close: Decimal,
        current_close_observation_id: UUID,
        current_available_at: datetime,
        resolution: AaplMarketChartResolution,
        window: int,
    ) -> AaplMarketChartSma | None:
        if len(prior_points) + 1 < window:
            return None
        inputs = prior_points[-(window - 1) :]
        with localcontext(Context(prec=34)):
            value = (
                sum((point.close for point in inputs), Decimal("0")) + current_close
            ) / Decimal(window)
        return AaplMarketChartSma(
            value=value,
            window=window,
            resolution=resolution,
            available_at=max(
                (point.bar_available_at for point in inputs),
                default=current_available_at,
            )
            if inputs and max(point.bar_available_at for point in inputs) > current_available_at
            else current_available_at,
            input_observation_ids=tuple(
                [point.close_observation_id for point in inputs] + [current_close_observation_id]
            ),
            algorithm_version=_SMA_ALGORITHM,
        )

    @staticmethod
    def _latest_statistics(
        computation: MarketStatisticsComputation,
        series: MarketBarSeries,
    ) -> tuple[MetricCalculation, ...]:
        if not series.bars:
            return ()
        latest_timestamp = series.bars[-1].timestamp
        selected: dict[str, MetricCalculation] = {}
        for calculation in computation.calculations:
            expected = _LATEST_STATISTICS.get(calculation.metric_key)
            if expected is None or calculation.as_of != latest_timestamp:
                continue
            algorithm, window = expected
            if calculation.algorithm_version != algorithm:
                raise AaplMarketChartQueryError(
                    "statistics returned an unsupported latest-statistic algorithm"
                )
            if window is not None and calculation.parameters.get("window") != window:
                continue
            if calculation.metric_key in selected:
                raise AaplMarketChartQueryError(
                    "statistics returned an ambiguous latest-statistic value"
                )
            selected[calculation.metric_key] = calculation
        return tuple(selected[key] for key in sorted(selected))

    @staticmethod
    def _range_statistics(
        points: tuple[AaplMarketChartPoint, ...],
        resolution: AaplMarketChartResolution,
    ) -> AaplMarketChartRangeStatistics:
        source_session_count = sum(point.source_session_count for point in points)
        if not points:
            return AaplMarketChartRangeStatistics(
                resolution=resolution,
                point_count=0,
                source_session_count=0,
            )
        highest = max(points, key=lambda point: point.high)
        lowest = min(points, key=lambda point: point.low)
        return_rate: Decimal | None = None
        compound_annual_growth_rate: Decimal | None = None
        maximum_drawdown_rate: Decimal | None = None
        return_inputs: tuple[UUID, ...] = ()
        drawdown_peak = points[0]
        drawdown_trough = points[0]
        drawdown_inputs: tuple[UUID, ...] = ()
        elapsed_days = (points[-1].timestamp.date() - points[0].timestamp.date()).days
        if len(points) > 1:
            running_peak = points[0]
            with localcontext(Context(prec=34)):
                return_rate = points[-1].close / points[0].close - Decimal("1")
                if elapsed_days >= 365:
                    compound_annual_growth_rate = (points[-1].close / points[0].close) ** (
                        _DAYS_PER_YEAR / Decimal(elapsed_days)
                    ) - Decimal("1")
                maximum_drawdown_rate = Decimal("0")
                for point in points[1:]:
                    if point.close > running_peak.close:
                        running_peak = point
                    drawdown = point.close / running_peak.close - Decimal("1")
                    if drawdown < maximum_drawdown_rate:
                        maximum_drawdown_rate = drawdown
                        drawdown_peak = running_peak
                        drawdown_trough = point
            return_inputs = (
                points[0].close_observation_id,
                points[-1].close_observation_id,
            )
            drawdown_inputs = (
                drawdown_peak.close_observation_id,
                drawdown_trough.close_observation_id,
            )
        return AaplMarketChartRangeStatistics(
            resolution=resolution,
            point_count=len(points),
            source_session_count=source_session_count,
            start_timestamp=points[0].timestamp,
            end_timestamp=points[-1].timestamp,
            elapsed_days=elapsed_days,
            high=highest.high,
            low=lowest.low,
            return_rate=return_rate,
            compound_annual_growth_rate=compound_annual_growth_rate,
            maximum_drawdown_rate=maximum_drawdown_rate,
            high_observation_id=highest.high_observation_id,
            low_observation_id=lowest.low_observation_id,
            return_input_observation_ids=return_inputs,
            maximum_drawdown_peak_timestamp=(drawdown_peak.timestamp if len(points) > 1 else None),
            maximum_drawdown_trough_timestamp=(
                drawdown_trough.timestamp if len(points) > 1 else None
            ),
            maximum_drawdown_input_observation_ids=drawdown_inputs,
            algorithm_version=_RANGE_ALGORITHM,
        )
