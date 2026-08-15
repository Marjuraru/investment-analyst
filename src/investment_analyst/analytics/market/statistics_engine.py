"""Deterministic Decimal-only historical market-statistics engine."""

import json
from collections import Counter
from decimal import Context, Decimal, localcontext
from uuid import UUID

from investment_analyst.analytics.market.bar_models import MarketBar, MarketBarSeries
from investment_analyst.analytics.market.bar_schemas import get_market_bar_schema
from investment_analyst.analytics.market.statistics_definitions import (
    ATR_KEY,
    BOLLINGER_BANDWIDTH_KEY,
    BOLLINGER_LOWER_KEY,
    BOLLINGER_PERCENT_B_KEY,
    BOLLINGER_UPPER_KEY,
    EMA_KEY,
    MACD_HISTOGRAM_KEY,
    MACD_LINE_KEY,
    MACD_SIGNAL_KEY,
    RELATIVE_VOLUME_KEY,
    RSI_AVERAGE_GAIN_KEY,
    RSI_AVERAGE_LOSS_KEY,
    RSI_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    TRUE_RANGE_KEY,
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
_RSI_ALGORITHM = "market-rsi-wilder-v1-decimal34"
_MACD_ALGORITHM = "market-macd-v1-decimal34"
_ATR_ALGORITHM = "market-atr-wilder-v1-decimal34"


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
            calculations.extend(self._rsi(series, request.rsi_window, warmups))
            calculations.extend(self._true_range_and_atr(series, request.atr_window, warmups))
            calculations.extend(self._macd(series, request, warmups))

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

    @staticmethod
    def _rsi(
        series: MarketBarSeries, window: int, warmups: dict[str, int]
    ) -> list[MetricCalculation]:
        bars = series.bars
        for key in (RSI_AVERAGE_GAIN_KEY, RSI_AVERAGE_LOSS_KEY, RSI_KEY):
            warmups[_detail_key(key, window)] = min(len(bars), window)
        if len(bars) <= window:
            return []
        parameters = {
            "window": window,
            "seed_method": "wilder_first_n_changes",
            "seed_start": bars[0].timestamp.isoformat(),
            "price_field": "close",
            **_common_parameters(series),
        }
        output: list[MetricCalculation] = []
        changes = tuple(bars[index].close - bars[index - 1].close for index in range(1, len(bars)))
        gain = sum(
            (max(change, Decimal("0")) for change in changes[:window]), Decimal("0")
        ) / Decimal(window)
        loss = sum(
            (max(-change, Decimal("0")) for change in changes[:window]), Decimal("0")
        ) / Decimal(window)
        previous_gain: MetricCalculation | None = None
        previous_loss: MetricCalculation | None = None
        for index in range(window, len(bars)):
            current = bars[index]
            if index > window:
                change = current.close - bars[index - 1].close
                gain = ((Decimal(window - 1) * gain) + max(change, Decimal("0"))) / Decimal(window)
                loss = ((Decimal(window - 1) * loss) + max(-change, Decimal("0"))) / Decimal(window)
            inputs = bars[: index + 1] if index == window else (current,)
            dependency_ids = (
                ()
                if previous_gain is None
                else (metric_result_id(previous_gain, series.query.known_at),)
            )
            gain_result = MetricCalculation(
                asset_id=current.asset_id,
                source_id=current.source_id,
                metric_key=RSI_AVERAGE_GAIN_KEY,
                value=gain,
                unit="USD",
                as_of=current.timestamp,
                available_at=max(bar.available_at for bar in inputs)
                if previous_gain is None
                else max(current.available_at, previous_gain.available_at),
                parameters=parameters,
                input_observation_ids=_ids(inputs, "close"),
                input_metric_result_ids=dependency_ids,
                algorithm_version=_RSI_ALGORITHM,
                quality=_quality(
                    tuple(bar.quality for bar in inputs)
                    if previous_gain is None
                    else (current.quality, previous_gain.quality)
                ),
            )
            loss_result = MetricCalculation(
                asset_id=current.asset_id,
                source_id=current.source_id,
                metric_key=RSI_AVERAGE_LOSS_KEY,
                value=loss,
                unit="USD",
                as_of=current.timestamp,
                available_at=max(bar.available_at for bar in inputs)
                if previous_loss is None
                else max(current.available_at, previous_loss.available_at),
                parameters=parameters,
                input_observation_ids=_ids(inputs, "close"),
                input_metric_result_ids=()
                if previous_loss is None
                else (metric_result_id(previous_loss, series.query.known_at),),
                algorithm_version=_RSI_ALGORITHM,
                quality=_quality(
                    tuple(bar.quality for bar in inputs)
                    if previous_loss is None
                    else (current.quality, previous_loss.quality)
                ),
            )
            rsi = (
                Decimal("50")
                if gain == 0 and loss == 0
                else Decimal("100")
                if loss == 0
                else Decimal("0")
                if gain == 0
                else Decimal("100") - Decimal("100") / (Decimal("1") + gain / loss)
            )
            output.extend(
                (
                    gain_result,
                    loss_result,
                    MetricCalculation(
                        asset_id=current.asset_id,
                        source_id=current.source_id,
                        metric_key=RSI_KEY,
                        value=rsi,
                        unit="index",
                        as_of=current.timestamp,
                        available_at=max(gain_result.available_at, loss_result.available_at),
                        parameters=parameters,
                        input_observation_ids=_ids((current,), "close"),
                        input_metric_result_ids=(
                            metric_result_id(gain_result, series.query.known_at),
                            metric_result_id(loss_result, series.query.known_at),
                        ),
                        algorithm_version=_RSI_ALGORITHM,
                        quality=_quality((gain_result.quality, loss_result.quality)),
                    ),
                )
            )
            previous_gain, previous_loss = gain_result, loss_result
        return output

    @staticmethod
    def _true_range_and_atr(
        series: MarketBarSeries, window: int, warmups: dict[str, int]
    ) -> list[MetricCalculation]:
        bars = series.bars
        warmups[TRUE_RANGE_KEY] = 0
        warmups[_detail_key(ATR_KEY, window)] = min(len(bars), window - 1)
        parameters = {
            "window": window,
            "seed_method": "mean_first_n_true_ranges",
            "seed_start": bars[0].timestamp.isoformat() if bars else None,
            **_common_parameters(series),
        }
        ranges: list[MetricCalculation] = []
        for index, current in enumerate(bars):
            previous = bars[index - 1] if index else None
            value = (
                current.high - current.low
                if previous is None
                else max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
            inputs = (current,) if previous is None else (previous, current)
            ranges.append(
                MetricCalculation(
                    asset_id=current.asset_id,
                    source_id=current.source_id,
                    metric_key=TRUE_RANGE_KEY,
                    value=value,
                    unit="USD",
                    as_of=current.timestamp,
                    available_at=max(bar.available_at for bar in inputs),
                    parameters={"first_bar_method": "high_low_only", **parameters},
                    input_observation_ids=_ids(inputs, "high")
                    + _ids(inputs, "low")
                    + (() if previous is None else _ids((previous,), "close")),
                    algorithm_version=_ATR_ALGORITHM,
                    quality=_quality(tuple(bar.quality for bar in inputs)),
                )
            )
        output = list(ranges)
        if len(ranges) < window:
            return output
        atr = sum((item.value for item in ranges[:window]), Decimal("0")) / Decimal(window)
        previous_atr: MetricCalculation | None = None
        for index in range(window - 1, len(ranges)):
            current_range = ranges[index]
            if previous_atr is not None:
                atr = ((Decimal(window - 1) * atr) + current_range.value) / Decimal(window)
            observations = bars[:window] if previous_atr is None else (bars[index],)
            output.append(
                MetricCalculation(
                    asset_id=current_range.asset_id,
                    source_id=current_range.source_id,
                    metric_key=ATR_KEY,
                    value=atr,
                    unit="USD",
                    as_of=current_range.as_of,
                    available_at=max(current_range.available_at, previous_atr.available_at)
                    if previous_atr
                    else max(item.available_at for item in ranges[:window]),
                    parameters=parameters,
                    input_observation_ids=_ids(observations, "close"),
                    input_metric_result_ids=(
                        metric_result_id(current_range, series.query.known_at),
                    )
                    + (
                        ()
                        if previous_atr is None
                        else (metric_result_id(previous_atr, series.query.known_at),)
                    ),
                    algorithm_version=_ATR_ALGORITHM,
                    quality=_quality(
                        (current_range.quality,)
                        if previous_atr is None
                        else (current_range.quality, previous_atr.quality)
                    ),
                )
            )
            previous_atr = output[-1]
        return output

    @staticmethod
    def _macd(
        series: MarketBarSeries, request: MarketStatisticsRequest, warmups: dict[str, int]
    ) -> list[MetricCalculation]:
        fast = MarketStatisticsEngine._ema(series, request.macd_fast_window, warmups)
        slow = MarketStatisticsEngine._ema(series, request.macd_slow_window, warmups)
        fast_by_time = {item.as_of: item for item in fast}
        slow_by_time = {item.as_of: item for item in slow}
        parameters = {
            "fast_window": request.macd_fast_window,
            "slow_window": request.macd_slow_window,
            "signal_window": request.macd_signal_window,
            "seed_method": "sma_first_signal_lines",
            "seed_start": series.bars[0].timestamp.isoformat() if series.bars else None,
            **_common_parameters(series),
        }
        lines: list[MetricCalculation] = []
        for timestamp in sorted(set(fast_by_time) & set(slow_by_time)):
            fast_item, slow_item = fast_by_time[timestamp], slow_by_time[timestamp]
            lines.append(
                MetricCalculation(
                    asset_id=fast_item.asset_id,
                    source_id=fast_item.source_id,
                    metric_key=MACD_LINE_KEY,
                    value=fast_item.value - slow_item.value,
                    unit="USD",
                    as_of=timestamp,
                    available_at=max(fast_item.available_at, slow_item.available_at),
                    parameters=parameters,
                    input_observation_ids=fast_item.input_observation_ids,
                    input_metric_result_ids=(
                        metric_result_id(fast_item, series.query.known_at),
                        metric_result_id(slow_item, series.query.known_at),
                    ),
                    algorithm_version=_MACD_ALGORITHM,
                    quality=_quality((fast_item.quality, slow_item.quality)),
                )
            )
        warmups[MACD_LINE_KEY] = min(len(series.bars), request.macd_slow_window - 1)
        warmups[MACD_SIGNAL_KEY] = min(len(lines), request.macd_signal_window - 1)
        warmups[MACD_HISTOGRAM_KEY] = min(len(lines), request.macd_signal_window - 1)
        output: list[MetricCalculation] = [*fast, *slow, *lines]
        if len(lines) < request.macd_signal_window:
            return output
        alpha = Decimal("2") / Decimal(request.macd_signal_window + 1)
        signal = sum(
            (item.value for item in lines[: request.macd_signal_window]), Decimal("0")
        ) / Decimal(request.macd_signal_window)
        previous: MetricCalculation | None = None
        for index in range(request.macd_signal_window - 1, len(lines)):
            line = lines[index]
            if previous is not None:
                signal = alpha * line.value + (Decimal("1") - alpha) * signal
            signal_item = MetricCalculation(
                asset_id=line.asset_id,
                source_id=line.source_id,
                metric_key=MACD_SIGNAL_KEY,
                value=signal,
                unit="USD",
                as_of=line.as_of,
                available_at=max(line.available_at, previous.available_at)
                if previous
                else max(item.available_at for item in lines[: request.macd_signal_window]),
                parameters={**parameters, "alpha": str(alpha)},
                input_observation_ids=line.input_observation_ids,
                input_metric_result_ids=(metric_result_id(line, series.query.known_at),)
                + (
                    () if previous is None else (metric_result_id(previous, series.query.known_at),)
                ),
                algorithm_version=_MACD_ALGORITHM,
                quality=_quality(
                    (line.quality,) if previous is None else (line.quality, previous.quality)
                ),
            )
            output.extend(
                (
                    signal_item,
                    MetricCalculation(
                        asset_id=line.asset_id,
                        source_id=line.source_id,
                        metric_key=MACD_HISTOGRAM_KEY,
                        value=line.value - signal,
                        unit="USD",
                        as_of=line.as_of,
                        available_at=max(line.available_at, signal_item.available_at),
                        parameters=parameters,
                        input_observation_ids=line.input_observation_ids,
                        input_metric_result_ids=(
                            metric_result_id(line, series.query.known_at),
                            metric_result_id(signal_item, series.query.known_at),
                        ),
                        algorithm_version=_MACD_ALGORITHM,
                        quality=_quality((line.quality, signal_item.quality)),
                    ),
                )
            )
            previous = signal_item
        return output
