"""Single-pass causal reconstruction of the three historical features."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

from investment_analyst.analytics.market.incremental_ema import decimal34_mean, ema_step
from investment_analyst.analytics.market.incremental_recursive import wilder_average_step
from investment_analyst.research.historical_models import (
    CLOSE_RETURN_ALGORITHM_VERSION,
    CLOSE_RETURN_COLUMN,
    DECIMAL34,
    FEATURE_RATIO_UNIT,
    MACD_ALGORITHM_VERSION,
    MACD_FAST_WINDOW,
    MACD_HISTOGRAM_COLUMN,
    MACD_SIGNAL_WINDOW,
    MACD_SLOW_WINDOW,
    MACD_WARMUP_BARS,
    RSI_ALGORITHM_VERSION,
    RSI_COLUMN,
    RSI_WARMUP_BARS,
    RSI_WINDOW,
    EvidenceReason,
    FeatureState,
    HistoricalCandle,
    HistoricalFeatureEvidence,
    ReasonCode,
    decision_instant,
    historical_prefix_digest,
)


class HistoricalFeatureError(RuntimeError):
    """Base error for causal historical feature reconstruction."""


class HistoricalFeatureIntegrityError(HistoricalFeatureError):
    """The supplied archive cannot be reconstructed deterministically."""


@dataclass(frozen=True, slots=True)
class HistoricalFeaturePoint:
    """Causal state after one bar of a contiguous daily segment."""

    day: date
    candle: HistoricalCandle
    segment_seed_start: date
    segment_bars: int
    prefix_digest: str
    rsi: Decimal | None
    macd_histogram: Decimal | None


@dataclass(frozen=True, slots=True)
class HistoricalFeatureSeries:
    """Ordered causal state for every bar of one sealed archive."""

    points: Mapping[date, HistoricalFeaturePoint]

    def point(self, day: date) -> HistoricalFeaturePoint | None:
        """Return the causal state after the bar of one day, if present."""
        return self.points.get(day)

    def features(self, decision_date: date) -> tuple[HistoricalFeatureEvidence, ...]:
        """Return the three causal features for one decision date."""
        decision_at = decision_instant(decision_date)
        previous = self.point(decision_date + timedelta(days=-1))
        prior = self.point(decision_date + timedelta(days=-2))
        return (
            _close_return_evidence(previous, prior, decision_at=decision_at),
            _rsi_evidence(previous, decision_at=decision_at),
            _macd_evidence(previous, decision_at=decision_at),
        )

    def feature_identity_inputs(self, decision_date: date) -> tuple[date | None, str]:
        """Return the causal segment seed and prefix digest consumed by the feature hash."""
        previous = self.point(decision_date + timedelta(days=-1))
        if previous is None:
            return None, ""
        return previous.segment_seed_start, previous.prefix_digest


def _reason(code: ReasonCode, detail: str) -> EvidenceReason:
    return EvidenceReason(code=code, detail=detail)


def _segment_warmup_reason(point: HistoricalFeaturePoint, required: int) -> EvidenceReason:
    return _reason(
        ReasonCode.WARMUP_INCOMPLETE,
        f"segment starting {point.segment_seed_start.isoformat()} has"
        f" {point.segment_bars} < {required} contiguous bars",
    )


def _available_reason(decision_at: datetime, point: HistoricalFeaturePoint) -> EvidenceReason:
    return _reason(
        ReasonCode.ASSUMED_AVAILABILITY_AFTER_DECISION,
        f"assumed availability {point.candle.assumed_available_at.isoformat()} is after"
        f" decision {decision_at}",
    )


def _close_return_evidence(
    previous: HistoricalFeaturePoint | None,
    prior: HistoricalFeaturePoint | None,
    *,
    decision_at: datetime,
) -> HistoricalFeatureEvidence:
    if previous is None:
        return HistoricalFeatureEvidence(
            column_name=CLOSE_RETURN_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            reasons=(
                _reason(
                    ReasonCode.BAR_ABSENT,
                    f"no bar exists for the previous decision day of {decision_at}",
                ),
            ),
        )
    if prior is None:
        return HistoricalFeatureEvidence(
            column_name=CLOSE_RETURN_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(
                _reason(
                    ReasonCode.BAR_ABSENT,
                    f"no bar exists for {previous.day - timedelta(days=1)}",
                ),
            ),
        )
    if not _strictly_positive(previous.candle.close) or not _strictly_positive(prior.candle.close):
        return HistoricalFeatureEvidence(
            column_name=CLOSE_RETURN_COLUMN,
            state=FeatureState.INCOMPATIBLE,
            algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(
                _reason(
                    ReasonCode.BAR_INVALID,
                    "close_return_1d requires strictly positive finite closes",
                ),
            ),
        )
    if previous.candle.assumed_available_at > decision_at:
        return HistoricalFeatureEvidence(
            column_name=CLOSE_RETURN_COLUMN,
            state=FeatureState.NOT_YET_AVAILABLE,
            algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(_available_reason(decision_at, previous),),
        )
    with localcontext(DECIMAL34):
        value = previous.candle.close / prior.candle.close - 1
    return HistoricalFeatureEvidence(
        column_name=CLOSE_RETURN_COLUMN,
        state=FeatureState.AVAILABLE,
        value=value,
        algorithm_version=CLOSE_RETURN_ALGORITHM_VERSION,
        unit=FEATURE_RATIO_UNIT,
        segment_seed_start=previous.segment_seed_start,
        warmup_bars=previous.segment_bars,
        assumed_available_at=previous.candle.assumed_available_at,
    )


def _rsi_evidence(
    previous: HistoricalFeaturePoint | None,
    *,
    decision_at: datetime,
) -> HistoricalFeatureEvidence:
    if previous is None:
        return HistoricalFeatureEvidence(
            column_name=RSI_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version=RSI_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            reasons=(_reason(ReasonCode.BAR_ABSENT, "no bar exists for the causal decision day"),),
        )
    if previous.rsi is None:
        return HistoricalFeatureEvidence(
            column_name=RSI_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version=RSI_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(_segment_warmup_reason(previous, RSI_WARMUP_BARS),),
        )
    if previous.candle.assumed_available_at > decision_at:
        return HistoricalFeatureEvidence(
            column_name=RSI_COLUMN,
            state=FeatureState.NOT_YET_AVAILABLE,
            algorithm_version=RSI_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(_available_reason(decision_at, previous),),
        )
    with localcontext(DECIMAL34):
        value = previous.rsi / Decimal(100)
    return HistoricalFeatureEvidence(
        column_name=RSI_COLUMN,
        state=FeatureState.AVAILABLE,
        value=value,
        algorithm_version=RSI_ALGORITHM_VERSION,
        unit=FEATURE_RATIO_UNIT,
        segment_seed_start=previous.segment_seed_start,
        warmup_bars=previous.segment_bars,
        assumed_available_at=previous.candle.assumed_available_at,
    )


def _macd_evidence(
    previous: HistoricalFeaturePoint | None,
    *,
    decision_at: datetime,
) -> HistoricalFeatureEvidence:
    if previous is None:
        return HistoricalFeatureEvidence(
            column_name=MACD_HISTOGRAM_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version=MACD_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            reasons=(_reason(ReasonCode.BAR_ABSENT, "no bar exists for the causal decision day"),),
        )
    if previous.macd_histogram is None:
        return HistoricalFeatureEvidence(
            column_name=MACD_HISTOGRAM_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version=MACD_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(_segment_warmup_reason(previous, MACD_WARMUP_BARS),),
        )
    if not _strictly_positive(previous.candle.close):
        return HistoricalFeatureEvidence(
            column_name=MACD_HISTOGRAM_COLUMN,
            state=FeatureState.INCOMPATIBLE,
            algorithm_version=MACD_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(
                _reason(
                    ReasonCode.BAR_INVALID,
                    "the histogram ratio requires a strictly positive finite close",
                ),
            ),
        )
    if previous.candle.assumed_available_at > decision_at:
        return HistoricalFeatureEvidence(
            column_name=MACD_HISTOGRAM_COLUMN,
            state=FeatureState.NOT_YET_AVAILABLE,
            algorithm_version=MACD_ALGORITHM_VERSION,
            unit=FEATURE_RATIO_UNIT,
            segment_seed_start=previous.segment_seed_start,
            warmup_bars=previous.segment_bars,
            reasons=(_available_reason(decision_at, previous),),
        )
    with localcontext(DECIMAL34):
        value = previous.macd_histogram / previous.candle.close
    return HistoricalFeatureEvidence(
        column_name=MACD_HISTOGRAM_COLUMN,
        state=FeatureState.AVAILABLE,
        value=value,
        algorithm_version=MACD_ALGORITHM_VERSION,
        unit=FEATURE_RATIO_UNIT,
        segment_seed_start=previous.segment_seed_start,
        warmup_bars=previous.segment_bars,
        assumed_available_at=previous.candle.assumed_available_at,
    )


def _strictly_positive(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def _rsi_from_averages(gain: Decimal, loss: Decimal) -> Decimal:
    """Combine Wilder averages exactly as the canonical market implementation."""
    with localcontext(DECIMAL34):
        if gain == 0 and loss == 0:
            return Decimal("50")
        if loss == 0:
            return Decimal("100")
        if gain == 0:
            return Decimal("0")
        return Decimal("100") - Decimal("100") / (Decimal("1") + gain / loss)


def _ema_series(closes: tuple[Decimal, ...], window: int) -> tuple[Decimal, ...]:
    seed = decimal34_mean(closes[:window])
    values = [seed]
    for close in closes[window:]:
        values.append(ema_step(values[-1], close, window))
    return tuple(values)


def _macd_seed(closes: tuple[Decimal, ...]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return the canonical fast/slow/signal/histogram seed for 34 contiguous closes."""
    if len(closes) != MACD_WARMUP_BARS:
        raise HistoricalFeatureIntegrityError("MACD seed requires exactly 34 contiguous closes")
    fast_series = _ema_series(closes, MACD_FAST_WINDOW)
    slow_series = _ema_series(closes, MACD_SLOW_WINDOW)
    offset = MACD_SLOW_WINDOW - MACD_FAST_WINDOW
    with localcontext(DECIMAL34):
        lines = tuple(
            fast - slow for fast, slow in zip(fast_series[offset:], slow_series, strict=True)
        )
        signal = decimal34_mean(lines[:MACD_SIGNAL_WINDOW])
        fast = fast_series[MACD_WARMUP_BARS - MACD_FAST_WINDOW]
        slow = slow_series[MACD_WARMUP_BARS - MACD_SLOW_WINDOW]
        line = lines[-1]
        histogram = line - signal
    return fast, slow, signal, histogram


@dataclass(slots=True)
class _SegmentState:
    seed_start: date
    bars: int = 0
    previous_close: Decimal | None = None
    closes: list[Decimal] | None = None
    average_gain: Decimal | None = None
    average_loss: Decimal | None = None
    rsi: Decimal | None = None
    fast_ema: Decimal | None = None
    slow_ema: Decimal | None = None
    signal: Decimal | None = None
    macd_histogram: Decimal | None = None
    prefix_digest: str | None = None


def reconstruct_feature_series(
    candles: tuple[HistoricalCandle, ...],
) -> HistoricalFeatureSeries:
    """Reconstruct every causal feature state in one chronological pass."""
    points: dict[date, HistoricalFeaturePoint] = {}
    state: _SegmentState | None = None
    for candle in candles:
        day = candle.bar_start.date()
        if state is None or state.seed_start + timedelta(days=state.bars) != day:
            state = _SegmentState(seed_start=day, closes=[])
        state.bars += 1
        state.prefix_digest = historical_prefix_digest(
            state.prefix_digest,
            bar_content_id=candle.bar_content_id,
            close=candle.close,
        )
        if state.closes is not None:
            state.closes.append(candle.close)
        _advance_rsi(state, candle.close)
        _advance_macd(state, candle.close)
        state.previous_close = candle.close
        points[day] = HistoricalFeaturePoint(
            day=day,
            candle=candle,
            segment_seed_start=state.seed_start,
            segment_bars=state.bars,
            prefix_digest=state.prefix_digest,
            rsi=state.rsi,
            macd_histogram=state.macd_histogram,
        )
    return HistoricalFeatureSeries(points=points)


def _advance_rsi(state: _SegmentState, close: Decimal) -> None:
    if state.bars == RSI_WARMUP_BARS:
        closes = tuple((state.closes or [])[-RSI_WARMUP_BARS:])
        if len(closes) != RSI_WARMUP_BARS:
            raise HistoricalFeatureIntegrityError("RSI seed lacks its contiguous prefix")
        with localcontext(DECIMAL34):
            changes = tuple(closes[index] - closes[index - 1] for index in range(1, len(closes)))
            state.average_gain = decimal34_mean(
                tuple(max(change, Decimal("0")) for change in changes)
            )
            state.average_loss = decimal34_mean(
                tuple(max(-change, Decimal("0")) for change in changes)
            )
            state.rsi = _rsi_from_averages(state.average_gain, state.average_loss)
        return
    if state.bars > RSI_WARMUP_BARS:
        if state.average_gain is None or state.average_loss is None:
            raise HistoricalFeatureIntegrityError("RSI recurrence lacks its seeded averages")
        if state.previous_close is None:
            raise HistoricalFeatureIntegrityError("RSI recurrence lacks its previous close")
        with localcontext(DECIMAL34):
            change = close - state.previous_close
            state.average_gain = wilder_average_step(
                state.average_gain,
                max(change, Decimal("0")),
                RSI_WINDOW,
            )
            state.average_loss = wilder_average_step(
                state.average_loss,
                max(-change, Decimal("0")),
                RSI_WINDOW,
            )
            state.rsi = _rsi_from_averages(state.average_gain, state.average_loss)


def _advance_macd(state: _SegmentState, close: Decimal) -> None:
    if state.bars == MACD_WARMUP_BARS:
        closes = tuple((state.closes or [])[-MACD_WARMUP_BARS:])
        if len(closes) != MACD_WARMUP_BARS:
            raise HistoricalFeatureIntegrityError("MACD seed lacks its contiguous prefix")
        fast, slow, signal, histogram = _macd_seed(closes)
        state.fast_ema = fast
        state.slow_ema = slow
        state.signal = signal
        state.macd_histogram = histogram
        state.closes = None
        return
    if state.bars > MACD_WARMUP_BARS:
        if state.fast_ema is None or state.slow_ema is None or state.signal is None:
            raise HistoricalFeatureIntegrityError("MACD recurrence lacks its seeded state")
        with localcontext(DECIMAL34):
            state.fast_ema = ema_step(state.fast_ema, close, MACD_FAST_WINDOW)
            state.slow_ema = ema_step(state.slow_ema, close, MACD_SLOW_WINDOW)
            line = state.fast_ema - state.slow_ema
            state.signal = ema_step(state.signal, line, MACD_SIGNAL_WINDOW)
            state.macd_histogram = line - state.signal


__all__ = [
    "HistoricalFeatureError",
    "HistoricalFeatureIntegrityError",
    "HistoricalFeaturePoint",
    "HistoricalFeatureSeries",
    "reconstruct_feature_series",
]
