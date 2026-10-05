"""Canonical incremental EMA state for daily market bars.

Pure, storage-free contract for the DATA-CHASSIS stage 5 seed: one
demonstrable daily recurrence (EMA) with a canonical seed, an ordered
``close`` observation-ID prefix and a verifiable checkpoint. It never
reads storage or the filesystem, never writes, never uses a clock and
never imports the statistics engine, pipeline or service. Nothing in
production imports it yet.
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
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency

ALGORITHM_VERSION = "market-ema-incremental-v2-decimal34"
SEED_LENGTH_POLICY = "incremental-ema-seed-first-window-v1"
_CHECKPOINT_LABEL = "incremental-ema-checkpoint-v1"
_MAX_WINDOW = 400


class IncrementalEmaError(RuntimeError):
    """Base error for the canonical incremental EMA contract."""


class IncrementalEmaScopeError(IncrementalEmaError):
    """Raised when bars leave the declared asset, source, frequency or window."""


class IncrementalEmaPrefixError(IncrementalEmaError):
    """Raised when the visible ordered prefix is absent, truncated or revised."""


class IncrementalEmaCheckpointError(IncrementalEmaError):
    """Raised when a checkpoint digest, value or identity is corrupt."""


def canonical_prefix_hash(observation_ids: Sequence[UUID]) -> str:
    """Return SHA-256 of the canonical encoding of the ordered prefix."""
    encoded = json.dumps(
        [str(identifier) for identifier in observation_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def checkpoint_identity(
    *,
    asset_id: str,
    source_id: str,
    window: int,
    seed_start: datetime,
    value: Decimal,
    as_of: datetime,
    available_at: datetime,
    prefix_length: int,
    prefix_hash: str,
) -> UUID:
    """Return the deterministic identity of one verified EMA checkpoint."""
    return _identity(
        _CHECKPOINT_LABEL,
        {
            "algorithm_version": ALGORITHM_VERSION,
            "asset_id": asset_id,
            "as_of": as_of.astimezone(UTC).isoformat(),
            "available_at": available_at.astimezone(UTC).isoformat(),
            "prefix_hash": prefix_hash,
            "prefix_length": prefix_length,
            "seed_start": seed_start.astimezone(UTC).isoformat(),
            "source_id": source_id,
            "value": format(value, "f"),
            "window": window,
        },
    )


class IncrementalEmaCheckpoint(ContractModel):
    """Verifiable state of one daily EMA prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: UUID
    algorithm_version: Literal["market-ema-incremental-v2-decimal34"] = ALGORITHM_VERSION
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: Literal[DataFrequency.DAY_1] = DataFrequency.DAY_1
    window: int = Field(ge=2, le=_MAX_WINDOW)
    seed_start: UTCDateTime
    value: Decimal
    as_of: UTCDateTime
    available_at: UTCDateTime
    prefix_length: int = Field(ge=1)
    prefix_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_checkpoint(self) -> IncrementalEmaCheckpoint:
        """Require finite values and a deterministic checkpoint identity."""
        if not self.value.is_finite():
            raise ValueError("checkpoint value must be finite")
        if self.as_of < self.seed_start:
            raise ValueError("checkpoint as_of must not precede seed_start")
        if self.available_at < self.as_of:
            raise ValueError("checkpoint available_at must not precede as_of")
        if self.checkpoint_id != checkpoint_identity(
            asset_id=self.asset_id,
            source_id=self.source_id,
            window=self.window,
            seed_start=self.seed_start,
            value=self.value,
            as_of=self.as_of,
            available_at=self.available_at,
            prefix_length=self.prefix_length,
            prefix_hash=self.prefix_hash,
        ):
            raise ValueError("checkpoint identity is not deterministic")
        return self


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


def _validate_window(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("window must be an integer")
    if value < 2:
        raise ValueError("window must be at least 2")
    if value > _MAX_WINDOW:
        raise ValueError("window must not exceed 400")
    return value


def _ordered_scope_bars(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> tuple[MarketBar, ...]:
    if not bars:
        raise IncrementalEmaPrefixError("incremental EMA requires at least one bar")
    ordered = tuple(bars)
    for bar in ordered:
        if bar.asset_id != asset_id or bar.source_id != source_id:
            raise IncrementalEmaScopeError("bar asset or source does not match scope")
        if bar.frequency is not DataFrequency.DAY_1:
            raise IncrementalEmaScopeError("incremental EMA requires DAY_1 bars")
        if not bar.close.is_finite():
            raise IncrementalEmaScopeError("incremental EMA close must be finite")
    timestamps = [bar.timestamp for bar in ordered]
    if timestamps != sorted(timestamps):
        raise IncrementalEmaPrefixError("bars must be ordered by timestamp")
    if len(set(timestamps)) != len(timestamps):
        raise IncrementalEmaPrefixError("bars must not contain duplicate timestamps")
    if len(ordered) < window:
        raise IncrementalEmaPrefixError("visible prefix is shorter than the EMA window")
    return ordered


def _alpha(window: int) -> Decimal:
    with localcontext(Context(prec=34)):
        return Decimal("2") / Decimal(window + 1)


def decimal34_mean(values: Sequence[Decimal]) -> Decimal:
    """Return the exact arithmetic mean under the canonical Decimal34 context."""
    if not values:
        raise ValueError("mean requires at least one value")
    if any(not value.is_finite() for value in values):
        raise ValueError("mean values must be finite")
    with localcontext(Context(prec=34)):
        return sum(values, Decimal("0")) / Decimal(len(values))


def ema_step(previous: Decimal, current: Decimal, window: int) -> Decimal:
    """Apply one canonical Decimal34 EMA recurrence step."""
    window = _validate_window(window)
    if not previous.is_finite() or not current.is_finite():
        raise ValueError("EMA step values must be finite")
    with localcontext(Context(prec=34)):
        alpha = _alpha(window)
        return alpha * current + (Decimal("1") - alpha) * previous


def _prefix_ids(ordered: Sequence[MarketBar]) -> tuple[UUID, ...]:
    try:
        return tuple(bar.observation_ids["close"] for bar in ordered)
    except KeyError as error:
        raise IncrementalEmaPrefixError("bar is missing required close observation ID") from error


def seed_checkpoint(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> IncrementalEmaCheckpoint:
    """Seed the canonical EMA from the first window of the full PIT history."""
    window = _validate_window(window)
    ordered = _ordered_scope_bars(bars, asset_id=asset_id, source_id=source_id, window=window)
    seed_bars = ordered[:window]
    value = decimal34_mean(tuple(bar.close for bar in seed_bars))
    prefix_ids = _prefix_ids(seed_bars)
    prefix_hash = canonical_prefix_hash(prefix_ids)
    available_at = max(bar.available_at for bar in seed_bars)
    return IncrementalEmaCheckpoint(
        checkpoint_id=checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            window=window,
            seed_start=ordered[0].timestamp,
            value=value,
            as_of=seed_bars[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=prefix_hash,
        ),
        asset_id=asset_id,
        source_id=source_id,
        window=window,
        seed_start=ordered[0].timestamp,
        value=value,
        as_of=seed_bars[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=prefix_hash,
    )


def _verify_prefix(
    ordered: Sequence[MarketBar],
    checkpoint: IncrementalEmaCheckpoint,
) -> tuple[MarketBar, ...]:
    visible = ordered[: checkpoint.prefix_length]
    if len(visible) != checkpoint.prefix_length:
        raise IncrementalEmaPrefixError("checkpoint prefix is not fully visible")
    if canonical_prefix_hash(_prefix_ids(visible)) != checkpoint.prefix_hash:
        raise IncrementalEmaPrefixError("checkpoint prefix was revised or truncated")
    if visible[0].timestamp != checkpoint.seed_start:
        raise IncrementalEmaPrefixError("checkpoint seed does not match visible history")
    if visible[-1].timestamp != checkpoint.as_of:
        raise IncrementalEmaPrefixError("checkpoint head does not match visible history")
    return visible


def validate_checkpoint(
    checkpoint: IncrementalEmaCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> tuple[MarketBar, ...]:
    """Verify a checkpoint against the full visible prefix at a cut."""
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise IncrementalEmaCheckpointError("known_at must be timezone-aware")
    window = _validate_window(checkpoint.window)
    ordered = _ordered_scope_bars(
        bars, asset_id=checkpoint.asset_id, source_id=checkpoint.source_id, window=window
    )
    if checkpoint.available_at > known_at:
        raise IncrementalEmaCheckpointError("checkpoint is not available at known_at")
    for bar in ordered:
        if bar.available_at > known_at:
            raise IncrementalEmaPrefixError("visible prefix contains future evidence")
    _verify_prefix(ordered, checkpoint)
    return ordered


def resume(
    checkpoint: IncrementalEmaCheckpoint,
    bars: Sequence[MarketBar],
    *,
    known_at: datetime,
) -> IncrementalEmaCheckpoint:
    """Continue a verified checkpoint over successive available daily bars."""
    ordered = validate_checkpoint(checkpoint, bars, known_at=known_at)
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    value = checkpoint.value
    available_at = checkpoint.available_at
    for current in tail:
        value = ema_step(value, current.close, checkpoint.window)
        available_at = max(current.available_at, available_at)
    prefix_ids = (*_prefix_ids(ordered[: checkpoint.prefix_length]), *_prefix_ids(tail))
    extended = ordered[: checkpoint.prefix_length] + tail
    prefix_hash = canonical_prefix_hash(prefix_ids)
    return IncrementalEmaCheckpoint(
        checkpoint_id=checkpoint_identity(
            asset_id=checkpoint.asset_id,
            source_id=checkpoint.source_id,
            window=checkpoint.window,
            seed_start=checkpoint.seed_start,
            value=value,
            as_of=extended[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=prefix_hash,
        ),
        asset_id=checkpoint.asset_id,
        source_id=checkpoint.source_id,
        window=checkpoint.window,
        seed_start=checkpoint.seed_start,
        value=value,
        as_of=extended[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=prefix_hash,
    )


def full_checkpoint(
    bars: Sequence[MarketBar],
    *,
    asset_id: str,
    source_id: str,
    window: int,
) -> IncrementalEmaCheckpoint:
    """Compute the canonical EMA over the full visible prefix in one pass."""
    checkpoint = seed_checkpoint(bars, asset_id=asset_id, source_id=source_id, window=window)
    ordered = _ordered_scope_bars(bars, asset_id=asset_id, source_id=source_id, window=window)
    tail = ordered[checkpoint.prefix_length :]
    if not tail:
        return checkpoint
    value = checkpoint.value
    available_at = checkpoint.available_at
    for current in tail:
        value = ema_step(value, current.close, window)
        available_at = max(current.available_at, available_at)
    prefix_ids = _prefix_ids(ordered)
    return IncrementalEmaCheckpoint(
        checkpoint_id=checkpoint_identity(
            asset_id=asset_id,
            source_id=source_id,
            window=window,
            seed_start=ordered[0].timestamp,
            value=value,
            as_of=ordered[-1].timestamp,
            available_at=available_at,
            prefix_length=len(prefix_ids),
            prefix_hash=canonical_prefix_hash(prefix_ids),
        ),
        asset_id=asset_id,
        source_id=source_id,
        window=window,
        seed_start=ordered[0].timestamp,
        value=value,
        as_of=ordered[-1].timestamp,
        available_at=available_at,
        prefix_length=len(prefix_ids),
        prefix_hash=canonical_prefix_hash(prefix_ids),
    )
