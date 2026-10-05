"""Canonical incremental RSI/ATR/MACD state for daily market bars.

Pure, storage-free contract for the DATA-CHASSIS stage 5 recursive graph:
three demonstrable daily recurrences (RSI Wilder, true range/ATR Wilder and
MACD over SMA-seeded close EMAs) with a canonical seed, an ordered
observation-ID prefix digest and a verifiable checkpoint each. It never reads
storage or the filesystem, never writes, never uses a clock and never imports
the statistics engine, pipeline or service. Nothing in production imports it
yet. Versioned independently from v1 engine results; persisted v1 rows are
never reinterpreted as v2.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.incremental_ema import (
    IncrementalEmaPrefixError,
    IncrementalEmaScopeError,
    _ordered_scope_bars,
    _prefix_ids,
    canonical_prefix_hash,
    decimal34_mean,
    ema_step,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency, DataQuality

RSI_ALGORITHM_VERSION = "market-rsi-incremental-v2-decimal34"
ATR_ALGORITHM_VERSION = "market-atr-incremental-v2-decimal34"
MACD_ALGORITHM_VERSION = "market-macd-incremental-v2-decimal34"
RECURSIVE_SEED_POLICY = "incremental-recursive-seed-first-window-v1"
_RSI_CHECKPOINT_LABEL = "incremental-rsi-checkpoint-v1"
_ATR_CHECKPOINT_LABEL = "incremental-atr-checkpoint-v1"
_MACD_CHECKPOINT_LABEL = "incremental-macd-checkpoint-v1"
_MAX_WINDOW = 400


class IncrementalRecursiveError(RuntimeError):
    """Base error for the canonical incremental RSI/ATR/MACD contract."""


class IncrementalRecursiveScopeError(IncrementalRecursiveError, IncrementalEmaScopeError):
    """Raised when bars or windows leave the declared asset, source or scope."""


class IncrementalRecursivePrefixError(IncrementalRecursiveError, IncrementalEmaPrefixError):
    """Raised when the visible ordered prefix is absent, truncated or revised."""


class IncrementalRecursiveCheckpointError(IncrementalRecursiveError):
    """Raised when a checkpoint digest, value or identity is corrupt."""


def _identity(label: str, document: dict[str, object]) -> UUID:
    encoded = json.dumps(
        {"label": label, **document},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).digest()
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _validate_window(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 2:
        raise ValueError(f"{name} must be at least 2")
    if value > _MAX_WINDOW:
        raise ValueError(f"{name} must not exceed 400")
    return value


def _validate_macd_windows(fast: int, slow: int, signal: int) -> tuple[int, int, int]:
    fast = _validate_window(fast, name="macd_fast_window")
    slow = _validate_window(slow, name="macd_slow_window")
    signal = _validate_window(signal, name="macd_signal_window")
    if fast >= slow:
        raise ValueError("macd_fast_window must be less than macd_slow_window")
    return fast, slow, signal


def _quality(values: Sequence[DataQuality]) -> DataQuality:
    for candidate in (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    ):
        if candidate in values:
            return candidate
    raise IncrementalRecursiveScopeError("recursive graph has no input quality")


def _max_available(bars: Sequence[MarketBar]) -> datetime:
    available = bars[0].available_at
    for bar in bars[1:]:
        if bar.available_at > available:
            available = bar.available_at
    return available


def _rsi_from_averages(gain: Decimal, loss: Decimal) -> Decimal:
    if gain == 0 and loss == 0:
        return Decimal("50")
    if loss == 0:
        return Decimal("100")
    if gain == 0:
        return Decimal("0")
    return Decimal("100") - Decimal("100") / (Decimal("1") + gain / loss)


def wilder_average_step(previous: Decimal, current: Decimal, window: int) -> Decimal:
    """Apply one canonical Decimal34 Wilder-average recurrence step."""
    window = _validate_window(window, name="window")
    if not previous.is_finite() or not current.is_finite():
        raise ValueError("Wilder step values must be finite")
    with localcontext(Context(prec=34)):
        return ((Decimal(window - 1) * previous) + current) / Decimal(window)


def _true_range(current: MarketBar, previous: MarketBar | None) -> Decimal:
    return true_range_from_previous_close(current, previous.close if previous is not None else None)


def true_range_from_previous_close(
    current: MarketBar,
    previous_close: Decimal | None,
) -> Decimal:
    """Return canonical true range from one current bar and optional prior close."""
    if previous_close is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous_close),
        abs(current.low - previous_close),
    )


def _rsi_checkpoint_identity(
    *,
    asset_id: str,
    source_id: str,
    window: int,
    seed_start: datetime,
    average_gain: Decimal,
    average_loss: Decimal,
    rsi: Decimal,
    as_of: datetime,
    available_at: datetime,
    prefix_length: int,
    prefix_hash: str,
) -> UUID:
    return _identity(
        _RSI_CHECKPOINT_LABEL,
        {
            "algorithm_version": RSI_ALGORITHM_VERSION,
            "asset_id": asset_id,
            "as_of": as_of.astimezone(UTC).isoformat(),
            "available_at": available_at.astimezone(UTC).isoformat(),
            "average_gain": format(average_gain, "f"),
            "average_loss": format(average_loss, "f"),
            "prefix_hash": prefix_hash,
            "prefix_length": prefix_length,
            "rsi": format(rsi, "f"),
            "seed_start": seed_start.astimezone(UTC).isoformat(),
            "source_id": source_id,
            "window": window,
        },
    )


def _atr_checkpoint_identity(
    *,
    asset_id: str,
    source_id: str,
    window: int,
    seed_start: datetime,
    true_range: Decimal,
    atr: Decimal,
    as_of: datetime,
    available_at: datetime,
    prefix_length: int,
    prefix_hash: str,
) -> UUID:
    return _identity(
        _ATR_CHECKPOINT_LABEL,
        {
            "algorithm_version": ATR_ALGORITHM_VERSION,
            "asset_id": asset_id,
            "as_of": as_of.astimezone(UTC).isoformat(),
            "atr": format(atr, "f"),
            "available_at": available_at.astimezone(UTC).isoformat(),
            "prefix_hash": prefix_hash,
            "prefix_length": prefix_length,
            "seed_start": seed_start.astimezone(UTC).isoformat(),
            "source_id": source_id,
            "true_range": format(true_range, "f"),
            "window": window,
        },
    )


def _macd_checkpoint_identity(
    *,
    asset_id: str,
    source_id: str,
    fast_window: int,
    slow_window: int,
    signal_window: int,
    seed_start: datetime,
    fast_ema: Decimal,
    slow_ema: Decimal,
    line: Decimal,
    signal: Decimal,
    histogram: Decimal,
    as_of: datetime,
    available_at: datetime,
    prefix_length: int,
    prefix_hash: str,
) -> UUID:
    return _identity(
        _MACD_CHECKPOINT_LABEL,
        {
            "algorithm_version": MACD_ALGORITHM_VERSION,
            "asset_id": asset_id,
            "as_of": as_of.astimezone(UTC).isoformat(),
            "available_at": available_at.astimezone(UTC).isoformat(),
            "fast_ema": format(fast_ema, "f"),
            "fast_window": fast_window,
            "histogram": format(histogram, "f"),
            "line": format(line, "f"),
            "prefix_hash": prefix_hash,
            "prefix_length": prefix_length,
            "seed_start": seed_start.astimezone(UTC).isoformat(),
            "signal": format(signal, "f"),
            "signal_window": signal_window,
            "slow_ema": format(slow_ema, "f"),
            "slow_window": slow_window,
            "source_id": source_id,
        },
    )


class IncrementalRsiCheckpoint(ContractModel):
    """Verifiable Wilder RSI state of one daily close prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: UUID
    algorithm_version: Literal["market-rsi-incremental-v2-decimal34"] = RSI_ALGORITHM_VERSION
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: Literal[DataFrequency.DAY_1] = DataFrequency.DAY_1
    window: int = Field(ge=2, le=_MAX_WINDOW)
    seed_start: UTCDateTime
    average_gain: Decimal
    average_loss: Decimal
    rsi: Decimal
    as_of: UTCDateTime
    available_at: UTCDateTime
    prefix_length: int = Field(ge=1)
    prefix_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_checkpoint(self) -> IncrementalRsiCheckpoint:
        """Require finite values and a deterministic checkpoint identity."""
        for name in ("average_gain", "average_loss", "rsi"):
            if not getattr(self, name).is_finite():
                raise ValueError(f"checkpoint {name} must be finite")
        if self.as_of < self.seed_start:
            raise ValueError("checkpoint as_of must not precede seed_start")
        if self.available_at < self.as_of:
            raise ValueError("checkpoint available_at must not precede as_of")
        if self.checkpoint_id != _rsi_checkpoint_identity(
            asset_id=self.asset_id,
            source_id=self.source_id,
            window=self.window,
            seed_start=self.seed_start,
            average_gain=self.average_gain,
            average_loss=self.average_loss,
            rsi=self.rsi,
            as_of=self.as_of,
            available_at=self.available_at,
            prefix_length=self.prefix_length,
            prefix_hash=self.prefix_hash,
        ):
            raise ValueError("checkpoint identity is not deterministic")
        return self


class IncrementalAtrCheckpoint(ContractModel):
    """Verifiable Wilder true range/ATR state of one daily bar prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: UUID
    algorithm_version: Literal["market-atr-incremental-v2-decimal34"] = ATR_ALGORITHM_VERSION
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: Literal[DataFrequency.DAY_1] = DataFrequency.DAY_1
    window: int = Field(ge=2, le=_MAX_WINDOW)
    seed_start: UTCDateTime
    true_range: Decimal
    atr: Decimal
    as_of: UTCDateTime
    available_at: UTCDateTime
    prefix_length: int = Field(ge=1)
    prefix_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_checkpoint(self) -> IncrementalAtrCheckpoint:
        """Require finite values and a deterministic checkpoint identity."""
        for name in ("true_range", "atr"):
            if not getattr(self, name).is_finite():
                raise ValueError(f"checkpoint {name} must be finite")
        if self.as_of < self.seed_start:
            raise ValueError("checkpoint as_of must not precede seed_start")
        if self.available_at < self.as_of:
            raise ValueError("checkpoint available_at must not precede as_of")
        if self.checkpoint_id != _atr_checkpoint_identity(
            asset_id=self.asset_id,
            source_id=self.source_id,
            window=self.window,
            seed_start=self.seed_start,
            true_range=self.true_range,
            atr=self.atr,
            as_of=self.as_of,
            available_at=self.available_at,
            prefix_length=self.prefix_length,
            prefix_hash=self.prefix_hash,
        ):
            raise ValueError("checkpoint identity is not deterministic")
        return self


class IncrementalMacdCheckpoint(ContractModel):
    """Verifiable MACD state of one daily close prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: UUID
    algorithm_version: Literal["market-macd-incremental-v2-decimal34"] = MACD_ALGORITHM_VERSION
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: Literal[DataFrequency.DAY_1] = DataFrequency.DAY_1
    fast_window: int = Field(ge=2, le=_MAX_WINDOW)
    slow_window: int = Field(ge=2, le=_MAX_WINDOW)
    signal_window: int = Field(ge=2, le=_MAX_WINDOW)
    seed_start: UTCDateTime
    fast_ema: Decimal
    slow_ema: Decimal
    line: Decimal
    signal: Decimal
    histogram: Decimal
    as_of: UTCDateTime
    available_at: UTCDateTime
    prefix_length: int = Field(ge=1)
    prefix_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_checkpoint(self) -> IncrementalMacdCheckpoint:
        """Require finite values, ordered windows and a deterministic identity."""
        for name in ("fast_ema", "slow_ema", "line", "signal", "histogram"):
            if not getattr(self, name).is_finite():
                raise ValueError(f"checkpoint {name} must be finite")
        if self.fast_window >= self.slow_window:
            raise ValueError("macd_fast_window must be less than macd_slow_window")
        if self.as_of < self.seed_start:
            raise ValueError("checkpoint as_of must not precede seed_start")
        if self.available_at < self.as_of:
            raise ValueError("checkpoint available_at must not precede as_of")
        if self.checkpoint_id != _macd_checkpoint_identity(
            asset_id=self.asset_id,
            source_id=self.source_id,
            fast_window=self.fast_window,
            slow_window=self.slow_window,
            signal_window=self.signal_window,
            seed_start=self.seed_start,
            fast_ema=self.fast_ema,
            slow_ema=self.slow_ema,
            line=self.line,
            signal=self.signal,
            histogram=self.histogram,
            as_of=self.as_of,
            available_at=self.available_at,
            prefix_length=self.prefix_length,
            prefix_hash=self.prefix_hash,
        ):
            raise ValueError("checkpoint identity is not deterministic")
        return self


def _verify_scope_windows(
    ordered: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
    minimum_bars: int,
) -> tuple[MarketBar, ...]:
    scoped = _ordered_scope_bars(ordered, asset_id=asset_id, source_id=source_id, window=window)
    if len(scoped) < minimum_bars:
        raise IncrementalRecursivePrefixError("visible prefix is shorter than the warm-up")
    return scoped


def seed_rsi(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> IncrementalRsiCheckpoint:
    """Seed canonical Wilder RSI from the first window of daily changes."""
    window = _validate_window(window, name="window")
    ordered = _verify_scope_windows(
        bars, asset_id=asset_id, source_id=source_id, window=window, minimum_bars=window + 1
    )
    seed_bars = ordered[: window + 1]
    with localcontext(Context(prec=34)):
        changes = tuple(
            seed_bars[index].close - seed_bars[index - 1].close
            for index in range(1, len(seed_bars))
        )
        average_gain = decimal34_mean(tuple(max(change, Decimal("0")) for change in changes))
        average_loss = decimal34_mean(tuple(max(-change, Decimal("0")) for change in changes))
        rsi = _rsi_from_averages(average_gain, average_loss)
    prefix_ids = _prefix_ids(seed_bars)
    available_at = _max_available(seed_bars)
    return IncrementalRsiCheckpoint(
        checkpoint_id=_rsi_checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            window=window,
            seed_start=ordered[0].timestamp,
            average_gain=average_gain,
            average_loss=average_loss,
            rsi=rsi,
            as_of=seed_bars[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        window=window,
        seed_start=ordered[0].timestamp,
        average_gain=average_gain,
        average_loss=average_loss,
        rsi=rsi,
        as_of=seed_bars[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )


def seed_atr(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> IncrementalAtrCheckpoint:
    """Seed canonical Wilder ATR from the first window of true ranges."""
    window = _validate_window(window, name="window")
    ordered = _verify_scope_windows(
        bars, asset_id=asset_id, source_id=source_id, window=window, minimum_bars=window
    )
    seed_bars = ordered[:window]
    with localcontext(Context(prec=34)):
        ranges = tuple(
            _true_range(current, seed_bars[index - 1] if index else None)
            for index, current in enumerate(seed_bars)
        )
        atr = decimal34_mean(ranges)
    prefix_ids = _prefix_ids(seed_bars)
    available_at = _max_available(seed_bars)
    return IncrementalAtrCheckpoint(
        checkpoint_id=_atr_checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            window=window,
            seed_start=ordered[0].timestamp,
            true_range=ranges[-1],
            atr=atr,
            as_of=seed_bars[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        window=window,
        seed_start=ordered[0].timestamp,
        true_range=ranges[-1],
        atr=atr,
        as_of=seed_bars[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )


def _ema_values(closes: Sequence[Decimal], window: int) -> list[Decimal]:
    seed = decimal34_mean(closes[:window])
    values = [seed]
    for close in closes[window:]:
        values.append(ema_step(values[-1], close, window))
    return values


def _macd_line_series(closes: Sequence[Decimal], fast: int, slow: int) -> list[Decimal]:
    fast_values = _ema_values(closes, fast)
    slow_values = _ema_values(closes, slow)
    offset = slow - fast
    with localcontext(Context(prec=34)):
        return [fast - slow for fast, slow in zip(fast_values[offset:], slow_values, strict=True)]


def seed_macd(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    fast_window: int,
    slow_window: int,
    signal_window: int,
) -> IncrementalMacdCheckpoint:
    """Seed canonical MACD from the first eligible signal line."""
    fast, slow, signal_window = _validate_macd_windows(fast_window, slow_window, signal_window)
    minimum_bars = slow + signal_window - 1
    ordered = _ordered_scope_bars(bars, asset_id=asset_id, source_id=source_id, window=slow)
    if len(ordered) < minimum_bars:
        raise IncrementalRecursivePrefixError("visible prefix is shorter than the warm-up")
    seed_bars = ordered[:minimum_bars]
    closes = tuple(bar.close for bar in seed_bars)
    fast_series = _ema_values(closes, fast)
    slow_series = _ema_values(closes, slow)
    lines = _macd_line_series(closes, fast, slow)
    with localcontext(Context(prec=34)):
        signal = decimal34_mean(lines[:signal_window])
        fast_ema = fast_series[minimum_bars - fast]
        slow_ema = slow_series[minimum_bars - slow]
        line = lines[-1]
        histogram = line - signal
    prefix_ids = _prefix_ids(seed_bars)
    available_at = _max_available(seed_bars)
    return IncrementalMacdCheckpoint(
        checkpoint_id=_macd_checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            fast_window=fast,
            slow_window=slow,
            signal_window=signal_window,
            seed_start=ordered[0].timestamp,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            line=line,
            signal=signal,
            histogram=histogram,
            as_of=seed_bars[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        fast_window=fast,
        slow_window=slow,
        signal_window=signal_window,
        seed_start=ordered[0].timestamp,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        line=line,
        signal=signal,
        histogram=histogram,
        as_of=seed_bars[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )


def _require_known_at(known_at: datetime) -> None:
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise IncrementalRecursiveCheckpointError("known_at must be timezone-aware")


def _verify_checkpoint_prefix(
    ordered: Sequence[MarketBar],
    checkpoint: IncrementalRsiCheckpoint | IncrementalAtrCheckpoint | IncrementalMacdCheckpoint,
    *,
    known_at: datetime,
) -> tuple[MarketBar, ...]:
    _require_known_at(known_at)
    scoped = _ordered_scope_bars(
        ordered,
        asset_id=checkpoint.asset_id,
        source_id=checkpoint.source_id,
        window=(
            checkpoint.window
            if not isinstance(checkpoint, IncrementalMacdCheckpoint)
            else checkpoint.slow_window
        ),
    )
    if checkpoint.available_at > known_at:
        raise IncrementalRecursiveCheckpointError("checkpoint is not available at known_at")
    for bar in scoped:
        if bar.available_at > known_at:
            raise IncrementalRecursivePrefixError("visible prefix contains future evidence")
    visible = scoped[: checkpoint.prefix_length]
    if len(visible) != checkpoint.prefix_length:
        raise IncrementalRecursivePrefixError("checkpoint prefix is not fully visible")
    if canonical_prefix_hash(_prefix_ids(visible)) != checkpoint.prefix_hash:
        raise IncrementalRecursivePrefixError("checkpoint prefix was revised or truncated")
    if visible[0].timestamp != checkpoint.seed_start:
        raise IncrementalRecursivePrefixError("checkpoint seed does not match visible history")
    if visible[-1].timestamp != checkpoint.as_of:
        raise IncrementalRecursivePrefixError("checkpoint head does not match visible history")
    return scoped


def validate_rsi_checkpoint(
    checkpoint: IncrementalRsiCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> tuple[MarketBar, ...]:
    """Verify an RSI checkpoint against the full visible prefix at a cut."""
    _validate_window(checkpoint.window, name="window")
    return _verify_checkpoint_prefix(bars, checkpoint, known_at=known_at)


def validate_atr_checkpoint(
    checkpoint: IncrementalAtrCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> tuple[MarketBar, ...]:
    """Verify an ATR checkpoint against the full visible prefix at a cut."""
    _validate_window(checkpoint.window, name="window")
    return _verify_checkpoint_prefix(bars, checkpoint, known_at=known_at)


def validate_macd_checkpoint(
    checkpoint: IncrementalMacdCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> tuple[MarketBar, ...]:
    """Verify a MACD checkpoint against the full visible prefix at a cut."""
    _validate_macd_windows(checkpoint.fast_window, checkpoint.slow_window, checkpoint.signal_window)
    return _verify_checkpoint_prefix(bars, checkpoint, known_at=known_at)


def resume_rsi(
    checkpoint: IncrementalRsiCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> IncrementalRsiCheckpoint:
    """Continue a verified RSI checkpoint over successive available daily bars."""
    ordered = validate_rsi_checkpoint(checkpoint, bars, known_at=known_at)
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    window = _validate_window(checkpoint.window, name="window")
    with localcontext(Context(prec=34)):
        average_gain = checkpoint.average_gain
        average_loss = checkpoint.average_loss
        available_at = checkpoint.available_at
        previous_close = ordered[checkpoint.prefix_length - 1].close
        for current in tail:
            change = current.close - previous_close
            average_gain = wilder_average_step(average_gain, max(change, Decimal("0")), window)
            average_loss = wilder_average_step(average_loss, max(-change, Decimal("0")), window)
            previous_close = current.close
            available_at = max(current.available_at, available_at)
        rsi = _rsi_from_averages(average_gain, average_loss)
    prefix_ids = (*_prefix_ids(ordered[: checkpoint.prefix_length]), *_prefix_ids(tail))
    extended = ordered[: checkpoint.prefix_length] + tail
    prefix_hash = canonical_prefix_hash(prefix_ids)
    return IncrementalRsiCheckpoint(
        checkpoint_id=_rsi_checkpoint_identity(
            asset_id=checkpoint.asset_id,
            source_id=checkpoint.source_id,
            window=window,
            seed_start=checkpoint.seed_start,
            average_gain=average_gain,
            average_loss=average_loss,
            rsi=rsi,
            as_of=extended[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=prefix_hash,
        ),
        asset_id=checkpoint.asset_id,
        source_id=checkpoint.source_id,
        window=window,
        seed_start=checkpoint.seed_start,
        average_gain=average_gain,
        average_loss=average_loss,
        rsi=rsi,
        as_of=extended[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=prefix_hash,
    )


def resume_atr(
    checkpoint: IncrementalAtrCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> IncrementalAtrCheckpoint:
    """Continue a verified ATR checkpoint over successive available daily bars."""
    ordered = validate_atr_checkpoint(checkpoint, bars, known_at=known_at)
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    window = _validate_window(checkpoint.window, name="window")
    with localcontext(Context(prec=34)):
        atr = checkpoint.atr
        available_at = checkpoint.available_at
        previous_close = ordered[checkpoint.prefix_length - 1].close
        true_range = checkpoint.true_range
        for current in tail:
            true_range = max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
            atr = wilder_average_step(atr, true_range, window)
            previous_close = current.close
            available_at = max(current.available_at, available_at)
    prefix_ids = (*_prefix_ids(ordered[: checkpoint.prefix_length]), *_prefix_ids(tail))
    extended = ordered[: checkpoint.prefix_length] + tail
    prefix_hash = canonical_prefix_hash(prefix_ids)
    return IncrementalAtrCheckpoint(
        checkpoint_id=_atr_checkpoint_identity(
            asset_id=checkpoint.asset_id,
            source_id=checkpoint.source_id,
            window=window,
            seed_start=checkpoint.seed_start,
            true_range=true_range,
            atr=atr,
            as_of=extended[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=prefix_hash,
        ),
        asset_id=checkpoint.asset_id,
        source_id=checkpoint.source_id,
        window=window,
        seed_start=checkpoint.seed_start,
        true_range=true_range,
        atr=atr,
        as_of=extended[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=prefix_hash,
    )


def resume_macd(
    checkpoint: IncrementalMacdCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> IncrementalMacdCheckpoint:
    """Continue a verified MACD checkpoint over successive available daily bars."""
    ordered = validate_macd_checkpoint(checkpoint, bars, known_at=known_at)
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    fast, slow, signal_window = _validate_macd_windows(
        checkpoint.fast_window, checkpoint.slow_window, checkpoint.signal_window
    )
    with localcontext(Context(prec=34)):
        fast_ema = checkpoint.fast_ema
        slow_ema = checkpoint.slow_ema
        signal = checkpoint.signal
        available_at = checkpoint.available_at
        for current in tail:
            fast_ema = ema_step(fast_ema, current.close, fast)
            slow_ema = ema_step(slow_ema, current.close, slow)
            line = fast_ema - slow_ema
            signal = ema_step(signal, line, signal_window)
            available_at = max(current.available_at, available_at)
        line = fast_ema - slow_ema
        histogram = line - signal
    prefix_ids = (*_prefix_ids(ordered[: checkpoint.prefix_length]), *_prefix_ids(tail))
    extended = ordered[: checkpoint.prefix_length] + tail
    prefix_hash = canonical_prefix_hash(prefix_ids)
    return IncrementalMacdCheckpoint(
        checkpoint_id=_macd_checkpoint_identity(
            asset_id=checkpoint.asset_id,
            source_id=checkpoint.source_id,
            fast_window=fast,
            slow_window=slow,
            signal_window=signal_window,
            seed_start=checkpoint.seed_start,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            line=line,
            signal=signal,
            histogram=histogram,
            as_of=extended[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=prefix_hash,
        ),
        asset_id=checkpoint.asset_id,
        source_id=checkpoint.source_id,
        fast_window=fast,
        slow_window=slow,
        signal_window=signal_window,
        seed_start=checkpoint.seed_start,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        line=line,
        signal=signal,
        histogram=histogram,
        as_of=extended[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=prefix_hash,
    )


def full_rsi(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> IncrementalRsiCheckpoint:
    """Compute canonical Wilder RSI over the full visible prefix in one pass."""
    checkpoint = seed_rsi(bars, asset_id=asset_id, source_id=source_id, window=window)
    ordered = _verify_scope_windows(
        bars, asset_id=asset_id, source_id=source_id, window=window, minimum_bars=window + 1
    )
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    window = _validate_window(window, name="window")
    with localcontext(Context(prec=34)):
        average_gain = checkpoint.average_gain
        average_loss = checkpoint.average_loss
        available_at = checkpoint.available_at
        previous_close = ordered[checkpoint.prefix_length - 1].close
        for current in tail:
            change = current.close - previous_close
            average_gain = wilder_average_step(average_gain, max(change, Decimal("0")), window)
            average_loss = wilder_average_step(average_loss, max(-change, Decimal("0")), window)
            previous_close = current.close
            available_at = max(current.available_at, available_at)
        rsi = _rsi_from_averages(average_gain, average_loss)
    prefix_ids = _prefix_ids(ordered)
    return IncrementalRsiCheckpoint(
        checkpoint_id=_rsi_checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            window=window,
            seed_start=ordered[0].timestamp,
            average_gain=average_gain,
            average_loss=average_loss,
            rsi=rsi,
            as_of=ordered[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        window=window,
        seed_start=ordered[0].timestamp,
        average_gain=average_gain,
        average_loss=average_loss,
        rsi=rsi,
        as_of=ordered[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )


def full_atr(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> IncrementalAtrCheckpoint:
    """Compute canonical Wilder ATR over the full visible prefix in one pass."""
    checkpoint = seed_atr(bars, asset_id=asset_id, source_id=source_id, window=window)
    ordered = _verify_scope_windows(
        bars, asset_id=asset_id, source_id=source_id, window=window, minimum_bars=window
    )
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    window = _validate_window(window, name="window")
    with localcontext(Context(prec=34)):
        atr = checkpoint.atr
        available_at = checkpoint.available_at
        previous_close = ordered[checkpoint.prefix_length - 1].close
        true_range = checkpoint.true_range
        for current in tail:
            true_range = max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
            atr = wilder_average_step(atr, true_range, window)
            previous_close = current.close
            available_at = max(current.available_at, available_at)
    prefix_ids = _prefix_ids(ordered)
    return IncrementalAtrCheckpoint(
        checkpoint_id=_atr_checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            window=window,
            seed_start=ordered[0].timestamp,
            true_range=true_range,
            atr=atr,
            as_of=ordered[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        window=window,
        seed_start=ordered[0].timestamp,
        true_range=true_range,
        atr=atr,
        as_of=ordered[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )


def full_macd(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    fast_window: int,
    slow_window: int,
    signal_window: int,
) -> IncrementalMacdCheckpoint:
    """Compute canonical MACD over the full visible prefix in one pass."""
    checkpoint = seed_macd(
        bars,
        asset_id=asset_id,
        source_id=source_id,
        fast_window=fast_window,
        slow_window=slow_window,
        signal_window=signal_window,
    )
    fast, slow, signal_window = _validate_macd_windows(fast_window, slow_window, signal_window)
    minimum_bars = slow + signal_window - 1
    ordered = _ordered_scope_bars(bars, asset_id=asset_id, source_id=source_id, window=slow)
    if len(ordered) < minimum_bars:
        raise IncrementalRecursivePrefixError("visible prefix is shorter than the warm-up")
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    with localcontext(Context(prec=34)):
        fast_ema = checkpoint.fast_ema
        slow_ema = checkpoint.slow_ema
        signal = checkpoint.signal
        available_at = checkpoint.available_at
        for current in tail:
            fast_ema = ema_step(fast_ema, current.close, fast)
            slow_ema = ema_step(slow_ema, current.close, slow)
            line = fast_ema - slow_ema
            signal = ema_step(signal, line, signal_window)
            available_at = max(current.available_at, available_at)
        line = fast_ema - slow_ema
        histogram = line - signal
    prefix_ids = _prefix_ids(ordered)
    return IncrementalMacdCheckpoint(
        checkpoint_id=_macd_checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            fast_window=fast,
            slow_window=slow,
            signal_window=signal_window,
            seed_start=ordered[0].timestamp,
            fast_ema=fast_ema,
            slow_ema=slow_ema,
            line=line,
            signal=signal,
            histogram=histogram,
            as_of=ordered[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        fast_window=fast,
        slow_window=slow,
        signal_window=signal_window,
        seed_start=ordered[0].timestamp,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        line=line,
        signal=signal,
        histogram=histogram,
        as_of=ordered[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )


__all__ = [
    "ATR_ALGORITHM_VERSION",
    "IncrementalAtrCheckpoint",
    "IncrementalMacdCheckpoint",
    "IncrementalRecursiveCheckpointError",
    "IncrementalRecursiveError",
    "IncrementalRecursivePrefixError",
    "IncrementalRecursiveScopeError",
    "MACD_ALGORITHM_VERSION",
    "RECURSIVE_SEED_POLICY",
    "RSI_ALGORITHM_VERSION",
    "full_atr",
    "full_macd",
    "full_rsi",
    "wilder_average_step",
    "resume_atr",
    "resume_macd",
    "resume_rsi",
    "seed_atr",
    "seed_macd",
    "seed_rsi",
    "true_range_from_previous_close",
    "validate_atr_checkpoint",
    "validate_macd_checkpoint",
    "validate_rsi_checkpoint",
]
