"""Tests for the canonical incremental RSI/ATR/MACD graph contract."""

from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import uuid4

import pytest

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.incremental_ema import (
    IncrementalEmaPrefixError,
    IncrementalEmaScopeError,
)
from investment_analyst.analytics.market.incremental_ema import (
    full_checkpoint as ema_full,
)
from investment_analyst.analytics.market.incremental_recursive import (
    ATR_ALGORITHM_VERSION,
    MACD_ALGORITHM_VERSION,
    RSI_ALGORITHM_VERSION,
    IncrementalAtrCheckpoint,
    IncrementalMacdCheckpoint,
    IncrementalRecursiveCheckpointError,
    IncrementalRecursivePrefixError,
    IncrementalRecursiveScopeError,
    IncrementalRsiCheckpoint,
    full_atr,
    full_macd,
    full_rsi,
    resume_atr,
    resume_macd,
    resume_rsi,
    seed_atr,
    seed_macd,
    seed_rsi,
    validate_macd_checkpoint,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.core.models import DataFrequency, DataQuality

_SERIES_START = datetime(2026, 1, 1, tzinfo=UTC)
_SERIES_KNOWN = datetime(2026, 3, 1, tzinfo=UTC)


def _series(closes: tuple[str, ...]) -> MarketBarSeries:
    query = HistoricalBarQuery(
        asset_id=_ASSET,
        source_id=_SOURCE,
        start=_SERIES_START,
        end=_SERIES_START + timedelta(days=max(len(closes), 1) + 1),
        known_at=_SERIES_KNOWN,
    )
    bars = tuple(
        MarketBar(
            asset_id=query.asset_id,
            source_id=query.source_id,
            raw_record_id=uuid4(),
            frequency=DataFrequency.DAY_1,
            timestamp=_SERIES_START + timedelta(days=index),
            available_at=_SERIES_START + timedelta(days=index, hours=1),
            open=Decimal(close),
            high=Decimal(close) + Decimal("1"),
            low=Decimal(close) - Decimal("0.5"),
            close=Decimal(close),
            volume=Decimal("100"),
            quality=DataQuality.VALID,
            observation_ids={
                "open": uuid4(),
                "high": uuid4(),
                "low": uuid4(),
                "close": uuid4(),
                "volume": uuid4(),
            },
        )
        for index, close in enumerate(closes)
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


_ASSET = "crypto:btc-usd"
_SOURCE = "coinbase-exchange:btc-usd:daily-candles"
_START = datetime(2026, 1, 1, tzinfo=UTC)
_KNOWN = datetime(2026, 3, 1, tzinfo=UTC)


def _bar(
    index: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
    asset_id: str = _ASSET,
    source_id: str = _SOURCE,
    start: datetime = _START,
    close_id=None,
    available_hour: int = 1,
) -> MarketBar:
    close_value = Decimal(close)
    return MarketBar(
        asset_id=asset_id,
        source_id=source_id,
        raw_record_id=uuid4(),
        frequency=DataFrequency.DAY_1,
        timestamp=start + timedelta(days=index),
        available_at=start + timedelta(days=index, hours=available_hour),
        open=close_value,
        high=Decimal(high) if high is not None else close_value + Decimal("1"),
        low=Decimal(low) if low is not None else close_value - Decimal("0.5"),
        close=close_value,
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
    closes = (
        "100",
        "102",
        "101",
        "103",
        "105",
        "104",
        "106",
        "108",
        "107",
        "109",
        "111",
        "110",
        "112",
        "114",
        "113",
        "115",
        "117",
        "116",
        "118",
        "120",
        "119",
        "121",
        "123",
        "122",
        "124",
        "126",
        "125",
        "127",
        "129",
        "128",
    )
    return tuple(_bar(index, closes[index % len(closes)], **kwargs) for index in range(count))


def _engine_values(
    closes: tuple[str, ...], *, rsi_window=3, atr_window=3, fast=2, slow=5, signal=3
) -> dict[str, list[Decimal]]:
    series = _series(closes)
    request = MarketStatisticsRequest(
        query=series.query,
        rsi_window=rsi_window,
        atr_window=atr_window,
        macd_fast_window=fast,
        macd_slow_window=slow,
        macd_signal_window=signal,
    )
    computation = MarketStatisticsEngine().compute(series, request)
    grouped: dict[str, list[Decimal]] = {}
    for item in computation.calculations:
        grouped.setdefault(item.metric_key, []).append(item.value)
    return grouped


def _independent_rsi(closes: tuple[Decimal, ...], window: int) -> list[Decimal]:
    with localcontext(Context(prec=34)):
        changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
        gain = sum((max(c, Decimal("0")) for c in changes[:window]), Decimal("0")) / Decimal(window)
        loss = sum((max(-c, Decimal("0")) for c in changes[:window]), Decimal("0")) / Decimal(
            window
        )
        values = [_rsi_value(gain, loss)]
        for change in changes[window:]:
            gain = ((Decimal(window - 1) * gain) + max(change, Decimal("0"))) / Decimal(window)
            loss = ((Decimal(window - 1) * loss) + max(-change, Decimal("0"))) / Decimal(window)
            values.append(_rsi_value(gain, loss))
    return values


def _rsi_value(gain: Decimal, loss: Decimal) -> Decimal:
    if gain == 0 and loss == 0:
        return Decimal("50")
    if loss == 0:
        return Decimal("100")
    if gain == 0:
        return Decimal("0")
    return Decimal("100") - Decimal("100") / (Decimal("1") + gain / loss)


def _independent_atr(bars: tuple[MarketBar, ...], window: int) -> list[Decimal]:
    with localcontext(Context(prec=34)):
        ranges = []
        for index, current in enumerate(bars):
            previous = bars[index - 1] if index else None
            if previous is None:
                ranges.append(current.high - current.low)
            else:
                ranges.append(
                    max(
                        current.high - current.low,
                        abs(current.high - previous.close),
                        abs(current.low - previous.close),
                    )
                )
        atr = sum(ranges[:window], Decimal("0")) / Decimal(window)
        values = [atr]
        for current_range in ranges[window:]:
            atr = ((Decimal(window - 1) * atr) + current_range) / Decimal(window)
            values.append(atr)
    return values


def _independent_ema(values: tuple[Decimal, ...], window: int) -> list[Decimal]:
    with localcontext(Context(prec=34)):
        alpha = Decimal("2") / Decimal(window + 1)
        seed = sum(values[:window], Decimal("0")) / Decimal(window)
        output = [seed]
        for value in values[window:]:
            output.append(alpha * value + (Decimal("1") - alpha) * output[-1])
    return output


def test_recursive_state_matches_independent_decimal34_baselines() -> None:
    bars = _bars(20)
    closes = tuple(bar.close for bar in bars)

    rsi = full_rsi(bars, asset_id=_ASSET, source_id=_SOURCE, window=3)
    assert rsi.algorithm_version == RSI_ALGORITHM_VERSION
    assert rsi.rsi == _independent_rsi(closes, 3)[-1]
    assert rsi.prefix_length == len(bars)
    assert rsi.model_dump_json() == rsi.model_copy().model_dump_json()

    atr = full_atr(bars, asset_id=_ASSET, source_id=_SOURCE, window=3)
    assert atr.algorithm_version == ATR_ALGORITHM_VERSION
    assert atr.atr == _independent_atr(bars, 3)[-1]
    assert atr.prefix_length == len(bars)

    macd = full_macd(
        bars, asset_id=_ASSET, source_id=_SOURCE, fast_window=2, slow_window=5, signal_window=3
    )
    assert macd.algorithm_version == MACD_ALGORITHM_VERSION
    fast_check = _independent_ema(closes, 2)
    slow_check = _independent_ema(closes, 5)
    with localcontext(Context(prec=34)):
        lines_check = [fast - slow for fast, slow in zip(fast_check[3:], slow_check, strict=True)]
        signal_alpha_check = Decimal("2") / Decimal(4)
        signal_check = sum(lines_check[:3], Decimal("0")) / Decimal(3)
        for line_value in lines_check[3:]:
            signal_check = (
                signal_alpha_check * line_value + (Decimal("1") - signal_alpha_check) * signal_check
            )
    assert macd.line == lines_check[-1]
    assert macd.signal == signal_check
    with localcontext(Context(prec=34)):
        assert macd.histogram == lines_check[-1] - signal_check
    assert macd.fast_ema == fast_check[-1]
    assert macd.slow_ema == slow_check[-1]

    engine = _engine_values(
        tuple(str(value) for value in ("100", "102", "101", "103", "105", "104", "106")),
        rsi_window=3,
        atr_window=3,
        fast=2,
        slow=5,
        signal=2,
    )
    assert engine["market.technical.rsi"][-1] == _independent_rsi(closes[:7], 3)[-1]


def test_recursive_full_equals_resumed_across_cuts() -> None:
    bars = _bars(16)
    rsi_full = full_rsi(bars, asset_id=_ASSET, source_id=_SOURCE, window=4)
    atr_full = full_atr(bars, asset_id=_ASSET, source_id=_SOURCE, window=4)
    macd_full = full_macd(
        bars, asset_id=_ASSET, source_id=_SOURCE, fast_window=3, slow_window=6, signal_window=3
    )
    rsi_cuts = (5, 7, len(bars) - 1, len(bars))
    macd_cuts = (8, 10, len(bars) - 1, len(bars))
    for cut in rsi_cuts:
        seed = seed_rsi(bars[:cut], asset_id=_ASSET, source_id=_SOURCE, window=4)
        resumed = resume_rsi(seed, bars, known_at=_KNOWN)
        assert resumed.rsi == rsi_full.rsi
        assert resumed.average_gain == rsi_full.average_gain
        assert resumed.average_loss == rsi_full.average_loss
        assert resumed.checkpoint_id == rsi_full.checkpoint_id
        assert resumed.prefix_hash == rsi_full.prefix_hash
        assert resumed.prefix_length == len(bars)
    for cut in (4, 6, len(bars) - 1, len(bars)):
        seed = seed_atr(bars[:cut], asset_id=_ASSET, source_id=_SOURCE, window=4)
        resumed = resume_atr(seed, bars, known_at=_KNOWN)
        assert resumed.atr == atr_full.atr
        assert resumed.true_range == atr_full.true_range
        assert resumed.checkpoint_id == atr_full.checkpoint_id
    for cut in macd_cuts:
        seed = seed_macd(
            bars[:cut],
            asset_id=_ASSET,
            source_id=_SOURCE,
            fast_window=3,
            slow_window=6,
            signal_window=3,
        )
        resumed = resume_macd(seed, bars, known_at=_KNOWN)
        assert resumed.line == macd_full.line
        assert resumed.signal == macd_full.signal
        assert resumed.histogram == macd_full.histogram
        assert resumed.checkpoint_id == macd_full.checkpoint_id
        assert resumed.prefix_length == len(bars)

    gapped = tuple(_bar(index * 2, str(100 + index)) for index in range(10))
    gapped_rsi = full_rsi(gapped, asset_id=_ASSET, source_id=_SOURCE, window=3)
    assert (
        resume_rsi(
            seed_rsi(gapped[:4], asset_id=_ASSET, source_id=_SOURCE, window=3),
            gapped,
            known_at=_KNOWN,
        ).checkpoint_id
        == gapped_rsi.checkpoint_id
    )
    eth_bars = _bars(
        10, asset_id="crypto:eth-usd", source_id="coinbase-exchange:eth-usd:daily-candles"
    )
    eth_rsi = full_rsi(
        eth_bars,
        asset_id="crypto:eth-usd",
        source_id="coinbase-exchange:eth-usd:daily-candles",
        window=3,
    )
    assert eth_rsi.checkpoint_id != rsi_full.checkpoint_id
    first = resume_rsi(
        seed_rsi(bars[:7], asset_id=_ASSET, source_id=_SOURCE, window=4),
        bars[:7],
        known_at=_KNOWN,
    )
    second = resume_rsi(first, bars, known_at=_KNOWN)
    assert second.checkpoint_id == rsi_full.checkpoint_id


def test_recursive_checkpoints_reject_revision_corruption_and_future() -> None:
    bars = _bars(10)
    rsi_seed = seed_rsi(bars, asset_id=_ASSET, source_id=_SOURCE, window=3)
    atr_seed = seed_atr(bars, asset_id=_ASSET, source_id=_SOURCE, window=3)
    macd_seed = seed_macd(
        bars, asset_id=_ASSET, source_id=_SOURCE, fast_window=2, slow_window=5, signal_window=3
    )

    revised = list(bars)
    revised_id = uuid4()
    revised[1] = _bar(1, "999", close_id=revised_id)
    assert revised_id != bars[1].observation_ids["close"]
    with pytest.raises(IncrementalRecursivePrefixError, match="revised or truncated"):
        resume_rsi(rsi_seed, tuple(revised), known_at=_KNOWN)
    with pytest.raises(IncrementalRecursivePrefixError, match="revised or truncated"):
        resume_atr(atr_seed, tuple(revised), known_at=_KNOWN)
    with pytest.raises(IncrementalRecursivePrefixError, match="revised or truncated"):
        resume_macd(macd_seed, tuple(revised), known_at=_KNOWN)

    truncated = bars[: rsi_seed.prefix_length - 1] + bars[rsi_seed.prefix_length :]
    with pytest.raises(
        (IncrementalRecursivePrefixError, IncrementalRecursiveScopeError), match="."
    ):
        resume_rsi(rsi_seed, truncated, known_at=_KNOWN)

    tampered = rsi_seed.model_copy(update={"rsi": rsi_seed.rsi + Decimal("1")})
    with pytest.raises(ValueError, match="deterministic"):
        IncrementalRsiCheckpoint.model_validate(tampered.model_dump())
    tampered_atr = atr_seed.model_copy(update={"atr": atr_seed.atr + Decimal("1")})
    with pytest.raises(ValueError, match="deterministic"):
        IncrementalAtrCheckpoint.model_validate(tampered_atr.model_dump())
    tampered_macd = macd_seed.model_copy(update={"line": macd_seed.line + Decimal("1")})
    with pytest.raises(ValueError, match="deterministic"):
        IncrementalMacdCheckpoint.model_validate(tampered_macd.model_dump())

    foreign = _bars(10, asset_id="equity:us:aapl", source_id="other-source")
    with pytest.raises(
        (IncrementalRecursiveScopeError, IncrementalEmaScopeError), match="asset or source"
    ):
        seed_macd(
            foreign,
            asset_id=_ASSET,
            source_id=_SOURCE,
            fast_window=2,
            slow_window=5,
            signal_window=3,
        )
    mixed = (*bars[:5], _bar(5, "105", asset_id="equity:us:aapl"), *bars[6:])
    with pytest.raises(
        (IncrementalRecursiveScopeError, IncrementalEmaScopeError), match="asset or source"
    ):
        seed_atr(mixed, asset_id=_ASSET, source_id=_SOURCE, window=3)
    late_bar = _bar(10, "110", available_hour=1).model_copy(
        update={"available_at": datetime(2026, 4, 1, tzinfo=UTC)}
    )
    with pytest.raises(IncrementalRecursivePrefixError, match="future evidence"):
        resume_rsi(rsi_seed, (*bars, late_bar), known_at=_KNOWN)
    with pytest.raises(IncrementalRecursiveCheckpointError, match="not available at known_at"):
        resume_atr(atr_seed, bars, known_at=datetime(2026, 1, 2, tzinfo=UTC))
    with pytest.raises(IncrementalRecursiveCheckpointError, match="timezone-aware"):
        validate_macd_checkpoint(macd_seed, bars, known_at=datetime(2026, 3, 1))
    with pytest.raises(ValueError, match="at least 2"):
        seed_rsi(bars, asset_id=_ASSET, source_id=_SOURCE, window=1)
    with pytest.raises(ValueError, match="must be less than"):
        seed_macd(
            bars,
            asset_id=_ASSET,
            source_id=_SOURCE,
            fast_window=5,
            slow_window=5,
            signal_window=3,
        )
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        IncrementalRsiCheckpoint.model_validate({"unexpected": "shape"})
    missing = list(bars)
    broken = missing[2].model_copy(
        update={
            "observation_ids": {
                key: value for key, value in missing[2].observation_ids.items() if key != "close"
            }
        }
    )
    with pytest.raises(
        (IncrementalRecursivePrefixError, IncrementalEmaPrefixError),
        match="close observation",
    ):
        seed_rsi(
            tuple([*missing[:2], broken, *missing[3:]]),
            asset_id=_ASSET,
            source_id=_SOURCE,
            window=3,
        )


def test_recursive_contract_does_not_change_productive_engine() -> None:
    closes = ("100", "102", "101", "103", "105", "104", "106", "108")
    series = _series(closes)
    before = MarketStatisticsEngine().compute(
        series,
        MarketStatisticsRequest(
            query=series.query,
            rsi_window=3,
            atr_window=3,
            macd_fast_window=2,
            macd_slow_window=5,
            macd_signal_window=2,
        ),
    )
    bars = tuple(_bar(index, close) for index, close in enumerate(closes))
    assert (
        full_rsi(bars, asset_id=_ASSET, source_id=_SOURCE, window=3).rsi
        == [
            item.value for item in before.calculations if item.metric_key == "market.technical.rsi"
        ][-1]
    )
    after = MarketStatisticsEngine().compute(
        series,
        MarketStatisticsRequest(
            query=series.query,
            rsi_window=3,
            atr_window=3,
            macd_fast_window=2,
            macd_slow_window=5,
            macd_signal_window=2,
        ),
    )
    assert [item.value for item in after.calculations] == [
        item.value for item in before.calculations
    ]
    assert ema_full(bars, asset_id=_ASSET, source_id=_SOURCE, window=3).value is not None
