"""Tests for the bounded read-only Apple market-chart service."""

from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChart,
    AaplMarketChartInterval,
    AaplMarketChartPeriod,
    AaplMarketChartRequest,
    AaplMarketChartResolution,
    AaplMarketChartSma,
)
from investment_analyst.analytics.market.chart_service import AaplMarketChartService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsComputation,
    MarketStatisticsRequest,
)
from investment_analyst.core.models import DataFrequency, DataQuality


class _FakeHistory:
    def __init__(self, count: int, *, discarded_revisions: int = 0) -> None:
        self._count = count
        self._discarded_revisions = discarded_revisions
        self.queries: list[HistoricalBarQuery] = []

    def query(self, query: HistoricalBarQuery) -> MarketBarSeries:
        self.queries.append(query)
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = tuple(
            _bar(query, start + timedelta(days=index), index) for index in range(self._count)
        )
        return MarketBarSeries(
            query=query,
            bars=bars,
            coverage=MarketBarCoverage(
                candidate_versions=len(bars) + self._discarded_revisions,
                selected_versions=len(bars),
                discarded_revisions=self._discarded_revisions,
                bar_count=len(bars),
                earliest_timestamp=bars[0].timestamp if bars else None,
                latest_timestamp=bars[-1].timestamp if bars else None,
            ),
            traceability_verified=True,
        )


class _SequenceHistory:
    def __init__(self, closes: tuple[str, ...]) -> None:
        self._closes = closes

    def query(self, query: HistoricalBarQuery) -> MarketBarSeries:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = tuple(
            _bar(
                query,
                start + timedelta(days=index),
                index,
                close=Decimal(close),
            )
            for index, close in enumerate(self._closes)
        )
        return MarketBarSeries(
            query=query,
            bars=bars,
            coverage=MarketBarCoverage(
                candidate_versions=len(bars),
                selected_versions=len(bars),
                discarded_revisions=0,
                bar_count=len(bars),
                earliest_timestamp=bars[0].timestamp if bars else None,
                latest_timestamp=bars[-1].timestamp if bars else None,
            ),
            traceability_verified=True,
        )


class _RecordingStatistics:
    def __init__(self) -> None:
        self.series_counts: list[int] = []

    def compute(
        self,
        series: MarketBarSeries,
        request: MarketStatisticsRequest,
    ) -> MarketStatisticsComputation:
        self.series_counts.append(len(series.bars))
        return MarketStatisticsEngine().compute(series, request)


class _MissingOptionalHistory:
    def query(self, query: HistoricalBarQuery) -> MarketBarSeries:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        bars = (
            _bar(query, start, 0, quality=DataQuality.VALID),
            _bar(
                query,
                start + timedelta(days=1),
                1,
                include_trade_count=False,
                include_vwap=False,
                quality=DataQuality.SUSPECT,
            ),
        )
        return MarketBarSeries(
            query=query,
            bars=bars,
            coverage=MarketBarCoverage(
                candidate_versions=2,
                selected_versions=2,
                discarded_revisions=0,
                bar_count=2,
                earliest_timestamp=bars[0].timestamp,
                latest_timestamp=bars[-1].timestamp,
            ),
            traceability_verified=True,
        )


def _bar(
    query: HistoricalBarQuery,
    timestamp: datetime,
    index: int,
    *,
    close: Decimal | None = None,
    include_trade_count: bool = True,
    include_vwap: bool = True,
    quality: DataQuality = DataQuality.PARTIAL,
) -> MarketBar:
    close = close if close is not None else Decimal("100.001") + Decimal(index)
    identifier_base = index * 10
    observation_ids = {
        "open": UUID(int=identifier_base + 2),
        "high": UUID(int=identifier_base + 3),
        "low": UUID(int=identifier_base + 4),
        "close": UUID(int=identifier_base + 5),
        "volume": UUID(int=identifier_base + 6),
    }
    if include_trade_count:
        observation_ids["trade_count"] = UUID(int=identifier_base + 7)
    if include_vwap:
        observation_ids["vwap"] = UUID(int=identifier_base + 8)
    return MarketBar(
        asset_id=query.asset_id,
        source_id=query.source_id,
        raw_record_id=UUID(int=identifier_base + 1),
        frequency=DataFrequency.DAY_1,
        timestamp=timestamp,
        available_at=timestamp + timedelta(hours=8),
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("1000000") + Decimal(index),
        trade_count=Decimal("1000") + Decimal(index) if include_trade_count else None,
        vwap=close if include_vwap else None,
        quality=quality,
        observation_ids=observation_ids,
    )


def test_daily_chart_is_bounded_exact_and_uses_resolution_sma() -> None:
    history = _FakeHistory(270, discarded_revisions=2)
    statistics = _RecordingStatistics()
    known_at = datetime(2026, 1, 1, tzinfo=UTC)

    chart = AaplMarketChartService(history, statistics).query(
        AaplMarketChartRequest(
            known_at=known_at,
            period=AaplMarketChartPeriod.ONE_MONTH,
        )
    )

    assert chart.schema_version == "aapl-market-chart-v5"
    assert chart.source_id == ALPACA_SOURCE_ID
    assert chart.session_limit == 22
    assert chart.resolution is AaplMarketChartResolution.DAILY
    assert chart.interval is AaplMarketChartInterval.AUTOMATIC
    assert chart.resolution_policy_version == "market-chart-resolution-policy-v2"
    assert len(chart.points) == 22
    assert chart.latest_session == chart.points[-1]
    assert chart.points[0].close == Decimal("348.001")
    assert chart.points[0].open == Decimal("348.001")
    assert chart.points[0].high == Decimal("349.001")
    assert chart.points[0].low == Decimal("347.001")
    assert chart.points[0].vwap == Decimal("348.001")
    assert chart.points[0].trade_count == Decimal("1248")
    assert chart.points[0].source_session_count == 1
    assert chart.points[0].raw_record_ids == (UUID(int=2481),)
    assert chart.points[0].volume_input_observation_ids == (UUID(int=2486),)
    assert chart.points[-1].close == Decimal("369.001")
    assert chart.sma_windows == (5, 20, 50)
    assert chart.points[0].short_sma is not None
    assert chart.points[0].short_sma.value == Decimal("346.001")
    assert chart.points[0].short_sma.resolution is AaplMarketChartResolution.DAILY
    assert len(chart.points[0].short_sma.input_observation_ids) == 5
    assert chart.points[0].long_sma is not None
    assert chart.points[0].long_sma.value == Decimal("338.501")
    assert len(chart.points[0].long_sma.input_observation_ids) == 20
    assert chart.points[0].third_sma is not None
    assert chart.points[0].third_sma.value == Decimal("323.501")
    assert len(chart.points[0].third_sma.input_observation_ids) == 50
    with localcontext(Context(prec=34)):
        expected_range_return = Decimal("369.001") / Decimal("348.001") - Decimal("1")
    assert chart.range_statistics.return_rate == expected_range_return
    assert chart.range_statistics.high == Decimal("370.001")
    assert chart.range_statistics.low == Decimal("347.001")
    assert chart.range_statistics.return_input_observation_ids == (
        chart.points[0].close_observation_id,
        chart.points[-1].close_observation_id,
    )
    latest_statistics = {item.metric_key: item for item in chart.latest_statistics}
    assert set(latest_statistics) == {
        "market.history.relative_volume",
        "market.history.rolling_daily_volatility",
        "market.history.simple_return_1d",
    }
    assert latest_statistics["market.history.simple_return_1d"].as_of == chart.points[-1].timestamp
    assert latest_statistics["market.history.rolling_daily_volatility"].parameters["window"] == 20
    assert latest_statistics["market.history.relative_volume"].parameters["window"] == 20
    assert chart.coverage.candidate_versions == 272
    assert chart.coverage.total_available_sessions == 270
    assert chart.coverage.selected_sessions == 22
    assert chart.coverage.displayed_points == 22
    assert chart.coverage.omitted_sessions == 248
    assert chart.to_json_dict()["points"][0]["close"] == "348.001"
    assert history.queries[0].start == datetime(1970, 1, 1, tzinfo=UTC)
    assert history.queries[0].end == known_at
    assert history.queries[0].known_at == known_at
    assert statistics.series_counts == [21]


def test_chart_preserves_warmup_and_empty_history_semantics() -> None:
    known_at = datetime(2026, 1, 1, tzinfo=UTC)
    short_chart = AaplMarketChartService(_FakeHistory(7), MarketStatisticsEngine()).query(
        AaplMarketChartRequest(known_at=known_at)
    )
    empty_chart = AaplMarketChartService(_FakeHistory(0), MarketStatisticsEngine()).query(
        AaplMarketChartRequest(known_at=known_at)
    )

    assert short_chart.points[3].short_sma is None
    assert short_chart.points[4].short_sma is not None
    assert all(point.long_sma is None for point in short_chart.points)
    assert all(point.third_sma is None for point in short_chart.points)
    assert empty_chart.points == ()
    assert empty_chart.latest_statistics == ()
    assert empty_chart.range_statistics.point_count == 0
    assert empty_chart.range_statistics.source_session_count == 0
    assert empty_chart.range_statistics.return_rate is None
    assert empty_chart.coverage.total_available_sessions == 0
    assert empty_chart.coverage.earliest_available_timestamp is None
    assert empty_chart.latest_session is None
    assert empty_chart.traceability_verified


def test_chart_uses_requested_sma_windows_with_exact_preceding_context() -> None:
    chart = AaplMarketChartService(_FakeHistory(270), MarketStatisticsEngine()).query(
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.ONE_MONTH,
            short_sma_window=10,
            long_sma_window=50,
            third_sma_window=100,
        )
    )

    first = chart.points[0]
    assert chart.sma_windows == (10, 50, 100)
    assert first.short_sma is not None
    assert first.short_sma.window == 10
    assert first.short_sma.value == Decimal("343.501")
    assert len(first.short_sma.input_observation_ids) == 10
    assert first.long_sma is not None
    assert first.long_sma.window == 50
    assert first.long_sma.value == Decimal("323.501")
    assert len(first.long_sma.input_observation_ids) == 50
    assert first.third_sma is not None
    assert first.third_sma.window == 100
    assert first.third_sma.value == Decimal("298.501")
    assert len(first.third_sma.input_observation_ids) == 100
    assert chart.latest_session is not None
    assert chart.latest_session.short_sma is not None
    assert chart.latest_session.short_sma.window == 10
    assert chart.latest_session.long_sma is not None
    assert chart.latest_session.long_sma.window == 50
    assert chart.latest_session.third_sma is not None
    assert chart.latest_session.third_sma.window == 100
    assert "SMA 10, SMA 50, and SMA 100" in chart.limitations[4]


def test_chart_uses_explicit_weekly_and_monthly_intervals_without_partial_buckets() -> None:
    service = AaplMarketChartService(_FakeHistory(270), MarketStatisticsEngine())
    known_at = datetime(2026, 1, 1, tzinfo=UTC)

    weekly = service.query(
        AaplMarketChartRequest(
            known_at=known_at,
            period=AaplMarketChartPeriod.ONE_MONTH,
            interval=AaplMarketChartInterval.ONE_WEEK,
        )
    )
    monthly = service.query(
        AaplMarketChartRequest(
            known_at=known_at,
            period=AaplMarketChartPeriod.ONE_MONTH,
            interval=AaplMarketChartInterval.ONE_MONTH,
        )
    )
    ongoing_week = service.query(
        AaplMarketChartRequest(
            known_at=datetime(2025, 9, 28, 12, tzinfo=UTC),
            period=AaplMarketChartPeriod.ONE_MONTH,
            interval=AaplMarketChartInterval.ONE_WEEK,
        )
    )

    assert weekly.interval is AaplMarketChartInterval.ONE_WEEK
    assert weekly.resolution is AaplMarketChartResolution.WEEKLY
    assert weekly.coverage.selected_sessions <= weekly.session_limit
    assert all(
        point.period_start_timestamp.date().isocalendar()[:2]
        == point.timestamp.date().isocalendar()[:2]
        for point in weekly.points
    )
    assert monthly.interval is AaplMarketChartInterval.ONE_MONTH
    assert monthly.resolution is AaplMarketChartResolution.MONTHLY
    assert len(monthly.points) == 1
    assert monthly.coverage.selected_sessions == monthly.points[0].source_session_count
    assert monthly.coverage.selected_sessions > monthly.session_limit
    assert monthly.points[0].period_start_timestamp.month == monthly.points[0].timestamp.month
    assert all(point.calendar_interval_closed for point in ongoing_week.points[:-1])
    assert not ongoing_week.points[-1].calendar_interval_closed
    assert ongoing_week.latest_session is not None
    assert ongoing_week.latest_session.calendar_interval_closed


def test_long_ranges_are_bounded_and_publish_cagr_and_drawdown_evidence() -> None:
    known_at = datetime(2030, 1, 1, tzinfo=UTC)
    history = _FakeHistory(1_400)

    five_years = AaplMarketChartService(history, MarketStatisticsEngine()).query(
        AaplMarketChartRequest(
            known_at=known_at,
            period=AaplMarketChartPeriod.FIVE_YEARS,
        )
    )
    maximum = AaplMarketChartService(history, MarketStatisticsEngine()).query(
        AaplMarketChartRequest(
            known_at=known_at,
            period=AaplMarketChartPeriod.MAXIMUM,
        )
    )
    statistics = five_years.range_statistics
    elapsed_days = (
        five_years.points[-1].timestamp.date() - five_years.points[0].timestamp.date()
    ).days
    with localcontext(Context(prec=34)):
        expected_cagr = (five_years.points[-1].close / five_years.points[0].close) ** (
            Decimal("365.2425") / Decimal(elapsed_days)
        ) - Decimal("1")

    assert five_years.session_limit == 1_300
    assert five_years.resolution is AaplMarketChartResolution.WEEKLY
    assert len(five_years.points) == 186
    assert five_years.coverage.selected_sessions == 1_297
    assert five_years.coverage.displayed_points == 186
    assert five_years.coverage.omitted_sessions == 103
    assert statistics.elapsed_days == elapsed_days
    assert statistics.compound_annual_growth_rate == expected_cagr
    assert statistics.maximum_drawdown_rate == Decimal("0")
    assert statistics.maximum_drawdown_input_observation_ids == (
        five_years.points[0].close_observation_id,
        five_years.points[0].close_observation_id,
    )
    assert statistics.source_session_count == 1_297
    assert statistics.algorithm_version == "market-chart-range-statistics-v3-decimal34"
    assert maximum.session_limit == 20_000
    assert maximum.resolution is AaplMarketChartResolution.MONTHLY
    assert len(maximum.points) == 46
    assert maximum.coverage.selected_sessions == 1_400
    assert maximum.coverage.omitted_sessions == 0


def test_maximum_drawdown_tracks_ordered_peak_and_trough_and_rejects_tampering() -> None:
    chart = AaplMarketChartService(
        _SequenceHistory(("100", "120", "90", "110")),
        MarketStatisticsEngine(),
    ).query(AaplMarketChartRequest(known_at=datetime(2026, 1, 1, tzinfo=UTC)))
    statistics = chart.range_statistics

    assert statistics.return_rate == Decimal("0.1")
    assert statistics.compound_annual_growth_rate is None
    assert statistics.maximum_drawdown_rate == Decimal("-0.25")
    assert statistics.maximum_drawdown_peak_timestamp == chart.points[1].timestamp
    assert statistics.maximum_drawdown_trough_timestamp == chart.points[2].timestamp
    assert statistics.maximum_drawdown_input_observation_ids == (
        chart.points[1].close_observation_id,
        chart.points[2].close_observation_id,
    )

    payload = chart.model_dump(mode="json")
    payload["range_statistics"]["maximum_drawdown_rate"] = "-0.5"
    with pytest.raises(ValidationError, match="do not match displayed evidence"):
        AaplMarketChart.model_validate(payload)


def test_weekly_aggregation_preserves_exact_ohlcv_and_daily_latest_session() -> None:
    chart = AaplMarketChartService(
        _SequenceHistory(("100", "110", "90", "105", "120", "80", "100")),
        MarketStatisticsEngine(),
    ).query(
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.FIVE_YEARS,
        )
    )

    first, second = chart.points
    with localcontext(Context(prec=34)):
        expected_vwap = sum(
            Decimal(close) * (Decimal("1000000") + Decimal(index))
            for index, close in enumerate(("100", "110", "90", "105", "120"))
        ) / Decimal("5000010")

    assert chart.resolution is AaplMarketChartResolution.WEEKLY
    assert chart.coverage.selected_sessions == 7
    assert chart.coverage.displayed_points == 2
    assert first.period_start_timestamp == datetime(2025, 1, 1, tzinfo=UTC)
    assert first.timestamp == datetime(2025, 1, 5, tzinfo=UTC)
    assert first.source_session_count == 5
    assert first.open == Decimal("100")
    assert first.high == Decimal("121")
    assert first.low == Decimal("89")
    assert first.close == Decimal("120")
    assert first.volume == Decimal("5000010")
    assert first.trade_count == Decimal("5010")
    assert first.vwap == expected_vwap
    assert first.raw_record_ids == tuple(UUID(int=index * 10 + 1) for index in range(5))
    assert first.open_observation_id == UUID(int=2)
    assert first.high_observation_id == UUID(int=43)
    assert first.low_observation_id == UUID(int=24)
    assert first.close_observation_id == UUID(int=45)
    assert len(first.volume_input_observation_ids) == 5
    assert second.period_start_timestamp == datetime(2025, 1, 6, tzinfo=UTC)
    assert second.timestamp == datetime(2025, 1, 7, tzinfo=UTC)
    assert second.source_session_count == 2
    assert second.close == Decimal("100")
    assert chart.latest_session is not None
    assert chart.latest_session.resolution is AaplMarketChartResolution.DAILY
    assert chart.latest_session.timestamp == second.timestamp
    assert chart.latest_session.short_sma is not None
    assert chart.latest_session.short_sma.value == Decimal("99")
    assert chart.range_statistics.return_rate == Decimal("-0.1666666666666666666666666666666667")
    assert chart.range_statistics.maximum_drawdown_rate == Decimal(
        "-0.1666666666666666666666666666666667"
    )

    payload = chart.model_dump(mode="json")
    payload["points"][0]["source_session_count"] = 4
    with pytest.raises(ValidationError, match="evidence must cover every source session"):
        AaplMarketChart.model_validate(payload)


def test_aggregation_does_not_invent_incomplete_optional_values() -> None:
    chart = AaplMarketChartService(
        _MissingOptionalHistory(),
        MarketStatisticsEngine(),
    ).query(
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.FIVE_YEARS,
        )
    )

    assert len(chart.points) == 1
    point = chart.points[0]
    assert point.source_session_count == 2
    assert point.trade_count is None
    assert point.trade_count_input_observation_ids == ()
    assert point.vwap is None
    assert point.vwap_input_observation_ids == ()
    assert point.quality is DataQuality.SUSPECT
    assert chart.latest_session is not None
    assert chart.latest_session.trade_count is None
    assert chart.latest_session.vwap is None


def test_chart_request_rejects_unsupported_cut_and_period() -> None:
    with pytest.raises(ValidationError, match="1970"):
        AaplMarketChartRequest(known_at=datetime(1969, 12, 31, tzinfo=UTC))
    with pytest.raises(ValidationError, match="period"):
        AaplMarketChartRequest.model_validate({"known_at": "2026-01-01T00:00:00Z", "period": "all"})
    with pytest.raises(ValidationError, match="interval"):
        AaplMarketChartRequest.model_validate(
            {"known_at": "2026-01-01T00:00:00Z", "interval": "1h"}
        )
    with pytest.raises(ValidationError, match="integers"):
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            short_sma_window=True,
        )
    with pytest.raises(ValidationError, match="smaller"):
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            short_sma_window=50,
            long_sma_window=20,
        )
    with pytest.raises(ValidationError, match="less than or equal to 200"):
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            short_sma_window=201,
            long_sma_window=400,
        )
    with pytest.raises(ValidationError, match="third_sma_window"):
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            third_sma_window=True,
        )
    with pytest.raises(ValidationError, match="smaller than third"):
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            long_sma_window=50,
            third_sma_window=50,
        )

    legacy_request = AaplMarketChartRequest(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        short_sma_window=10,
        long_sma_window=50,
    )
    assert legacy_request.third_sma_window == 50

    assert (
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.TWO_YEARS,
        ).session_limit
        == 520
    )
    assert (
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.ONE_MONTH,
            interval=AaplMarketChartInterval.ONE_MONTH,
        ).resolution
        is AaplMarketChartResolution.MONTHLY
    )
    assert (
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.FIVE_YEARS,
        ).resolution
        is AaplMarketChartResolution.WEEKLY
    )
    assert (
        AaplMarketChartRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            period=AaplMarketChartPeriod.MAXIMUM,
        ).resolution
        is AaplMarketChartResolution.MONTHLY
    )


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (Decimal("0"), "finite and greater than zero"),
        (Decimal("NaN"), "finite number"),
        (Decimal("Infinity"), "finite number"),
    ),
)
def test_chart_sma_rejects_invalid_values(value: Decimal, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AaplMarketChartSma(
            value=value,
            window=5,
            resolution=AaplMarketChartResolution.DAILY,
            available_at=datetime(2026, 1, 1, tzinfo=UTC),
            input_observation_ids=tuple(UUID(int=index + 1) for index in range(5)),
            algorithm_version="market-chart-sma-v2-decimal34",
        )
