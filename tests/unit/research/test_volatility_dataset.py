"""Focused checks for causal features, reserved-period isolation and folds."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.historical_archive import build_historical_archive
from investment_analyst.research.historical_models import (
    DECIMAL34,
    HistoricalArchive,
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)
from investment_analyst.research.volatility_dataset import (
    VolatilityDataset,
    VolatilityDatasetError,
    VolatilityInsufficientSampleError,
    _decision_row,
    _target,
    _window_feature,
    build_volatility_decisions,
    plan_folds,
)
from investment_analyst.research.volatility_export import build_protocol
from investment_analyst.research.volatility_models import (
    ARCHIVE_REQUEST_END,
    ARCHIVE_REQUEST_START,
    ASSET_ID,
    DECISION_START,
    EPSILON,
    HYPOTHESIS,
    PREREGISTERED_FOLDS,
    REFERENCE_MODELS,
    RESERVED_START_DATE,
    SOURCE_ID,
    STUDY_LIMITATIONS,
    TARGET_HORIZON,
    VolatilityCriteria,
    VolatilityHoldoutPolicy,
    VolatilityModelParameters,
    VolatilityProtocol,
    VolatilityResampling,
    decision_instant,
)

_RETRIEVED = datetime(2026, 11, 1, 2, 37, 32, tzinfo=UTC)
_SCOPE = HistoricalAssetIdentity(
    asset_id=ASSET_ID,
    source_id=SOURCE_ID,
    product_id="BTC-USD",
    asset_class=AssetClass.CRYPTO,
    quote_currency="USD",
    crypto_profile="bitcoin",
    exchange="COINBASE",
    granularity_seconds=86_400,
)
_REQUEST = HistoricalArchiveRequest(
    asset_id=ASSET_ID,
    start_date=ARCHIVE_REQUEST_START,
    end_date=ARCHIVE_REQUEST_END,
)


def _bar(start: datetime, open_value: Decimal, close: Decimal) -> HistoricalCandle:
    high = (max(open_value, close) * Decimal("1.01")).quantize(Decimal("0.01"))
    low = (min(open_value, close) * Decimal("0.99")).quantize(Decimal("0.01"))
    volume = Decimal("100")
    raw = (str(open_value), str(high), str(low), str(close), str(volume), "0")
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=_SCOPE.asset_id,
            source_id=_SCOPE.source_id,
            bar_start=start,
            open_value=open_value,
            high=high,
            low=low,
            close=close,
            volume=volume,
            raw_values=raw,
        ),
        bar_start=start,
        bar_end=start + timedelta(days=1),
        open=open_value,
        high=high,
        low=low,
        close=close,
        volume=volume,
        raw_values=raw,
        assumed_available_at=assumed_available_instant(start + timedelta(days=1)),
        retrieved_at=_RETRIEVED,
    )


def _build_archive(
    *,
    mutate_reserved: bool = False,
    present_days: int = 2557,
) -> HistoricalArchive:
    candles: list[HistoricalCandle] = []
    day = ARCHIVE_REQUEST_START
    with localcontext(DECIMAL34):
        for index in range(present_days):
            base = (Decimal(10000) * (Decimal("1.0005") ** index)).quantize(Decimal("0.01"))
            close = (base * (Decimal(1) + Decimal((index % 7) - 3) / Decimal(1000))).quantize(
                Decimal("0.01")
            )
            if mutate_reserved and day >= RESERVED_START_DATE:
                base *= Decimal(2)
                close *= Decimal(2)
            candles.append(_bar(bar_start_instant(day), base, close))
            day += timedelta(days=1)
    return build_historical_archive(
        _SCOPE,
        _REQUEST,
        request_urls=("https://example.invalid/candles",),
        retrieved_at=_RETRIEVED,
        candles=tuple(candles),
    )


def _protocol(**overrides: object) -> VolatilityProtocol:
    payload: dict[str, object] = {
        "asset_id": ASSET_ID,
        "source_id": SOURCE_ID,
        "archive_id": UUID("00000000-0000-4000-8000-000000000001"),
        "archive_semantic_sha256": "a" * 64,
        "archive_candles_sha256": "b" * 64,
        "archive_retrieved_at": _RETRIEVED,
        "archive_request_start": ARCHIVE_REQUEST_START,
        "archive_request_end": ARCHIVE_REQUEST_END,
        "study_cut_at": _RETRIEVED,
        "decision_start": DECISION_START,
        "decision_end": ARCHIVE_REQUEST_END - timedelta(days=6),
        "decision_count": 2186,
        "reserved_start_index": 1748,
        "reserved_start_date": RESERVED_START_DATE,
        "folds": PREREGISTERED_FOLDS,
        "feature_windows": (1, 7, 30),
        "reference_window": 5,
        "target_horizon": TARGET_HORIZON,
        "epsilon": EPSILON,
        "model": VolatilityModelParameters(),
        "references": REFERENCE_MODELS,
        "criteria": VolatilityCriteria(),
        "resampling": VolatilityResampling(),
        "holdout": VolatilityHoldoutPolicy(),
        "hypothesis": HYPOTHESIS,
        "limitations": STUDY_LIMITATIONS,
    }
    payload.update(overrides)
    return VolatilityProtocol(**payload)


def _close_maps(
    start: date,
    end: date,
) -> tuple[dict[date, Decimal], dict[date, UUID], dict[date, datetime]]:
    closes: dict[date, Decimal] = {}
    bar_ids: dict[date, UUID] = {}
    available: dict[date, datetime] = {}
    day = start
    offset = 0
    while day <= end:
        closes[day] = Decimal(100) + Decimal(offset)
        bar_ids[day] = UUID(int=offset + 1)
        available[day] = assumed_available_instant(bar_start_instant(day + timedelta(days=1)))
        day += timedelta(days=1)
        offset += 1
    return closes, bar_ids, available


def test_window_and_target_match_manual_decimal_formulas() -> None:
    closes, bar_ids, available = _close_maps(date(2019, 11, 1), date(2020, 1, 20))
    decision = date(2020, 1, 1)
    with localcontext(DECIMAL34):
        returns = {
            day: (closes[day] / closes[day - timedelta(days=1)]).ln()
            for day in closes
            if day > min(closes)
        }
        for window in (1, 5, 7, 30):
            expected = (
                sum(
                    (
                        returns[decision - timedelta(days=offset)] ** 2
                        for offset in range(window, 0, -1)
                    ),
                    Decimal(0),
                )
                / Decimal(window)
                + EPSILON
            ).ln()
            outcome = _window_feature(
                window=window,
                decision_date=decision,
                closes=closes,
                bar_ids=bar_ids,
                available=available,
            )
            assert not isinstance(outcome, str)
            assert outcome.value == expected
            assert outcome.available_at == datetime(2020, 1, 1, 1, 0, tzinfo=UTC)
            assert len(outcome.bar_content_ids) == window + 1
        expected_target = (
            sum(
                (
                    returns[decision + timedelta(days=offset)] ** 2
                    for offset in range(1, TARGET_HORIZON + 1)
                ),
                Decimal(0),
            )
            / Decimal(TARGET_HORIZON)
            + EPSILON
        ).ln()
        target = _target(decision_date=decision, closes=closes, bar_ids=bar_ids)
        assert not isinstance(target, str)
        assert target[0] == expected_target
        assert len(target[1]) == TARGET_HORIZON + 1


def test_missing_and_nonpositive_prices_are_excluded_without_fill() -> None:
    closes, bar_ids, available = _close_maps(date(2019, 11, 1), date(2020, 1, 20))
    decision = date(2020, 1, 1)
    missing = dict(closes)
    del missing[decision - timedelta(days=2)]
    outcome = _window_feature(
        window=1,
        decision_date=decision,
        closes=missing,
        bar_ids=bar_ids,
        available=available,
    )
    assert outcome == "missing_close"

    negative = dict(closes)
    negative[decision - timedelta(days=1)] = Decimal(0)
    outcome = _window_feature(
        window=1,
        decision_date=decision,
        closes=negative,
        bar_ids=bar_ids,
        available=available,
    )
    assert outcome == "nonpositive_close"

    target_missing = dict(closes)
    del target_missing[decision + timedelta(days=2)]
    target_negative = dict(closes)
    target_negative[decision + timedelta(days=2)] = Decimal(0)
    target = _target(decision_date=decision, closes=target_missing, bar_ids=bar_ids)
    assert target == "label_history_incomplete"
    assert _target(decision_date=decision, closes=target_negative, bar_ids=bar_ids) == (
        "label_nonpositive_close"
    )


def test_decision_row_excludes_reserved_labels_and_exposes_no_row_identity() -> None:
    protocol = _protocol()
    closes, bar_ids, available = _close_maps(date(2024, 8, 1), date(2024, 10, 20))
    index = (date(2024, 10, 3) - DECISION_START).days
    row = _decision_row(
        index,
        closes=closes,
        bar_ids=bar_ids,
        available=available,
        protocol=protocol,
    )
    assert row.decision_date == date(2024, 10, 3)
    assert row.features is not None
    assert row.target is None
    assert row.target_exclusion == "label_period_reserved"
    assert row.row_id is None
    assert row.label_maturity == datetime(2024, 10, 9, 1, 0, tzinfo=UTC)


def test_mutating_only_the_reserved_period_keeps_development_evidence_identical() -> None:
    baseline = _build_archive()
    mutated = _build_archive(mutate_reserved=True)
    assert baseline.manifest.semantic_sha256 != mutated.manifest.semantic_sha256
    baseline_protocol = build_protocol(baseline)
    mutated_protocol = build_protocol(mutated)
    baseline_dataset = build_volatility_decisions(baseline, baseline_protocol)
    mutated_dataset = build_volatility_decisions(mutated, mutated_protocol)

    assert baseline_dataset.counts == mutated_dataset.counts
    assert mutated_dataset.counts.reserved_target_constructions == 0
    for left, right in zip(baseline_dataset.decisions, mutated_dataset.decisions, strict=True):
        assert left.decision_date == right.decision_date
        assert left.features == right.features
        assert left.reference_5d == right.reference_5d
        assert left.target == right.target
        assert left.row_id == right.row_id
    assert baseline_protocol.archive_semantic_sha256 != mutated_protocol.archive_semantic_sha256


def test_fold_planning_applies_embargo_and_contiguous_segments() -> None:
    archive = _build_archive()
    protocol = build_protocol(archive)
    dataset = build_volatility_decisions(archive, protocol)
    plans = plan_folds(dataset, protocol)

    assert [(len(plan.train), len(plan.validation)) for plan in plans] == [
        (1082, 207),
        (1300, 207),
        (1518, 208),
    ]
    assert [plan.train_embargo_excluded for plan in plans] == [11, 11, 11]
    assert [plan.train_ineligible_excluded for plan in plans] == [0, 0, 0]
    assert [plan.validation_embargo_excluded for plan in plans] == [11, 11, 0]
    assert [plan.validation_ineligible_excluded for plan in plans] == [0, 0, 11]
    assert [tuple(len(segment) for segment in plan.segments) for plan in plans] == [
        (207,),
        (207,),
        (208,),
    ]
    assert plans[0].validation[0].decision_date == date(2022, 12, 29)
    assert plans[0].validation[-1].decision_date == date(2023, 7, 23)
    assert plans[2].validation[-1].decision_date == date(2024, 10, 2)


def test_insufficient_training_floor_fails_closed() -> None:
    archive = _build_archive()
    protocol = build_protocol(archive)
    dataset = build_volatility_decisions(archive, protocol)
    decisions = list(dataset.decisions)
    for index in range(100, len(decisions)):
        decisions[index] = replace(
            decisions[index],
            features=None,
            reference_5d=None,
            target=None,
            row_id=None,
        )
    trimmed = VolatilityDataset(
        protocol=dataset.protocol,
        decisions=tuple(decisions),
        counts=dataset.counts,
    )
    with pytest.raises(VolatilityInsufficientSampleError, match="training rows"):
        plan_folds(trimmed, protocol)


def test_archive_digest_mismatch_fails_closed() -> None:
    archive = _build_archive()
    protocol = build_protocol(archive)
    altered = protocol.model_copy(update={"archive_candles_sha256": "f" * 64})
    with pytest.raises(VolatilityDatasetError, match="candles digest"):
        build_volatility_decisions(archive, altered)


def test_sparse_archive_still_builds_an_honest_insufficient_dataset() -> None:
    archive = _build_archive(present_days=600)
    protocol = build_protocol(archive)
    dataset = build_volatility_decisions(archive, protocol)
    assert dataset.counts.candles_read == 600
    assert dataset.counts.feature_ready < dataset.counts.constructed_decisions
    assert dataset.counts.target_ready <= dataset.counts.feature_ready
    with pytest.raises(VolatilityInsufficientSampleError):
        plan_folds(dataset, protocol)
    assert dataset.decisions[0].decision_at == decision_instant(date(2020, 1, 1))
