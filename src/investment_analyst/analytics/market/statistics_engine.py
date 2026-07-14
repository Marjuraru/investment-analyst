"""Deterministic Decimal-only historical market-statistics engine."""

import json
from collections import Counter
from decimal import Context, Decimal, localcontext
from uuid import UUID

from investment_analyst.analytics.market.bar_models import MarketBar, MarketBarSeries
from investment_analyst.analytics.market.statistics_definitions import (
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsComputation,
    MarketStatisticsRequest,
    MetricCalculation,
)
from investment_analyst.core.models import DataQuality

_RETURN_ALGORITHM = "market-simple-return-1d-v1-decimal34"
_SMA_ALGORITHM = "market-sma-v1-decimal34"
_VOLATILITY_ALGORITHM = "market-rolling-daily-volatility-v1-decimal34"
_RELATIVE_VOLUME_ALGORITHM = "market-relative-volume-v1-decimal34"


class MarketStatisticsError(RuntimeError):
    """Base error for deterministic historical-statistics calculation."""


class InsufficientMarketDataError(MarketStatisticsError):
    """Reserved for callers that require a non-empty calculation result."""


class MarketStatisticsTraceabilityError(MarketStatisticsError):
    """Raised when a bar series cannot support auditable calculations."""


def _detail_key(metric_key: str, window: int | None = None) -> str:
    return metric_key if window is None else f"{metric_key}:{window}"


def _parameter_sort_key(parameters: dict[str, object]) -> str:
    return json.dumps(parameters, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _quality(values: tuple[DataQuality, ...]) -> DataQuality:
    precedence = (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    )
    for candidate in precedence:
        if candidate in values:
            return candidate
    raise MarketStatisticsTraceabilityError("metric calculation has no input quality")


def _ids(bars: tuple[MarketBar, ...], field_name: str) -> tuple[UUID, ...]:
    try:
        return tuple(bar.observation_ids[field_name] for bar in bars)
    except KeyError as error:
        raise MarketStatisticsTraceabilityError(
            f"bar is missing required observation ID for {field_name!r}"
        ) from error


def _common_parameters(series: MarketBarSeries) -> dict[str, object]:
    return {
        "source_id": series.query.source_id,
        "known_at": series.query.known_at.isoformat(),
    }


class MarketStatisticsEngine:
    """Compute four explicit statistics from one verified immutable bar series."""

    def compute(
        self,
        series: MarketBarSeries,
        request: MarketStatisticsRequest,
    ) -> MarketStatisticsComputation:
        """Calculate returns, SMAs, volatility, and relative volume."""
        self._validate_inputs(series, request)
        calculations: list[MetricCalculation] = []
        warmups: dict[str, int] = {}
        zero_skips: dict[str, int] = {}

        with localcontext(Context(prec=34)):
            calculations.extend(self._simple_returns(series, warmups))
            for window in request.sma_windows:
                calculations.extend(self._sma(series, window, warmups))
            calculations.extend(self._volatility(series, request.volatility_window, warmups))
            calculations.extend(
                self._relative_volume(
                    series,
                    request.relative_volume_window,
                    warmups,
                    zero_skips,
                )
            )

        calculations.sort(
            key=lambda item: (item.as_of, item.metric_key, _parameter_sort_key(item.parameters))
        )
        counts = Counter(item.metric_key for item in calculations)
        return MarketStatisticsComputation(
            request=request,
            bar_count=len(series.bars),
            calculations=tuple(calculations),
            calculation_counts=dict(sorted(counts.items())),
            warmup_counts=dict(sorted(warmups.items())),
            zero_denominator_skips=dict(sorted(zero_skips.items())),
            traceability_verified=True,
        )

    @staticmethod
    def _validate_inputs(series: MarketBarSeries, request: MarketStatisticsRequest) -> None:
        if not series.traceability_verified:
            raise MarketStatisticsTraceabilityError("bar series traceability is not verified")
        if series.query != request.query:
            raise MarketStatisticsTraceabilityError("series query does not match request query")
        timestamps = [bar.timestamp for bar in series.bars]
        if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
            raise MarketStatisticsTraceabilityError("bar series must be ordered and unique")
        for bar in series.bars:
            if bar.asset_id != request.query.asset_id or bar.source_id != request.query.source_id:
                raise MarketStatisticsTraceabilityError(
                    "bar asset or source does not match request"
                )
            if bar.available_at > request.query.known_at:
                raise MarketStatisticsTraceabilityError("bar was not available at known_at")

    @staticmethod
    def _simple_returns(
        series: MarketBarSeries,
        warmups: dict[str, int],
    ) -> list[MetricCalculation]:
        bars = series.bars
        warmups[SIMPLE_RETURN_KEY] = min(len(bars), 1)
        common = _common_parameters(series)
        output: list[MetricCalculation] = []
        for index in range(1, len(bars)):
            previous, current = bars[index - 1], bars[index]
            value = current.close / previous.close - Decimal("1")
            output.append(
                MetricCalculation(
                    asset_id=current.asset_id,
                    source_id=current.source_id,
                    metric_key=SIMPLE_RETURN_KEY,
                    value=value,
                    unit="ratio",
                    as_of=current.timestamp,
                    available_at=max(previous.available_at, current.available_at),
                    parameters={
                        "periods": 1,
                        "price_field": "close",
                        "previous_bar_semantics": "previous_available_bar",
                        **common,
                    },
                    input_observation_ids=_ids((previous, current), "close"),
                    algorithm_version=_RETURN_ALGORITHM,
                    quality=_quality((previous.quality, current.quality)),
                )
            )
        return output

    @staticmethod
    def _sma(
        series: MarketBarSeries,
        window: int,
        warmups: dict[str, int],
    ) -> list[MetricCalculation]:
        key = _detail_key(SMA_KEY, window)
        warmups[key] = min(len(series.bars), window - 1)
        common = _common_parameters(series)
        output: list[MetricCalculation] = []
        for end_index in range(window - 1, len(series.bars)):
            bars = series.bars[end_index - window + 1 : end_index + 1]
            value = sum((bar.close for bar in bars), Decimal("0")) / Decimal(window)
            output.append(
                MetricCalculation(
                    asset_id=bars[-1].asset_id,
                    source_id=bars[-1].source_id,
                    metric_key=SMA_KEY,
                    value=value,
                    unit="USD",
                    as_of=bars[-1].timestamp,
                    available_at=max(bar.available_at for bar in bars),
                    parameters={
                        "window": window,
                        "price_field": "close",
                        "includes_current_bar": True,
                        **common,
                    },
                    input_observation_ids=_ids(bars, "close"),
                    algorithm_version=_SMA_ALGORITHM,
                    quality=_quality(tuple(bar.quality for bar in bars)),
                )
            )
        return output

    @staticmethod
    def _volatility(
        series: MarketBarSeries,
        window: int,
        warmups: dict[str, int],
    ) -> list[MetricCalculation]:
        key = _detail_key(VOLATILITY_KEY, window)
        warmups[key] = min(len(series.bars), window)
        common = _common_parameters(series)
        output: list[MetricCalculation] = []
        for end_index in range(window, len(series.bars)):
            bars = series.bars[end_index - window : end_index + 1]
            returns = tuple(
                bars[index].close / bars[index - 1].close - Decimal("1")
                for index in range(1, len(bars))
            )
            mean = sum(returns, Decimal("0")) / Decimal(window)
            variance = sum(((item - mean) ** 2 for item in returns), Decimal("0")) / Decimal(
                window - 1
            )
            output.append(
                MetricCalculation(
                    asset_id=bars[-1].asset_id,
                    source_id=bars[-1].source_id,
                    metric_key=VOLATILITY_KEY,
                    value=variance.sqrt(),
                    unit="ratio",
                    as_of=bars[-1].timestamp,
                    available_at=max(bar.available_at for bar in bars),
                    parameters={
                        "window": window,
                        "return_type": "simple",
                        "degrees_of_freedom": 1,
                        "annualized": False,
                        **common,
                    },
                    input_observation_ids=_ids(bars, "close"),
                    algorithm_version=_VOLATILITY_ALGORITHM,
                    quality=_quality(tuple(bar.quality for bar in bars)),
                )
            )
        return output

    @staticmethod
    def _relative_volume(
        series: MarketBarSeries,
        window: int,
        warmups: dict[str, int],
        zero_skips: dict[str, int],
    ) -> list[MetricCalculation]:
        key = _detail_key(RELATIVE_VOLUME_KEY, window)
        warmups[key] = min(len(series.bars), window)
        zero_skips[key] = 0
        common = _common_parameters(series)
        output: list[MetricCalculation] = []
        for current_index in range(window, len(series.bars)):
            bars = series.bars[current_index - window : current_index + 1]
            baseline = bars[:-1]
            historical_mean = sum((bar.volume for bar in baseline), Decimal("0")) / Decimal(window)
            if historical_mean == 0:
                zero_skips[key] += 1
                continue
            output.append(
                MetricCalculation(
                    asset_id=bars[-1].asset_id,
                    source_id=bars[-1].source_id,
                    metric_key=RELATIVE_VOLUME_KEY,
                    value=bars[-1].volume / historical_mean,
                    unit="ratio",
                    as_of=bars[-1].timestamp,
                    available_at=max(bar.available_at for bar in bars),
                    parameters={
                        "window": window,
                        "comparison": "previous_available_bars",
                        "excludes_current_bar_from_baseline": True,
                        **common,
                    },
                    input_observation_ids=_ids(bars, "volume"),
                    algorithm_version=_RELATIVE_VOLUME_ALGORITHM,
                    quality=_quality(tuple(bar.quality for bar in bars)),
                )
            )
        return output
