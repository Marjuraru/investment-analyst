"""Tests for the canonical incremental daily EMA contract."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery, MarketBar
from investment_analyst.analytics.market.incremental_ema import (
    ALGORITHM_VERSION,
    IncrementalEmaCheckpoint,
    IncrementalEmaCheckpointError,
    IncrementalEmaPrefixError,
    IncrementalEmaScopeError,
    canonical_prefix_hash,
    checkpoint_identity,
    full_checkpoint,
    resume,
    seed_checkpoint,
    validate_checkpoint,
)
from investment_analyst.core.models import DataFrequency, DataQuality

_ASSET = "crypto:btc-usd"
_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_START = datetime(2026, 1, 1, tzinfo=UTC)
_KNOWN = datetime(2026, 3, 1, tzinfo=UTC)


def _bar(
    index: int,
    close: str,
    *,
    asset_id: str = _ASSET,
    source_id: str = _SOURCE,
    start: datetime = _START,
    close_id=None,
    available_hour: int = 1,
) -> MarketBar:
    return MarketBar(
        asset_id=asset_id,
        source_id=source_id,
        raw_record_id=uuid4(),
        frequency=DataFrequency.DAY_1,
        timestamp=start + timedelta(days=index),
        available_at=start + timedelta(days=index, hours=available_hour),
        open=Decimal(close),
        high=Decimal(close) + Decimal("1"),
        low=Decimal(close) - Decimal("0.5"),
        close=Decimal(close),
        volume=Decimal("10"),
        quality=DataQuality.VALID,
        observation_ids={
            "open": uuid4(),
            "high": uuid4(),
            "low": uuid4(),
            "close": close_id or uuid4(),
            "volume": uuid4(),
        },
    )


def _bars(count: int, **kwargs) -> tuple[MarketBar, ...]:
    return tuple(_bar(index, str(100 + index), **kwargs) for index in range(count))


def test_canonical_seed_and_checkpoint_identity() -> None:
    bars = _bars(6)
    seed = seed_checkpoint(bars, asset_id=_ASSET, source_id=_SOURCE, window=3)

    assert seed.asset_id == _ASSET
    assert seed.algorithm_version == ALGORITHM_VERSION
    assert seed.seed_start == _START
    assert seed.as_of == _START + timedelta(days=2)
    assert seed.prefix_length == 3
    assert seed.value == Decimal("101")
    assert seed.prefix_hash == canonical_prefix_hash(
        tuple(bar.observation_ids["close"] for bar in bars[:3])
    )
    assert seed.checkpoint_id == checkpoint_identity(
        asset_id=_ASSET,
        source_id=_SOURCE,
        window=3,
        seed_start=seed.seed_start,
        value=seed.value,
        as_of=seed.as_of,
        available_at=seed.available_at,
        prefix_length=seed.prefix_length,
        prefix_hash=seed.prefix_hash,
    )
    assert seed.model_dump_json() == seed.model_copy().model_dump_json()

    shifted_query_start = _START + timedelta(days=2)
    assert seed.seed_start == _START
    assert shifted_query_start != seed.seed_start

    with pytest.raises(IncrementalEmaPrefixError, match="at least one bar"):
        seed_checkpoint((), asset_id=_ASSET, source_id=_SOURCE, window=3)
    with pytest.raises(ValueError, match="at least 2"):
        seed_checkpoint(bars, asset_id=_ASSET, source_id=_SOURCE, window=1)
    with pytest.raises(ValueError, match="not exceed 400"):
        seed_checkpoint(bars, asset_id=_ASSET, source_id=_SOURCE, window=401)
    with pytest.raises(IncrementalEmaScopeError, match="asset or source"):
        seed_checkpoint(bars, asset_id="equity:us:aapl", source_id=_SOURCE, window=3)
    with pytest.raises(IncrementalEmaScopeError, match="DAY_1"):
        seed_checkpoint(
            tuple(bar.model_copy(update={"frequency": DataFrequency.HOUR_1}) for bar in bars),
            asset_id=_ASSET,
            source_id=_SOURCE,
            window=3,
        )


def test_full_equals_resumed_decimal_across_daily_series() -> None:
    full_bars = _bars(10)
    window = 4
    full = full_checkpoint(full_bars, asset_id=_ASSET, source_id=_SOURCE, window=window)

    for cut in (window, window + 2, len(full_bars) - 1, len(full_bars)):
        seed = seed_checkpoint(full_bars[:cut], asset_id=_ASSET, source_id=_SOURCE, window=window)
        resumed = resume(seed, full_bars, known_at=_KNOWN)
        assert resumed.value == full.value
        assert resumed.checkpoint_id == full.checkpoint_id
        assert resumed.prefix_hash == full.prefix_hash
        assert resumed.prefix_length == len(full_bars)

    gapped = tuple(_bar(index * 2, str(100 + index)) for index in range(6))
    gapped_full = full_checkpoint(gapped, asset_id=_ASSET, source_id=_SOURCE, window=3)
    gapped_resumed = resume(
        seed_checkpoint(gapped[:3], asset_id=_ASSET, source_id=_SOURCE, window=3),
        gapped,
        known_at=_KNOWN,
    )
    assert gapped_resumed.value == gapped_full.value

    eth_bars = _bars(
        8, asset_id="crypto:eth-usd", source_id="coinbase-exchange:eth-usd:daily-candles"
    )
    eth_full = full_checkpoint(
        eth_bars,
        asset_id="crypto:eth-usd",
        source_id="coinbase-exchange:eth-usd:daily-candles",
        window=3,
    )
    eth_resumed = resume(
        seed_checkpoint(
            eth_bars[:3],
            asset_id="crypto:eth-usd",
            source_id="coinbase-exchange:eth-usd:daily-candles",
            window=3,
        ),
        eth_bars,
        known_at=_KNOWN,
    )
    assert eth_resumed.value == eth_full.value
    assert eth_full.checkpoint_id != full.checkpoint_id


def test_revisions_corruption_and_future_evidence_fail_closed() -> None:
    bars = _bars(8)
    window = 3
    seed = seed_checkpoint(bars, asset_id=_ASSET, source_id=_SOURCE, window=window)

    revised = list(bars)
    revised_close_id = uuid4()
    revised[1] = _bar(1, "999", close_id=revised_close_id)
    assert revised_close_id != bars[1].observation_ids["close"]
    with pytest.raises(IncrementalEmaPrefixError, match="revised or truncated"):
        resume(seed, tuple(revised), known_at=_KNOWN)

    truncated = bars[: seed.prefix_length - 1] + bars[seed.prefix_length :]
    with pytest.raises((IncrementalEmaPrefixError, IncrementalEmaScopeError), match="."):
        resume(seed, truncated, known_at=_KNOWN)

    tampered = seed.model_copy(update={"value": seed.value + Decimal("1")})
    with pytest.raises(ValueError, match="deterministic"):
        IncrementalEmaCheckpoint.model_validate(tampered.model_dump())
    with pytest.raises(ValueError, match="deterministic"):
        IncrementalEmaCheckpoint.model_validate({**seed.model_dump(), "prefix_hash": "0" * 64})

    late_bar = _bar(8, "108", available_hour=1).model_copy(
        update={"available_at": datetime(2026, 4, 1, tzinfo=UTC)}
    )
    with pytest.raises(IncrementalEmaPrefixError, match="future evidence"):
        resume(seed, (*bars, late_bar), known_at=_KNOWN)
    with pytest.raises(IncrementalEmaCheckpointError, match="not available at known_at"):
        resume(seed, bars, known_at=datetime(2026, 1, 2, tzinfo=UTC))

    with pytest.raises(IncrementalEmaCheckpointError, match="timezone-aware"):
        validate_checkpoint(seed, bars, known_at=datetime(2026, 3, 1))

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        IncrementalEmaCheckpoint.model_validate({"unexpected": "shape"})


def test_interrupted_resume_recovers_from_last_valid_checkpoint() -> None:
    bars = _bars(9)
    window = 3
    first = resume(
        seed_checkpoint(bars[:5], asset_id=_ASSET, source_id=_SOURCE, window=window),
        bars[:5],
        known_at=_KNOWN,
    )
    assert first.prefix_length == 5
    second = resume(first, bars, known_at=_KNOWN)
    full = full_checkpoint(bars, asset_id=_ASSET, source_id=_SOURCE, window=window)
    assert second.value == full.value
    assert second.checkpoint_id == full.checkpoint_id

    query = HistoricalBarQuery(
        asset_id=_ASSET,
        source_id=_SOURCE,
        start=_START,
        end=_START + timedelta(days=len(bars) + 1),
        known_at=_KNOWN,
    )
    assert query.start == _START
