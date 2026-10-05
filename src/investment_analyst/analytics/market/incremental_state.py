"""Bounded per-bar Decimal34 transitions for persistent market checkpoints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.daily_evidence import (
    DAILY_EVIDENCE_POLICY,
    DailyEvidenceError,
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
)
from investment_analyst.analytics.market.incremental_ema import (
    ALGORITHM_VERSION as EMA_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.incremental_ema import (
    ema_step,
)
from investment_analyst.analytics.market.incremental_recursive import (
    ATR_ALGORITHM_VERSION,
    MACD_ALGORITHM_VERSION,
    RSI_ALGORITHM_VERSION,
    _rsi_from_averages,
    true_range_from_previous_close,
    wilder_average_step,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency, DataQuality

MARKET_CHECKPOINT_POLICY = "market-recursive-checkpoint-v1"
_CHECKPOINT_ID_LABEL = "market-recursive-checkpoint-id-v1"
_ALGORITHM_BY_FAMILY = {
    "ema": EMA_ALGORITHM_VERSION,
    "rsi": RSI_ALGORITHM_VERSION,
    "atr": ATR_ALGORITHM_VERSION,
    "macd": MACD_ALGORITHM_VERSION,
}


class IncrementalStateError(ValueError):
    """Raised when a persisted recurrence state cannot be resumed safely."""


class EmaParameters(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["ema"] = "ema"
    window: int = Field(ge=2, le=400)


class RsiParameters(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["rsi"] = "rsi"
    window: int = Field(ge=2, le=400)


class AtrParameters(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["atr"] = "atr"
    window: int = Field(ge=2, le=400)


class MacdParameters(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["macd"] = "macd"
    fast_window: int = Field(ge=2, le=400)
    slow_window: int = Field(ge=2, le=400)
    signal_window: int = Field(ge=2, le=400)

    @model_validator(mode="after")
    def validate_window_order(self) -> MacdParameters:
        if self.fast_window >= self.slow_window:
            raise ValueError("MACD fast window must be less than slow window")
        return self


type RecursiveParameters = Annotated[
    EmaParameters | RsiParameters | AtrParameters | MacdParameters,
    Field(discriminator="family"),
]


class EmaState(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["ema"] = "ema"
    bars_seen: int = Field(ge=1)
    seed_count: int = Field(ge=1, le=400)
    seed_total: Decimal
    value: Decimal | None = None
    quality: DataQuality


class RsiState(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["rsi"] = "rsi"
    bars_seen: int = Field(ge=1)
    change_count: int = Field(ge=0)
    previous_close: Decimal
    seed_gain_total: Decimal
    seed_loss_total: Decimal
    average_gain: Decimal | None = None
    average_loss: Decimal | None = None
    value: Decimal | None = None
    quality: DataQuality


class AtrState(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["atr"] = "atr"
    bars_seen: int = Field(ge=1)
    seed_count: int = Field(ge=1, le=400)
    seed_true_range_total: Decimal
    true_range: Decimal
    previous_close: Decimal
    value: Decimal | None = None
    quality: DataQuality


class MacdState(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["macd"] = "macd"
    bars_seen: int = Field(ge=1)
    previous_close: Decimal
    fast_seed_count: int = Field(ge=1, le=400)
    fast_seed_total: Decimal
    fast_ema: Decimal | None = None
    slow_seed_count: int = Field(ge=1, le=400)
    slow_seed_total: Decimal
    slow_ema: Decimal | None = None
    line_count: int = Field(ge=0)
    signal_seed_total: Decimal
    line: Decimal | None = None
    signal: Decimal | None = None
    histogram: Decimal | None = None
    quality: DataQuality


type RecursiveState = Annotated[
    EmaState | RsiState | AtrState | MacdState,
    Field(discriminator="family"),
]


class CheckpointMetricReference(ContractModel):
    """A durable metric result optionally referenced by its same-bar checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    result_id: UUID


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("checkpoint Decimal must be finite")
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("checkpoint timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"unsupported checkpoint identity value: {type(value).__name__}")


def _checkpoint_preimage(
    *,
    asset_id: str,
    source_id: str,
    algorithm_version: str,
    parameters: RecursiveParameters,
    seed_start: datetime,
    as_of: datetime,
    available_at: datetime,
    daily_prefix_id: UUID,
    daily_prefix_hash: str,
    daily_prefix_length: int,
    state: RecursiveState,
) -> dict[str, object]:
    return {
        "algorithm_version": algorithm_version,
        "as_of": as_of,
        "asset_id": asset_id,
        "available_at": available_at,
        "daily_prefix_hash": daily_prefix_hash,
        "daily_prefix_id": daily_prefix_id,
        "daily_prefix_length": daily_prefix_length,
        "family": parameters.family,
        "parameters": parameters.model_dump(mode="python"),
        "policy_version": MARKET_CHECKPOINT_POLICY,
        "seed_start": seed_start,
        "source_id": source_id,
        "state": state.model_dump(mode="python"),
    }


def _identity(document: dict[str, object]) -> UUID:
    digest = hashlib.sha256(_identity_bytes(document)).digest()
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _identity_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(
        {"label": _CHECKPOINT_ID_LABEL, **document},
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class MarketRecursiveCheckpoint(ContractModel):
    """Strict content-addressed Decimal34 state at one daily evidence prefix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: UUID
    policy_version: Literal["market-recursive-checkpoint-v1"] = MARKET_CHECKPOINT_POLICY
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    frequency: Literal[DataFrequency.DAY_1] = DataFrequency.DAY_1
    algorithm_version: NonEmptyStr
    parameters: RecursiveParameters
    seed_start: UTCDateTime
    as_of: UTCDateTime
    available_at: UTCDateTime
    daily_prefix_id: UUID
    daily_prefix_hash: NonEmptyStr
    daily_prefix_length: int = Field(ge=1)
    state: RecursiveState
    metric_references: tuple[CheckpointMetricReference, ...] = ()

    @model_validator(mode="after")
    def validate_checkpoint(self) -> MarketRecursiveCheckpoint:
        family = self.parameters.family
        if self.state.family != family:
            raise ValueError("checkpoint state family does not match its parameters")
        if self.algorithm_version != _ALGORITHM_BY_FAMILY[family]:
            raise ValueError("checkpoint algorithm version is not authorized for its family")
        if self.as_of < self.seed_start:
            raise ValueError("checkpoint as_of must not precede seed_start")
        if self.available_at < self.as_of:
            raise ValueError("checkpoint available_at must not precede as_of")
        if self.state.bars_seen != self.daily_prefix_length:
            raise ValueError("checkpoint bars_seen must match its daily prefix length")
        if len(self.daily_prefix_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.daily_prefix_hash
        ):
            raise ValueError("checkpoint prefix hash must be lowercase SHA-256")
        _validate_state(self.parameters, self.state)
        if len({item.metric_key for item in self.metric_references}) != len(self.metric_references):
            raise ValueError("checkpoint metric references must have unique metric keys")
        if len({item.result_id for item in self.metric_references}) != len(self.metric_references):
            raise ValueError("checkpoint metric references must have unique result IDs")
        identity = _identity(
            _checkpoint_preimage(
                asset_id=self.asset_id,
                source_id=self.source_id,
                algorithm_version=self.algorithm_version,
                parameters=self.parameters,
                seed_start=self.seed_start,
                as_of=self.as_of,
                available_at=self.available_at,
                daily_prefix_id=self.daily_prefix_id,
                daily_prefix_hash=self.daily_prefix_hash,
                daily_prefix_length=self.daily_prefix_length,
                state=self.state,
            )
        )
        if self.checkpoint_id != identity:
            raise ValueError("checkpoint identity does not match its semantic state")
        return self

    @property
    def family(self) -> str:
        """Return the recurrence family selected by the discriminated parameters."""
        return self.parameters.family


def market_checkpoint_content_hash(checkpoint: MarketRecursiveCheckpoint) -> str:
    """Return the full SHA-256 content hash used to derive a checkpoint UUID."""
    preimage = _checkpoint_preimage(
        asset_id=checkpoint.asset_id,
        source_id=checkpoint.source_id,
        algorithm_version=checkpoint.algorithm_version,
        parameters=checkpoint.parameters,
        seed_start=checkpoint.seed_start,
        as_of=checkpoint.as_of,
        available_at=checkpoint.available_at,
        daily_prefix_id=checkpoint.daily_prefix_id,
        daily_prefix_hash=checkpoint.daily_prefix_hash,
        daily_prefix_length=checkpoint.daily_prefix_length,
        state=checkpoint.state,
    )
    return hashlib.sha256(_identity_bytes(preimage)).hexdigest()


def _validate_state(parameters: RecursiveParameters, state: RecursiveState) -> None:
    decimals = [
        value
        for name, value in state.model_dump(mode="python").items()
        if isinstance(value, Decimal)
    ]
    if any(not value.is_finite() for value in decimals):
        raise ValueError("checkpoint state Decimal values must be finite")
    if isinstance(parameters, EmaParameters) and isinstance(state, EmaState):
        if state.seed_count != min(state.bars_seen, parameters.window):
            raise ValueError("EMA seed count does not match bars and window")
        if (state.value is None) != (state.bars_seen < parameters.window):
            raise ValueError("EMA readiness does not match its warm-up")
    elif isinstance(parameters, RsiParameters) and isinstance(state, RsiState):
        if state.change_count != state.bars_seen - 1:
            raise ValueError("RSI change count does not match bars seen")
        ready = state.change_count >= parameters.window
        if ready != all(
            item is not None for item in (state.average_gain, state.average_loss, state.value)
        ):
            raise ValueError("RSI readiness does not match its warm-up")
    elif isinstance(parameters, AtrParameters) and isinstance(state, AtrState):
        if state.seed_count != min(state.bars_seen, parameters.window):
            raise ValueError("ATR seed count does not match bars and window")
        if (state.value is None) != (state.bars_seen < parameters.window):
            raise ValueError("ATR readiness does not match its warm-up")
    elif isinstance(parameters, MacdParameters) and isinstance(state, MacdState):
        if state.fast_seed_count != min(state.bars_seen, parameters.fast_window):
            raise ValueError("MACD fast seed count does not match its window")
        if state.slow_seed_count != min(state.bars_seen, parameters.slow_window):
            raise ValueError("MACD slow seed count does not match its window")
        expected_lines = max(0, state.bars_seen - parameters.slow_window + 1)
        if state.line_count != expected_lines:
            raise ValueError("MACD line count does not match bars and slow window")
        if (state.fast_ema is None) != (state.bars_seen < parameters.fast_window):
            raise ValueError("MACD fast EMA readiness does not match its warm-up")
        if (state.slow_ema is None) != (state.bars_seen < parameters.slow_window):
            raise ValueError("MACD slow EMA readiness does not match its warm-up")
        ready = state.line_count >= parameters.signal_window
        if ready != all(item is not None for item in (state.line, state.signal, state.histogram)):
            raise ValueError("MACD signal readiness does not match its warm-up")
    else:
        raise ValueError("checkpoint parameters and state types do not match")


def _checkpoint(
    *,
    prefix: DailyEvidencePrefix,
    parameters: RecursiveParameters,
    state: RecursiveState,
    seed_start: datetime,
    metric_references: tuple[CheckpointMetricReference, ...] = (),
) -> MarketRecursiveCheckpoint:
    algorithm_version = _ALGORITHM_BY_FAMILY[parameters.family]
    preimage = _checkpoint_preimage(
        asset_id=prefix.asset_id,
        source_id=prefix.source_id,
        algorithm_version=algorithm_version,
        parameters=parameters,
        seed_start=seed_start,
        as_of=prefix.timestamp,
        available_at=prefix.available_at,
        daily_prefix_id=prefix.prefix_id,
        daily_prefix_hash=prefix.prefix_hash,
        daily_prefix_length=prefix.length,
        state=state,
    )
    return MarketRecursiveCheckpoint(
        checkpoint_id=_identity(preimage),
        asset_id=prefix.asset_id,
        source_id=prefix.source_id,
        algorithm_version=algorithm_version,
        parameters=parameters,
        seed_start=seed_start,
        as_of=prefix.timestamp,
        available_at=prefix.available_at,
        daily_prefix_id=prefix.prefix_id,
        daily_prefix_hash=prefix.prefix_hash,
        daily_prefix_length=prefix.length,
        state=state,
        metric_references=metric_references,
    )


def advance_checkpoint(
    previous: MarketRecursiveCheckpoint | None,
    bar: MarketBar,
    prefix: DailyEvidencePrefix,
    parameters: RecursiveParameters,
    *,
    metric_references: tuple[CheckpointMetricReference, ...] = (),
) -> MarketRecursiveCheckpoint:
    """Advance one recurrence over one selected daily bar and prefix node."""
    _validate_bar_prefix(bar, prefix, parameters)
    if previous is None:
        if prefix.parent_prefix_id is not None or prefix.length != 1:
            raise IncrementalStateError("a seed checkpoint must start at a daily prefix root")
        seed_start = bar.timestamp
        prior_quality = bar.quality
        prior: RecursiveState | None = None
    else:
        if previous.parameters != parameters:
            raise IncrementalStateError("checkpoint parameters changed during continuation")
        if (
            previous.asset_id != bar.asset_id
            or previous.source_id != bar.source_id
            or previous.daily_prefix_id != prefix.parent_prefix_id
            or previous.daily_prefix_hash != prefix.parent_hash
            or previous.daily_prefix_length + 1 != prefix.length
            or previous.as_of >= bar.timestamp
        ):
            raise IncrementalStateError("checkpoint does not match the daily evidence parent")
        seed_start = previous.seed_start
        prior_quality = _combine_quality(previous.state.quality, bar.quality)
        prior = previous.state

    with localcontext(Context(prec=34)):
        if isinstance(parameters, EmaParameters):
            old = prior if isinstance(prior, EmaState) else None
            seed_count = old.seed_count if old is not None else 0
            seed_total = old.seed_total if old is not None else Decimal("0")
            value = old.value if old is not None else None
            if value is None:
                seed_count += 1
                seed_total += bar.close
                if seed_count == parameters.window:
                    value = seed_total / Decimal(parameters.window)
            else:
                value = ema_step(value, bar.close, parameters.window)
            state: RecursiveState = EmaState(
                bars_seen=bar_count(previous),
                seed_count=seed_count,
                seed_total=seed_total,
                value=value,
                quality=prior_quality,
            )
        elif isinstance(parameters, RsiParameters):
            old = prior if isinstance(prior, RsiState) else None
            change_count = old.change_count if old is not None else 0
            previous_close = old.previous_close if old is not None else None
            seed_gain = old.seed_gain_total if old is not None else Decimal("0")
            seed_loss = old.seed_loss_total if old is not None else Decimal("0")
            average_gain = old.average_gain if old is not None else None
            average_loss = old.average_loss if old is not None else None
            value = old.value if old is not None else None
            if previous_close is not None:
                change = bar.close - previous_close
                gain = max(change, Decimal("0"))
                loss = max(-change, Decimal("0"))
                change_count += 1
                if average_gain is None or average_loss is None:
                    seed_gain += gain
                    seed_loss += loss
                    if change_count == parameters.window:
                        average_gain = seed_gain / Decimal(parameters.window)
                        average_loss = seed_loss / Decimal(parameters.window)
                else:
                    average_gain = wilder_average_step(average_gain, gain, parameters.window)
                    average_loss = wilder_average_step(average_loss, loss, parameters.window)
                if average_gain is not None and average_loss is not None:
                    value = _rsi_from_averages(average_gain, average_loss)
            state = RsiState(
                bars_seen=bar_count(previous),
                change_count=change_count,
                previous_close=bar.close,
                seed_gain_total=seed_gain,
                seed_loss_total=seed_loss,
                average_gain=average_gain,
                average_loss=average_loss,
                value=value,
                quality=prior_quality,
            )
        elif isinstance(parameters, AtrParameters):
            old = prior if isinstance(prior, AtrState) else None
            seed_count = old.seed_count if old is not None else 0
            seed_total = old.seed_true_range_total if old is not None else Decimal("0")
            previous_close = old.previous_close if old is not None else None
            value = old.value if old is not None else None
            true_range = true_range_from_previous_close(bar, previous_close)
            if value is None:
                seed_count += 1
                seed_total += true_range
                if seed_count == parameters.window:
                    value = seed_total / Decimal(parameters.window)
            else:
                value = wilder_average_step(value, true_range, parameters.window)
            state = AtrState(
                bars_seen=bar_count(previous),
                seed_count=seed_count,
                seed_true_range_total=seed_total,
                true_range=true_range,
                previous_close=bar.close,
                value=value,
                quality=prior_quality,
            )
        else:
            old = prior if isinstance(prior, MacdState) else None
            fast_count = old.fast_seed_count if old is not None else 0
            fast_total = old.fast_seed_total if old is not None else Decimal("0")
            fast_ema = old.fast_ema if old is not None else None
            slow_count = old.slow_seed_count if old is not None else 0
            slow_total = old.slow_seed_total if old is not None else Decimal("0")
            slow_ema = old.slow_ema if old is not None else None
            line_count = old.line_count if old is not None else 0
            signal_total = old.signal_seed_total if old is not None else Decimal("0")
            signal = old.signal if old is not None else None
            if fast_ema is None:
                fast_count += 1
                fast_total += bar.close
                if fast_count == parameters.fast_window:
                    fast_ema = fast_total / Decimal(parameters.fast_window)
            else:
                fast_ema = ema_step(fast_ema, bar.close, parameters.fast_window)
            if slow_ema is None:
                slow_count += 1
                slow_total += bar.close
                if slow_count == parameters.slow_window:
                    slow_ema = slow_total / Decimal(parameters.slow_window)
            else:
                slow_ema = ema_step(slow_ema, bar.close, parameters.slow_window)
            line = fast_ema - slow_ema if fast_ema is not None and slow_ema is not None else None
            histogram: Decimal | None = None
            if line is not None:
                line_count += 1
                if signal is None:
                    signal_total += line
                    if line_count == parameters.signal_window:
                        signal = signal_total / Decimal(parameters.signal_window)
                else:
                    signal = ema_step(signal, line, parameters.signal_window)
                if signal is not None:
                    histogram = line - signal
            state = MacdState(
                bars_seen=bar_count(previous),
                previous_close=bar.close,
                fast_seed_count=fast_count,
                fast_seed_total=fast_total,
                fast_ema=fast_ema,
                slow_seed_count=slow_count,
                slow_seed_total=slow_total,
                slow_ema=slow_ema,
                line_count=line_count,
                signal_seed_total=signal_total,
                line=line,
                signal=signal,
                histogram=histogram,
                quality=prior_quality,
            )
    return _checkpoint(
        prefix=prefix,
        parameters=parameters,
        state=state,
        seed_start=seed_start,
        metric_references=metric_references,
    )


def _validate_bar_prefix(
    bar: MarketBar,
    prefix: DailyEvidencePrefix,
    parameters: RecursiveParameters,
) -> None:
    expected_group = (
        DailyEvidenceFieldGroup.HIGH_LOW_CLOSE
        if isinstance(parameters, AtrParameters)
        else DailyEvidenceFieldGroup.CLOSE
    )
    expected_fields = (
        ("high", "low", "close") if isinstance(parameters, AtrParameters) else ("close",)
    )
    if (
        bar.asset_id != prefix.asset_id
        or bar.source_id != prefix.source_id
        or bar.frequency is not DataFrequency.DAY_1
        or bar.timestamp != prefix.timestamp
        or prefix.field_group is not expected_group
        or tuple(bar.observation_ids[field] for field in expected_fields) != prefix.observation_ids
        or bar.available_at > prefix.available_at
    ):
        raise IncrementalStateError("daily bar and evidence prefix do not match")
    if prefix.policy_version != DAILY_EVIDENCE_POLICY:
        raise DailyEvidenceError("daily evidence policy is not supported")


def bar_count(previous: MarketRecursiveCheckpoint | None) -> int:
    """Return the next persisted prefix length."""
    return previous.daily_prefix_length + 1 if previous is not None else 1


def _combine_quality(left: DataQuality, right: DataQuality) -> DataQuality:
    for candidate in (
        DataQuality.SUSPECT,
        DataQuality.PARTIAL,
        DataQuality.DELAYED,
        DataQuality.VALID,
    ):
        if candidate in (left, right):
            return candidate
    raise IncrementalStateError("checkpoint quality is unknown")


def checkpoint_ready(checkpoint: MarketRecursiveCheckpoint) -> bool:
    """Return whether the family's canonical warm-up has completed."""
    if isinstance(checkpoint.state, MacdState):
        return checkpoint.state.histogram is not None
    return checkpoint.state.value is not None


__all__ = [
    "ATR_ALGORITHM_VERSION",
    "MACD_ALGORITHM_VERSION",
    "RSI_ALGORITHM_VERSION",
    "AtrParameters",
    "AtrState",
    "CheckpointMetricReference",
    "EmaParameters",
    "EmaState",
    "IncrementalStateError",
    "MacdParameters",
    "MacdState",
    "MARKET_CHECKPOINT_POLICY",
    "MarketRecursiveCheckpoint",
    "RecursiveParameters",
    "RecursiveState",
    "RsiParameters",
    "RsiState",
    "advance_checkpoint",
    "checkpoint_ready",
    "market_checkpoint_content_hash",
]
