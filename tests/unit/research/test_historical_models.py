"""Strict historical contracts, identities, splits and readiness rules."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.core.models.enums import AssetClass, DataFrequency
from investment_analyst.research.historical_models import (
    CLOSE_RETURN_COLUMN,
    DECIMAL34,
    FEATURE_COLUMNS,
    MACD_HISTOGRAM_COLUMN,
    PREREGISTERED_SPLIT_POLICY,
    RSI_COLUMN,
    AvailabilityBasis,
    EvidenceReason,
    FeatureState,
    HistoricalArchiveCounts,
    HistoricalArchiveManifest,
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    HistoricalDatasetCounts,
    HistoricalDecisionRequest,
    HistoricalFeatureEvidence,
    HistoricalLabelBarEvidence,
    HistoricalLabelEvidence,
    HistoricalReadiness,
    HistoricalSample,
    HistoricalSplitPolicy,
    LabelState,
    PurgeReason,
    ReadinessStatus,
    ReasonCode,
    SampleSplit,
    assumed_available_instant,
    bar_start_instant,
    decision_instant,
    historical_archive_id,
    historical_archive_semantic_hash,
    historical_bar_content_id,
    historical_feature_hash,
    historical_row_id,
    historical_sample_key,
    label_exit_bar_end,
    split_boundaries,
)

_DAY = date(2024, 3, 4)
_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
_RETRIEVED = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)


def _asset() -> HistoricalAssetIdentity:
    return HistoricalAssetIdentity(
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        product_id="BTC-USD",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        crypto_profile="bitcoin",
        exchange="COINBASE",
        granularity_seconds=86_400,
    )


def _candle(
    day: date,
    *,
    close: Decimal = Decimal("100"),
    asset_id: str = _ASSET_ID,
    retrieved_at: datetime = _RETRIEVED,
) -> HistoricalCandle:
    start = bar_start_instant(day)
    end = start + timedelta(days=1)
    raw_values = (
        str(int(start.timestamp())),
        str(close),
        str(close + 1),
        str(close),
        str(close),
        "12.5",
    )
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=asset_id,
            source_id=_SOURCE_ID,
            bar_start=start,
            open_value=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=Decimal("12.5"),
            raw_values=raw_values,
        ),
        bar_start=start,
        bar_end=end,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=Decimal("12.5"),
        raw_values=raw_values,
        assumed_available_at=assumed_available_instant(end),
        retrieved_at=retrieved_at,
    )


def _reason(detail: str = "missing") -> EvidenceReason:
    return EvidenceReason(code=ReasonCode.BAR_ABSENT, detail=detail)


def _feature(
    column: str,
    *,
    value: Decimal | None = None,
    state: FeatureState = FeatureState.AVAILABLE,
    warmup: int = 40,
    seed: date = date(2019, 1, 1),
) -> HistoricalFeatureEvidence:
    if state is FeatureState.AVAILABLE:
        return HistoricalFeatureEvidence(
            column_name=column,
            state=state,
            value=value if value is not None else Decimal("0.01"),
            algorithm_version=f"{column}-v1",
            segment_seed_start=seed,
            warmup_bars=warmup,
            assumed_available_at=bar_start_instant(_DAY) + timedelta(hours=1),
        )
    return HistoricalFeatureEvidence(
        column_name=column,
        state=state,
        algorithm_version=f"{column}-v1",
        segment_seed_start=seed,
        warmup_bars=warmup,
        reasons=(_reason(),),
    )


def _features(**overrides: HistoricalFeatureEvidence) -> tuple[HistoricalFeatureEvidence, ...]:
    ordered = {
        CLOSE_RETURN_COLUMN: _feature(CLOSE_RETURN_COLUMN),
        RSI_COLUMN: _feature(RSI_COLUMN, value=Decimal("0.5")),
        MACD_HISTOGRAM_COLUMN: _feature(MACD_HISTOGRAM_COLUMN, value=Decimal("0.02")),
        **overrides,
    }
    return tuple(ordered[name] for name in FEATURE_COLUMNS)


def _bars(decision: date, *, closes: tuple[Decimal, ...]) -> tuple[HistoricalLabelBarEvidence, ...]:
    bars: list[HistoricalLabelBarEvidence] = []
    for offset, close in enumerate(closes):
        day = decision + timedelta(days=offset)
        start = bar_start_instant(day)
        bars.append(
            HistoricalLabelBarEvidence(
                bar_content_id=UUID(int=offset + 1),
                bar_start=start,
                bar_end=start + timedelta(days=1),
                state=FeatureState.AVAILABLE,
                value=close,
                assumed_available_at=assumed_available_instant(start + timedelta(days=1)),
            )
        )
    return tuple(bars)


def _label(
    decision: date = _DAY,
    *,
    closes: tuple[Decimal, ...] = (
        Decimal("100"),
        Decimal("101"),
        Decimal("102"),
        Decimal("103"),
        Decimal("104"),
        Decimal("105"),
    ),
    retrieved_at: datetime = _RETRIEVED,
) -> HistoricalLabelEvidence:
    bars = _bars(decision, closes=closes)
    exit_end = label_exit_bar_end(decision)
    with localcontext(DECIMAL34):
        return_5d = closes[-1] / closes[0] - 1
    return HistoricalLabelEvidence(
        state=LabelState.AVAILABLE,
        decision_at=decision_instant(decision),
        entry_bar_start=bar_start_instant(decision),
        exit_bar_end=exit_end,
        entry_close=closes[0],
        exit_close=closes[-1],
        return_5d=return_5d,
        label=1 if return_5d > 0 else 0,
        evidence_available_at=max(exit_end, retrieved_at),
        archive_retrieved_at=retrieved_at,
        bars=bars,
    )


def test_asset_identity_rejects_other_assets_quotes_and_granularity() -> None:
    with pytest.raises(ValidationError):
        HistoricalAssetIdentity(
            asset_id="equity:us:aapl",
            source_id=_SOURCE_ID,
            product_id="BTC-USD",
            asset_class=AssetClass.EQUITY,
            quote_currency="USD",
            crypto_profile="bitcoin",
            exchange="COINBASE",
            granularity_seconds=86_400,
        )
    with pytest.raises(ValidationError):
        HistoricalAssetIdentity.model_validate(_asset().model_dump() | {"quote_currency": "EUR"})
    with pytest.raises(ValidationError):
        HistoricalAssetIdentity.model_validate(_asset().model_dump() | {"granularity_seconds": 60})


def test_archive_request_is_inclusive_and_bounded() -> None:
    request = HistoricalArchiveRequest(
        asset_id=_ASSET_ID,
        start_date=date(2019, 1, 1),
        end_date=date(2025, 12, 31),
    )
    assert request.requested_days() == 2557
    assert request.interval() == (
        datetime(2019, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ValidationError):
        HistoricalArchiveRequest(
            asset_id=_ASSET_ID,
            start_date=date(2020, 1, 2),
            end_date=date(2020, 1, 1),
        )
    with pytest.raises(ValidationError):
        HistoricalArchiveRequest(
            asset_id=_ASSET_ID,
            start_date=date(2019, 1, 1),
            end_date=date(2026, 1, 10),
        )


def test_candle_rejects_float_naive_time_and_inconsistent_ohlc() -> None:
    candle = _candle(_DAY)
    with pytest.raises(ValidationError):
        HistoricalCandle.model_validate(candle.model_dump() | {"close": 100.5})
    with pytest.raises(ValidationError):
        HistoricalCandle.model_validate(
            candle.model_dump() | {"bar_start": datetime(2024, 3, 4, 0, 0)}
        )
    with pytest.raises(ValidationError):
        HistoricalCandle.model_validate(
            candle.model_dump() | {"low": Decimal("101"), "high": Decimal("99")}
        )
    with pytest.raises(ValidationError):
        HistoricalCandle.model_validate(
            candle.model_dump() | {"raw_values": ("1", "2", "3", "4", "5")}
        )


def test_archive_counts_partition_and_manifest_interval() -> None:
    with pytest.raises(ValidationError):
        HistoricalArchiveCounts(requested_days=3, bar_count=2, absent_days=2)
    request = HistoricalArchiveRequest(
        asset_id=_ASSET_ID,
        start_date=date(2019, 1, 1),
        end_date=date(2019, 1, 3),
    )
    candles = (_candle(date(2019, 1, 1)),)
    archive_id = historical_archive_id(
        asset=_asset(),
        request=request,
        request_urls=("https://api.exchange.coinbase.com/products/BTC-USD/candles",),
        retrieved_at=_RETRIEVED,
        candles=candles,
    )
    manifest = HistoricalArchiveManifest(
        archive_id=archive_id,
        asset=_asset(),
        request=request,
        requested_start=request.interval()[0],
        requested_end=request.interval()[1],
        request_urls=("https://api.exchange.coinbase.com/products/BTC-USD/candles",),
        retrieved_at=_RETRIEVED,
        counts=HistoricalArchiveCounts(requested_days=3, bar_count=1, absent_days=2),
        semantic_sha256=historical_archive_semantic_hash(archive_id=archive_id, candles=candles),
        candles_sha256="0" * 64,
        candles_bytes=10,
        limitations=("limit",),
    )
    assert manifest.strict_pit_verified is False
    assert manifest.availability_basis is AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED
    with pytest.raises(ValidationError):
        HistoricalArchiveManifest.model_validate(
            manifest.model_dump() | {"requested_end": datetime(2019, 1, 3, tzinfo=UTC)}
        )
    with pytest.raises(ValidationError):
        HistoricalArchiveManifest.model_validate(
            manifest.model_dump()
            | {"request_urls": tuple(f"https://example.test/{i}" for i in range(13))}
        )


def test_bar_identity_is_stable_and_changes_with_content() -> None:
    first = _candle(_DAY).bar_content_id
    assert first == _candle(_DAY).bar_content_id
    assert first != _candle(_DAY, close=Decimal("101")).bar_content_id
    assert first != _candle(_DAY, asset_id="crypto:sol-usd").bar_content_id


def test_sample_feature_and_row_identities_separate_causality() -> None:
    decision_at = decision_instant(_DAY)
    sample_key = historical_sample_key(
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        decision_at=decision_at,
    )
    features = _features()
    feature_hash = historical_feature_hash(
        sample_key=sample_key,
        decision_at=decision_at,
        segment_seed_start=date(2019, 1, 1),
        prefix_digest="a" * 64,
        features=features,
    )
    changed_past = historical_feature_hash(
        sample_key=sample_key,
        decision_at=decision_at,
        segment_seed_start=date(2019, 1, 1),
        prefix_digest="b" * 64,
        features=features,
    )
    assert feature_hash != changed_past
    row_id = historical_row_id(
        sample_key=sample_key,
        feature_hash=feature_hash,
        features=features,
        label=_label(),
    )
    changed_future = historical_row_id(
        sample_key=sample_key,
        feature_hash=feature_hash,
        features=features,
        label=_label(
            closes=(
                Decimal("100"),
                Decimal("101"),
                Decimal("102"),
                Decimal("103"),
                Decimal("104"),
                Decimal("99"),
            )
        ),
    )
    assert row_id != changed_future


def test_label_requires_six_continuous_bars_and_exact_return() -> None:
    label = _label()
    assert label.label == 1
    with pytest.raises(ValidationError):
        HistoricalLabelEvidence.model_validate(label.model_dump() | {"bars": label.bars[:5]})
    with pytest.raises(ValidationError):
        HistoricalLabelEvidence.model_validate(label.model_dump() | {"return_5d": Decimal("0.5")})
    zero = _label(
        closes=(
            Decimal("100"),
            Decimal("101"),
            Decimal("102"),
            Decimal("103"),
            Decimal("104"),
            Decimal("100"),
        )
    )
    assert zero.return_5d == 0
    assert zero.label == 0


def test_unavailable_label_hides_target_values() -> None:
    with pytest.raises(ValidationError):
        HistoricalLabelEvidence(
            state=LabelState.MISSING,
            decision_at=decision_instant(_DAY),
            entry_bar_start=bar_start_instant(_DAY),
            exit_bar_end=label_exit_bar_end(_DAY),
            entry_close=Decimal("100"),
            archive_retrieved_at=_RETRIEVED,
            bars=_bars(_DAY, closes=(Decimal("100"),) * 6),
            reasons=(EvidenceReason(code=ReasonCode.LABEL_BAR_ABSENT, detail="gap"),),
        )


def test_feature_evidence_requires_warmup_and_reasons() -> None:
    with pytest.raises(ValidationError):
        _feature(RSI_COLUMN, value=Decimal("0.5"), warmup=14)
    with pytest.raises(ValidationError):
        HistoricalFeatureEvidence(
            column_name=RSI_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version="v1",
        )
    available = _feature(MACD_HISTOGRAM_COLUMN, value=Decimal("0.1"), warmup=34)
    assert available.warmup_bars == 34


def test_split_boundaries_use_floors_and_fixed_policy() -> None:
    assert split_boundaries(2186) == (1311, 1748)
    assert split_boundaries(0) == (0, 0)
    with pytest.raises(ValidationError):
        HistoricalSplitPolicy(embargo_days=6)
    with pytest.raises(ValidationError):
        HistoricalSplitPolicy(train_fraction=Decimal("0.7"))
    assert PREREGISTERED_SPLIT_POLICY.embargo_days == 5


def test_readiness_uses_reconstruction_status_and_exact_floors() -> None:
    ready = HistoricalReadiness.from_counts(
        eligible_before_split=300,
        train_eligible=150,
        validation_eligible=40,
        holdout_eligible=40,
        train_positive=20,
        train_negative=20,
    )
    assert ready.status is ReadinessStatus.RECONSTRUCTION_READY
    assert ready.strict_pit_verified is False
    assert ready.unmet_requirements == ()
    short = HistoricalReadiness.from_counts(
        eligible_before_split=299,
        train_eligible=150,
        validation_eligible=40,
        holdout_eligible=40,
        train_positive=20,
        train_negative=20,
    )
    assert short.status is ReadinessStatus.INSUFFICIENT_RECONSTRUCTED_SAMPLE
    assert short.unmet_requirements == ("eligible_before_split 299 < 300",)
    with pytest.raises(ValidationError):
        HistoricalReadiness.model_validate(
            ready.model_dump() | {"status": "insufficient_reconstructed_sample"}
        )


def test_counts_partition_and_sample_eligibility_are_enforced() -> None:
    with pytest.raises(ValidationError):
        HistoricalDatasetCounts(
            requested=2,
            feature_available=1,
            feature_missing=0,
            feature_not_yet_available=0,
            feature_incompatible=0,
            label_available=2,
            label_missing=0,
            label_not_yet_available=0,
            label_incompatible=0,
            eligible_before_split=2,
            purged=0,
        )
    sample_key = historical_sample_key(
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        decision_at=decision_instant(_DAY),
    )
    features = _features()
    label = _label()
    row_id = historical_row_id(
        sample_key=sample_key,
        feature_hash=historical_feature_hash(
            sample_key=sample_key,
            decision_at=decision_instant(_DAY),
            segment_seed_start=date(2019, 1, 1),
            prefix_digest="a" * 64,
            features=features,
        ),
        features=features,
        label=label,
    )
    with pytest.raises(ValidationError):
        HistoricalSample(
            sample_key=sample_key,
            feature_hash=UUID(int=7),
            row_id=row_id,
            decision_at=decision_instant(_DAY),
            features=features,
            label=label,
            feature_state=FeatureState.MISSING,
            label_state=LabelState.AVAILABLE,
            split=SampleSplit.TRAIN,
            eligible=False,
        )
    with pytest.raises(ValidationError):
        HistoricalSample(
            sample_key=sample_key,
            feature_hash=UUID(int=7),
            row_id=row_id,
            decision_at=decision_instant(_DAY),
            features=features,
            label=label,
            feature_state=FeatureState.AVAILABLE,
            label_state=LabelState.AVAILABLE,
            split=SampleSplit.TRAIN,
            purged=True,
            eligible=True,
        )


def test_decision_request_rejects_inverted_oversized_and_future_cuts() -> None:
    with pytest.raises(ValidationError):
        HistoricalDecisionRequest(
            asset_id=_ASSET_ID,
            start_date=date(2020, 1, 2),
            end_date=date(2020, 1, 1),
            known_at=_RETRIEVED,
        )
    with pytest.raises(ValidationError):
        HistoricalDecisionRequest(
            asset_id=_ASSET_ID,
            start_date=date(2020, 1, 1),
            end_date=date(2026, 12, 31),
            known_at=_RETRIEVED,
        )
    with pytest.raises(ValidationError):
        HistoricalDecisionRequest(
            asset_id=_ASSET_ID,
            start_date=date(2020, 1, 1),
            end_date=date(2020, 1, 2),
            known_at=datetime(2020, 1, 2, 11, 0, tzinfo=UTC),
        )
    request = HistoricalDecisionRequest(
        asset_id=_ASSET_ID,
        start_date=date(2020, 1, 1),
        end_date=date(2020, 1, 2),
        known_at=_RETRIEVED,
    )
    assert request.decision_dates() == (date(2020, 1, 1), date(2020, 1, 2))


def test_purge_reason_enum_is_finite() -> None:
    assert {item.value for item in PurgeReason} == {"label_interval_overlap", "embargo"}
    assert SampleSplit.HOLDOUT.value == "holdout"
    assert DataFrequency.DAY_1.value == "day_1"
