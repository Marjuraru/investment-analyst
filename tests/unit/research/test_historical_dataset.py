"""Causal reconstruction, exact label, gaps, purge and readiness for the dataset."""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.analytics.market.bar_models import MarketBar
from investment_analyst.analytics.market.incremental_recursive import full_macd, full_rsi
from investment_analyst.core.models.enums import DataFrequency, DataQuality
from investment_analyst.research import historical_dataset, historical_features
from investment_analyst.research.historical_archive import (
    build_historical_archive,
    resolve_historical_scope,
    validate_archive_range,
)
from investment_analyst.research.historical_dataset import (
    HistoricalDatasetRequestError,
    build_historical_dataset,
)
from investment_analyst.research.historical_features import reconstruct_feature_series
from investment_analyst.research.historical_models import (
    CLOSE_RETURN_COLUMN,
    DECIMAL34,
    FEATURE_COLUMNS,
    MACD_HISTOGRAM_COLUMN,
    RSI_COLUMN,
    FeatureState,
    HistoricalArchiveRequest,
    HistoricalCandle,
    HistoricalDecisionRequest,
    LabelState,
    PurgeReason,
    ReadinessStatus,
    ReasonCode,
    SampleSplit,
    assumed_available_instant,
    bar_start_instant,
    decision_instant,
    historical_bar_content_id,
    label_exit_bar_end,
    split_boundaries,
)

_ASSET = "crypto:btc-usd"
_START = date(2019, 1, 1)
_RETRIEVED = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"


def _price(index: int) -> Decimal:
    return (
        Decimal(100) + Decimal(index) * Decimal("0.37") + Decimal((index % 7) - 3) * Decimal("1.13")
    )


def _candle(
    day: date,
    price: Decimal,
    *,
    asset_id: str,
    source_id: str,
    retrieved_at: datetime,
) -> HistoricalCandle:
    start = bar_start_instant(day)
    end = start + timedelta(days=1)
    raw = (str(int(start.timestamp())), str(price), str(price + 1), str(price), str(price), "9.5")
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=asset_id,
            source_id=source_id,
            bar_start=start,
            open_value=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=Decimal("9.5"),
            raw_values=raw,
        ),
        bar_start=start,
        bar_end=end,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=Decimal("9.5"),
        raw_values=raw,
        assumed_available_at=assumed_available_instant(end),
        retrieved_at=retrieved_at,
    )


def _archive(
    prices: dict[date, Decimal],
    *,
    asset_id: str = _ASSET,
    retrieved_at: datetime = _RETRIEVED,
):
    scope = resolve_historical_scope(asset_id).asset
    candles = tuple(
        _candle(
            day,
            price,
            asset_id=scope.asset_id,
            source_id=scope.source_id,
            retrieved_at=retrieved_at,
        )
        for day, price in sorted(prices.items())
    )
    request = HistoricalArchiveRequest(
        asset_id=asset_id,
        start_date=candles[0].bar_start.date(),
        end_date=candles[-1].bar_start.date(),
    )
    return build_historical_archive(
        scope,
        request,
        request_urls=(_URL,),
        retrieved_at=retrieved_at,
        candles=candles,
    )


def _contiguous(start: date, count: int, *, skip: tuple[int, ...] = ()) -> dict[date, Decimal]:
    return {
        start + timedelta(days=index): _price(index) for index in range(count) if index not in skip
    }


def _market_bar(candle: HistoricalCandle, index: int) -> MarketBar:
    return MarketBar(
        asset_id=_ASSET,
        source_id="coinbase-exchange:btc-usd:daily-candles",
        raw_record_id=uuid5(NAMESPACE_URL, f"raw-{index}"),
        frequency=DataFrequency.DAY_1,
        timestamp=candle.bar_start,
        available_at=candle.assumed_available_at,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        quality=DataQuality.VALID,
        observation_ids={
            name: uuid5(NAMESPACE_URL, f"obs-{index}-{name}")
            for name in ("open", "high", "low", "close", "volume")
        },
    )


class _MarketBars:
    def __init__(self, candles: tuple[HistoricalCandle, ...]) -> None:
        self._candles = candles

    def prefix(self, count: int) -> tuple[MarketBar, ...]:
        return tuple(
            _market_bar(candle, index) for index, candle in enumerate(self._candles[:count])
        )


def _request(
    start: date,
    end: date,
    *,
    known_at: datetime = _RETRIEVED,
    asset_id: str = _ASSET,
) -> HistoricalDecisionRequest:
    return HistoricalDecisionRequest(
        asset_id=asset_id,
        start_date=start,
        end_date=end,
        known_at=known_at,
    )


def test_reconstruction_matches_canonical_rsi_and_macd_prefixes() -> None:
    archive = _archive(_contiguous(_START, 80))
    series = reconstruct_feature_series(archive.candles)
    bars = _MarketBars(archive.candles)
    source = archive.manifest.asset.source_id
    for count in (15, 16, 34, 35, 60, 80):
        point = series.point(_START + timedelta(days=count - 1))
        assert point is not None
        canonical_rsi = full_rsi(bars.prefix(count), asset_id=_ASSET, source_id=source, window=14)
        assert point.rsi == canonical_rsi.rsi
        if count >= 34:
            canonical_macd = full_macd(
                bars.prefix(count),
                asset_id=_ASSET,
                source_id=source,
                fast_window=12,
                slow_window=26,
                signal_window=9,
            )
            assert point.macd_histogram == canonical_macd.histogram
        else:
            assert point.macd_histogram is None


def test_gap_resets_segment_warmup_without_crossing_it() -> None:
    prices = _contiguous(_START, 80, skip=(21,))
    archive = _archive(prices)
    series = reconstruct_feature_series(archive.candles)
    before = series.point(_START + timedelta(days=20))
    assert before is not None
    assert before.segment_bars == 21
    assert before.rsi is not None
    assert before.macd_histogram is None
    after = series.point(_START + timedelta(days=22))
    assert after is not None
    assert after.segment_bars == 1
    assert after.segment_seed_start == _START + timedelta(days=22)
    assert after.rsi is None
    assert after.macd_histogram is None
    rsi_ready = series.point(_START + timedelta(days=22 + 14))
    assert rsi_ready is not None and rsi_ready.rsi is not None
    macd_ready = series.point(_START + timedelta(days=22 + 33))
    assert macd_ready is not None and macd_ready.macd_histogram is not None

    dataset = build_historical_dataset(
        _request(_START + timedelta(days=23), _START + timedelta(days=30)), archive
    )
    first = dataset.samples[0]
    assert first.feature_state is FeatureState.MISSING
    assert first.features[1].reasons[0].code is ReasonCode.WARMUP_INCOMPLETE
    assert first.features[2].reasons[0].code is ReasonCode.WARMUP_INCOMPLETE


def test_features_use_only_previous_closes_with_assumed_availability() -> None:
    prices = _contiguous(_START, 60)
    archive = _archive(prices)
    decision = _START + timedelta(days=50)
    dataset = build_historical_dataset(_request(decision, decision), archive)
    sample = dataset.samples[0]
    with localcontext(DECIMAL34):
        expected = prices[decision + timedelta(days=-1)] / prices[decision + timedelta(days=-2)] - 1
    close_return = sample.features[0]
    assert close_return.value == expected
    assert close_return.assumed_available_at == bar_start_instant(decision) + timedelta(hours=1)
    assert close_return.assumed_available_at <= decision_instant(decision)
    assert sample.decision_at == decision_instant(decision)


def test_future_bars_do_not_change_earlier_features_or_feature_hash() -> None:
    base_prices = _contiguous(_START, 80)
    changed_prices = dict(base_prices)
    for offset in range(70, 80):
        changed_prices[_START + timedelta(days=offset)] = _price(offset) + Decimal("5")
    first = build_historical_dataset(
        _request(_START + timedelta(days=40), _START + timedelta(days=42)),
        _archive(base_prices),
    )
    second = build_historical_dataset(
        _request(_START + timedelta(days=40), _START + timedelta(days=42)),
        _archive(changed_prices),
    )
    for left, right in zip(first.samples, second.samples, strict=True):
        assert left.sample_key == right.sample_key
        assert left.features == right.features
        assert left.feature_hash == right.feature_hash
    past_changed = dict(base_prices)
    past_changed[_START + timedelta(days=30)] = _price(30) + Decimal("5")
    third = build_historical_dataset(
        _request(_START + timedelta(days=40), _START + timedelta(days=42)),
        _archive(past_changed),
    )
    assert third.samples[0].feature_hash != first.samples[0].feature_hash
    assert third.samples[0].row_id != first.samples[0].row_id

    label_changed_prices = dict(base_prices)
    for offset in range(44, 48):
        label_changed_prices[_START + timedelta(days=offset)] = _price(offset) + Decimal("7")
    fourth = build_historical_dataset(
        _request(_START + timedelta(days=40), _START + timedelta(days=42)),
        _archive(label_changed_prices),
    )
    assert fourth.samples[0].feature_hash == first.samples[0].feature_hash
    assert fourth.samples[0].row_id != first.samples[0].row_id


def test_label_is_exact_six_bar_window_and_zero_is_negative() -> None:
    prices = {_START + timedelta(days=index): _price(index) for index in range(40)}
    decision = _START + timedelta(days=20)
    prices[decision + timedelta(days=5)] = prices[decision]
    archive = _archive(prices)
    dataset = build_historical_dataset(_request(decision, decision, known_at=_RETRIEVED), archive)
    label = dataset.samples[0].label
    assert label.state is LabelState.AVAILABLE
    assert label.entry_bar_start == bar_start_instant(decision)
    assert label.exit_bar_end == label_exit_bar_end(decision)
    assert len(label.bars) == 6
    assert [bar.bar_start for bar in label.bars] == [
        bar_start_instant(decision + timedelta(days=offset)) for offset in range(6)
    ]
    assert label.return_5d == 0
    assert label.label == 0
    assert label.evidence_available_at == _RETRIEVED


def test_label_before_maturity_is_not_yet_available_and_never_zero() -> None:
    retrieved = datetime(2019, 1, 25, tzinfo=UTC)
    archive = _archive(_contiguous(_START, 25), retrieved_at=retrieved)
    decision = _START + timedelta(days=19)
    dataset = build_historical_dataset(
        _request(decision, decision, known_at=retrieved),
        archive,
    )
    label = dataset.samples[0].label
    assert label.state is LabelState.NOT_YET_AVAILABLE
    assert label.label is None
    assert label.return_5d is None
    assert label.reasons[0].code is ReasonCode.LABEL_NOT_MATURED
    assert dataset.samples[0].eligible is False


def test_internal_gap_is_represented_and_not_filled() -> None:
    prices = _contiguous(_START, 60, skip=(31,))
    archive = _archive(prices)
    affected = _START + timedelta(days=31)
    dataset = build_historical_dataset(
        _request(affected + timedelta(days=-5), affected + timedelta(days=5)),
        archive,
    )
    missing = [sample for sample in dataset.samples if sample.label_state is LabelState.MISSING]
    assert missing
    assert any(sample.decision_at.date() == affected for sample in missing)
    assert dataset.samples[0].feature_state in {FeatureState.AVAILABLE, FeatureState.MISSING}
    assert dataset.summary.counts.label_missing >= 1
    assert all(sample.eligible is False for sample in missing)
    assert archive.manifest.counts.absent_days == 1


def test_purge_and_embargo_exclude_boundary_rows_before_eligibility() -> None:
    archive = _archive(_contiguous(_START, 110))
    decisions_start = _START + timedelta(days=10)
    dataset = build_historical_dataset(
        _request(decisions_start, decisions_start + timedelta(days=89)),
        archive,
    )
    decision_dates = tuple(sample.decision_at.date() for sample in dataset.samples)
    train_end, _ = split_boundaries(len(decision_dates))
    boundary = bar_start_instant(decision_dates[train_end])
    train = tuple(sample for sample in dataset.samples if sample.split is SampleSplit.TRAIN)
    overlap = [
        sample for sample in train if sample.purge_reason is PurgeReason.LABEL_INTERVAL_OVERLAP
    ]
    embargo = [sample for sample in train if sample.purge_reason is PurgeReason.EMBARGO]
    assert overlap
    assert embargo
    assert all(label_exit_bar_end(sample.decision_at.date()) >= boundary for sample in overlap)
    assert all(label_exit_bar_end(sample.decision_at.date()) < boundary for sample in embargo)
    assert all(sample.eligible is False for sample in overlap + embargo)


def test_readiness_requires_every_declared_floor() -> None:
    alternating = {
        _START + timedelta(days=index): Decimal(100) if index % 2 == 0 else Decimal(102)
        for index in range(520)
    }
    archive = _archive(alternating)
    decisions_start = _START + timedelta(days=40)
    dataset = build_historical_dataset(
        _request(decisions_start, decisions_start + timedelta(days=440)),
        archive,
    )
    assert dataset.summary.readiness.status is ReadinessStatus.RECONSTRUCTION_READY
    assert dataset.summary.readiness.strict_pit_verified is False
    assert dataset.summary.splits[0].positive >= 20
    assert dataset.summary.splits[0].negative >= 20
    small = build_historical_dataset(
        _request(decisions_start, decisions_start + timedelta(days=80)),
        archive,
    )
    assert small.summary.readiness.status is ReadinessStatus.INSUFFICIENT_RECONSTRUCTED_SAMPLE
    assert small.summary.readiness.unmet_requirements


def test_two_profile_assets_resolve_generically_and_enforce_history_start() -> None:
    btc = resolve_historical_scope("crypto:btc-usd")
    sol = resolve_historical_scope("crypto:sol-usd")
    assert btc.asset.asset_id != sol.asset.asset_id
    assert btc.product_id != sol.product_id
    assert btc.asset.source_id != sol.asset.source_id
    assert btc.history_start is None
    assert sol.history_start == "2025-01-01"
    with pytest.raises(ValueError, match="history starts"):
        validate_archive_range(
            sol,
            HistoricalArchiveRequest(
                asset_id="crypto:sol-usd",
                start_date=date(2019, 1, 1),
                end_date=date(2019, 1, 5),
            ),
        )
    archive = _archive(_contiguous(date(2025, 1, 1), 60), asset_id="crypto:sol-usd")
    dataset = build_historical_dataset(
        _request(date(2025, 1, 30), date(2025, 1, 31), asset_id="crypto:sol-usd"),
        archive,
    )
    assert dataset.summary.asset.asset_id == "crypto:sol-usd"


def test_request_outside_archive_and_before_reception_fails_closed() -> None:
    archive = _archive(_contiguous(_START, 60))
    with pytest.raises(HistoricalDatasetRequestError, match="start before"):
        build_historical_dataset(_request(_START - timedelta(days=1), _START), archive)
    with pytest.raises(HistoricalDatasetRequestError, match="end after"):
        build_historical_dataset(_request(_START, _START + timedelta(days=60)), archive)
    with pytest.raises(HistoricalDatasetRequestError, match="label window"):
        build_historical_dataset(
            _request(_START + timedelta(days=50), _START + timedelta(days=59)), archive
        )
    early = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(HistoricalDatasetRequestError, match="reception"):
        build_historical_dataset(
            HistoricalDecisionRequest(
                asset_id=_ASSET,
                start_date=_START + timedelta(days=10),
                end_date=_START + timedelta(days=11),
                known_at=early,
            ),
            archive,
        )


def test_reconstruction_never_uses_operational_storage_models() -> None:
    for module in (historical_features, historical_dataset):
        source = inspect.getsource(module)
        assert "investment_analyst.storage" not in source
        assert "investment_analyst.workspace" not in source
        assert "MarketBar" not in source
        assert "MetricResult" not in source
    assert (
        tuple(
            item.column_name
            for item in build_historical_dataset(
                _request(_START + timedelta(days=40), _START + timedelta(days=40)),
                _archive(_contiguous(_START, 60)),
            )
            .samples[0]
            .features
        )
        == FEATURE_COLUMNS
    )
    assert CLOSE_RETURN_COLUMN != RSI_COLUMN != MACD_HISTOGRAM_COLUMN
