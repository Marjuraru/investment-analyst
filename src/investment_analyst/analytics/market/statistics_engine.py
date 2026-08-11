"""Deterministic Decimal-only historical market-statistics engine."""

import json
from collections import Counter
from decimal import Context, Decimal, localcontext
from uuid import UUID

from investment_analyst.analytics.market.bar_models import MarketBar, MarketBarSeries
from investment_analyst.analytics.market.bar_schemas import get_market_bar_schema
from investment_analyst.analytics.market.statistics_definitions import (
    BOLLINGER_BANDWIDTH_KEY,
    BOLLINGER_LOWER_KEY,
    BOLLINGER_PERCENT_B_KEY,
    BOLLINGER_UPPER_KEY,
    EMA_KEY,
    RELATIVE_VOLUME_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_identity import metric_result_id
from investment_analyst.analytics.market.statistics_models import (
    MarketStatisticsComputation,
    MarketStatisticsRequest,
    MetricCalculation,
)
from investment_analyst.core.models import DataFrequency, DataQuality

_RETURN_ALGORITHM = "market-simple-return-1d-v1-decimal34"
_SMA_ALGORITHM = "market-sma-v1-decimal34"
_VOLATILITY_ALGORITHM = "market-rolling-daily-volatility-v1-decimal34"
_RELATIVE_VOLUME_ALGORITHM = "market-relative-volume-v1-decimal34"
_BOLLINGER_ALGORITHM = "market-bollinger-bands-v1-decimal34"
_EMA_ALGORITHM = "market-ema-v1-decimal34"


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
    """Compute explicit Decimal-only statistics from one verified immutable bar series."""

    def compute(
        self,
        series: MarketBarSeries,
        request: MarketStatisticsRequest,
    ) -> MarketStatisticsComputation:
        """Calculate descriptive market statistics from one point-in-time bar series."""
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
            calculations.extend(
                self._bollinger(
                    series,
                    request.bollinger_window,
                    request.bollinger_multiplier,
                    warmups,
                    zero_skips,
                )
            )
            for window in request.ema_windows:
                calculations.extend(self._ema(series, window, warmups))

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
        try:
            source_frequency = get_market_bar_schema(request.query.source_id).frequency
        except ValueError as error:
            raise MarketStatisticsTraceabilityError(
                "daily market statistics require a registered source"
            ) from error
        if source_frequency is not DataFrequency.DAY_1:
            raise MarketStatisticsTraceabilityError(
                "daily market statistics require a DAY_1 source"
            )
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
            if bar.frequency is not DataFrequency.DAY_1:
                raise MarketStatisticsTraceabilityError(
                    "daily market statistics require DAY_1 bars"
                )

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

    @staticmethod
    def _bollinger(
        series: MarketBarSeries,
        window: int,
        multiplier: Decimal,
        warmups: dict[str, int],
        zero_skips: dict[str, int],
    ) -> list[MetricCalculation]:
        """Compute population-standard-deviation Bollinger values over close windows."""
        keys = (
            BOLLINGER_UPPER_KEY,
            BOLLINGER_LOWER_KEY,
            BOLLINGER_BANDWIDTH_KEY,
            BOLLINGER_PERCENT_B_KEY,
        )
        for metric_key in keys:
            warmups[_detail_key(metric_key, window)] = min(len(series.bars), window - 1)
        zero_skips[_detail_key(BOLLINGER_PERCENT_B_KEY, window)] = 0
        common = _common_parameters(series)
        output: list[MetricCalculation] = []
        for end_index in range(window - 1, len(series.bars)):
            bars = series.bars[end_index - window + 1 : end_index + 1]
            middle = sum((bar.close for bar in bars), Decimal("0")) / Decimal(window)
            variance = sum(((bar.close - middle) ** 2 for bar in bars), Decimal("0")) / Decimal(
                window
            )
            standard_deviation = variance.sqrt()
            upper = middle + multiplier * standard_deviation
            lower = middle - multiplier * standard_deviation
            parameters = {
                "window": window,
                "multiplier": str(multiplier),
                "price_field": "close",
                "degrees_of_freedom": 0,
                "includes_current_bar": True,
                **common,
            }
            base = {
                "asset_id": bars[-1].asset_id,
                "source_id": bars[-1].source_id,
                "as_of": bars[-1].timestamp,
                "available_at": max(bar.available_at for bar in bars),
                "parameters": parameters,
                "input_observation_ids": _ids(bars, "close"),
                "algorithm_version": _BOLLINGER_ALGORITHM,
                "quality": _quality(tuple(bar.quality for bar in bars)),
            }
            output.extend(
                (
                    MetricCalculation(
                        metric_key=BOLLINGER_UPPER_KEY,
                        value=upper,
                        unit="USD",
                        **base,
                    ),
                    MetricCalculation(
                        metric_key=BOLLINGER_LOWER_KEY,
                        value=lower,
                        unit="USD",
                        **base,
                    ),
                    MetricCalculation(
                        metric_key=BOLLINGER_BANDWIDTH_KEY,
                        value=(upper - lower) / middle,
                        unit="ratio",
                        **base,
                    ),
                )
            )
            denominator = upper - lower
            if denominator == 0:
                zero_skips[_detail_key(BOLLINGER_PERCENT_B_KEY, window)] += 1
                continue
            output.append(
                MetricCalculation(
                    metric_key=BOLLINGER_PERCENT_B_KEY,
                    value=(bars[-1].close - lower) / denominator,
                    unit="ratio",
                    **base,
                )
            )
        return output

    @staticmethod
    def _ema(
        series: MarketBarSeries,
        window: int,
        warmups: dict[str, int],
    ) -> list[MetricCalculation]:
        """Compute a point-in-time EMA with a linear derived-metric lineage."""
        key = _detail_key(EMA_KEY, window)
        bars = series.bars
        warmups[key] = min(len(bars), window - 1)
        if len(bars) < window:
            return []

        alpha = Decimal("2") / Decimal(window + 1)
        parameters = {
            "window": window,
            "alpha": str(alpha),
            "seed_method": "sma_first_window",
            "seed_start": bars[0].timestamp.isoformat(),
            "price_field": "close",
            "includes_current_bar": True,
            **_common_parameters(series),
        }
        seed_bars = bars[:window]
        seed = MetricCalculation(
            asset_id=seed_bars[-1].asset_id,
            source_id=seed_bars[-1].source_id,
            metric_key=EMA_KEY,
            value=sum((bar.close for bar in seed_bars), Decimal("0")) / Decimal(window),
            unit="USD",
            as_of=seed_bars[-1].timestamp,
            available_at=max(bar.available_at for bar in seed_bars),
            parameters=parameters,
            input_observation_ids=_ids(seed_bars, "close"),
            algorithm_version=_EMA_ALGORITHM,
            quality=_quality(tuple(bar.quality for bar in seed_bars)),
        )
        output = [seed]
        previous = seed
        for current in bars[window:]:
            calculation = MetricCalculation(
                asset_id=current.asset_id,
                source_id=current.source_id,
                metric_key=EMA_KEY,
                value=alpha * current.close + (Decimal("1") - alpha) * previous.value,
                unit="USD",
                as_of=current.timestamp,
                available_at=max(current.available_at, previous.available_at),
                parameters=parameters,
                input_observation_ids=_ids((current,), "close"),
                input_metric_result_ids=(metric_result_id(previous, series.query.known_at),),
                algorithm_version=_EMA_ALGORITHM,
                quality=_quality((current.quality, previous.quality)),
            )
            output.append(calculation)
            previous = calculation
        return output
