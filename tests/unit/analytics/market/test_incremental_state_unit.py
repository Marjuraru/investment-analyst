"""Tests for bounded persistent EMA/RSI/ATR/MACD state transitions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
    make_daily_evidence_prefix,
    observation_rows_digest,
)
from investment_analyst.analytics.market.incremental_ema import full_checkpoint
from investment_analyst.analytics.market.incremental_recursive import (
    full_atr,
    full_macd,
    full_rsi,
)
from investment_analyst.analytics.market.incremental_state import (
    AtrParameters,
    AtrState,
    EmaParameters,
    EmaState,
    IncrementalStateError,
    MacdParameters,
    MacdState,
    MarketRecursiveCheckpoint,
    RecursiveParameters,
    RsiParameters,
    RsiState,
    advance_checkpoint,
)
from investment_analyst.core.models.enums import DataFrequency, DataQuality

ASSET_ID = "equity:us:test"
SOURCE_ID = "simulated:daily-bars"


def _bars(count: int) -> tuple[MarketBar, ...]:
    output: list[MarketBar] = []
    for index in range(count):
        timestamp = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index + index // 7)
        close = Decimal("100") + Decimal(index) / Decimal("10")
        if index % 5 == 3:
            close -= Decimal("1.7")
        values = {
            "open": close,
            "high": close + Decimal("2.5"),
            "low": close - Decimal("1.25"),
            "close": close,
            "volume": Decimal("1000") + Decimal(index),
        }
        observation_ids = {
            name: uuid5(NAMESPACE_URL, f"{ASSET_ID}:{SOURCE_ID}:{index}:{name}") for name in values
        }
        output.append(
            MarketBar(
                asset_id=ASSET_ID,
                source_id=SOURCE_ID,
                raw_record_id=uuid5(NAMESPACE_URL, f"raw:{index}"),
                frequency=DataFrequency.DAY_1,
                timestamp=timestamp,
                available_at=timestamp + timedelta(hours=1),
                open=values["open"],
                high=values["high"],
                low=values["low"],
                close=values["close"],
                volume=values["volume"],
                quality=DataQuality.VALID,
                observation_ids=observation_ids,
            )
        )
    return tuple(output)


def _prefixes(
    bars: tuple[MarketBar, ...], field_group: DailyEvidenceFieldGroup
) -> tuple[DailyEvidencePrefix, ...]:
    fields = (
        ("close",)
        if field_group is DailyEvidenceFieldGroup.CLOSE
        else (
            "high",
            "low",
            "close",
        )
    )
    output: list[DailyEvidencePrefix] = []
    parent = None
    for bar in bars:
        ids = tuple(bar.observation_ids[field] for field in fields)
        rows = [
            [
                field,
                str(identifier),
                str({"close": bar.close, "high": bar.high, "low": bar.low}[field]),
                bar.timestamp.isoformat(),
            ]
            for field, identifier in zip(fields, ids, strict=True)
        ]
        parent = make_daily_evidence_prefix(
            asset_id=bar.asset_id,
            source_id=bar.source_id,
            field_group=field_group,
            timestamp=bar.timestamp,
            observation_ids=ids,
            observation_digest=observation_rows_digest(rows),
            current_available_at=bar.available_at,
            quality=bar.quality,
            parent=parent,
        )
        output.append(parent)
    return tuple(output)


def _run(
    bars: tuple[MarketBar, ...],
    prefixes: tuple[DailyEvidencePrefix, ...],
    parameters: RecursiveParameters,
) -> tuple[MarketRecursiveCheckpoint, ...]:
    checkpoints: list[MarketRecursiveCheckpoint] = []
    previous: MarketRecursiveCheckpoint | None = None
    for bar, prefix in zip(bars, prefixes, strict=True):
        previous = advance_checkpoint(previous, bar, prefix, parameters)
        checkpoints.append(previous)
    return tuple(checkpoints)


def test_all_family_transitions_match_existing_decimal34_oracles() -> None:
    bars = _bars(80)
    close_prefixes = _prefixes(bars, DailyEvidenceFieldGroup.CLOSE)
    hloc_prefixes = _prefixes(bars, DailyEvidenceFieldGroup.HIGH_LOW_CLOSE)

    ema = _run(bars, close_prefixes, EmaParameters(window=20))[-1]
    assert isinstance(ema.state, EmaState)
    assert (
        ema.state.value
        == full_checkpoint(bars, asset_id=ASSET_ID, source_id=SOURCE_ID, window=20).value
    )

    rsi = _run(bars, close_prefixes, RsiParameters(window=14))[-1]
    assert isinstance(rsi.state, RsiState)
    assert rsi.state.value == full_rsi(bars, asset_id=ASSET_ID, source_id=SOURCE_ID, window=14).rsi

    atr = _run(bars, hloc_prefixes, AtrParameters(window=14))[-1]
    assert isinstance(atr.state, AtrState)
    assert atr.state.value == full_atr(bars, asset_id=ASSET_ID, source_id=SOURCE_ID, window=14).atr

    macd = _run(
        bars,
        close_prefixes,
        MacdParameters(fast_window=12, slow_window=26, signal_window=9),
    )[-1]
    assert isinstance(macd.state, MacdState)
    expected_macd = full_macd(
        bars,
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        fast_window=12,
        slow_window=26,
        signal_window=9,
    )
    assert (macd.state.fast_ema, macd.state.slow_ema) == (
        expected_macd.fast_ema,
        expected_macd.slow_ema,
    )
    assert (macd.state.line, macd.state.signal, macd.state.histogram) == (
        expected_macd.line,
        expected_macd.signal,
        expected_macd.histogram,
    )


@pytest.mark.parametrize(
    ("parameters", "field_group"),
    [
        (EmaParameters(window=8), DailyEvidenceFieldGroup.CLOSE),
        (RsiParameters(window=8), DailyEvidenceFieldGroup.CLOSE),
        (AtrParameters(window=8), DailyEvidenceFieldGroup.HIGH_LOW_CLOSE),
        (
            MacdParameters(fast_window=5, slow_window=10, signal_window=4),
            DailyEvidenceFieldGroup.CLOSE,
        ),
    ],
)
def test_split_resume_produces_the_same_checkpoint_identity(parameters, field_group) -> None:
    bars = _bars(53)
    prefixes = _prefixes(bars, field_group)
    complete = _run(bars, prefixes, parameters)

    split = 17
    resumed = _run(bars[:split], prefixes[:split], parameters)[-1]
    for bar, prefix in zip(bars[split:], prefixes[split:], strict=True):
        resumed = advance_checkpoint(resumed, bar, prefix, parameters)
    assert resumed.checkpoint_id == complete[-1].checkpoint_id
    assert resumed.state == complete[-1].state


def test_checkpoint_rejects_a_wrong_parent_or_changed_semantic_parameters() -> None:
    bars = _bars(12)
    prefixes = _prefixes(bars, DailyEvidenceFieldGroup.CLOSE)
    prior = _run(bars[:5], prefixes[:5], EmaParameters(window=4))[-1]

    with pytest.raises(IncrementalStateError, match="parameters changed"):
        advance_checkpoint(prior, bars[5], prefixes[5], EmaParameters(window=5))

    wrong_root = make_daily_evidence_prefix(
        asset_id=bars[5].asset_id,
        source_id=bars[5].source_id,
        field_group=DailyEvidenceFieldGroup.CLOSE,
        timestamp=bars[5].timestamp,
        observation_ids=(bars[5].observation_ids["close"],),
        observation_digest=observation_rows_digest(
            [[str(bars[5].observation_ids["close"]), str(bars[5].close)]]
        ),
        current_available_at=bars[5].available_at,
        quality=bars[5].quality,
    )
    with pytest.raises(IncrementalStateError, match="parent"):
        advance_checkpoint(prior, bars[5], wrong_root, EmaParameters(window=4))
