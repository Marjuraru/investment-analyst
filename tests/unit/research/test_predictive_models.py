"""Strict predictive contracts, identities, splits and readiness rules."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from investment_analyst.core.models.enums import DataQuality
from investment_analyst.research.predictive_models import (
    CLOSE_RETURN_ALGORITHM_VERSION,
    CLOSE_RETURN_COLUMN,
    DECIMAL34,
    FEATURE_COLUMNS,
    FEATURE_RATIO_UNIT,
    MACD_HISTOGRAM_COLUMN,
    PREREGISTERED_EXPERIMENT,
    PREREGISTERED_SPLIT_POLICY,
    RSI_COLUMN,
    EvidenceReason,
    FeatureState,
    LabelState,
    PredictiveDatasetCounts,
    PredictiveDatasetRequest,
    PredictiveFeatureEvidence,
    PredictiveLabelBarEvidence,
    PredictiveLabelEvidence,
    PredictiveReadiness,
    PredictiveSample,
    PurgeReason,
    ReadinessStatus,
    ReasonCode,
    SampleSplit,
    bar_start_instant,
    decision_instant,
    label_exit_bar_end,
    predictive_dataset_content_hash,
    predictive_dataset_id,
    predictive_feature_hash,
    predictive_row_id,
    predictive_sample_key,
    split_boundaries,
)

_DECISION = date(2026, 8, 10)
_DECISION_AT = decision_instant(_DECISION)
_AVAILABLE_AT = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
_WORKSPACE_UUID = UUID("70c64668-1738-4dfe-ba1f-7e72026bb7fe")
_ASSET_ID = "crypto:btc-usd"
_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"


def _metric_id(seed: str) -> UUID:
    return uuid5(UUID("00000000-0000-0000-0000-000000000000"), seed)


def _feature(
    column: str, value: Decimal, *, computed_at: datetime | None = None
) -> PredictiveFeatureEvidence:
    spec = next(
        item for item in PREREGISTERED_EXPERIMENT.feature_columns if item.column_name == column
    )
    return PredictiveFeatureEvidence(
        column_name=column,
        state=FeatureState.AVAILABLE,
        value=value,
        metric_result_id=None if spec.metric_key is None else _metric_id(column),
        as_of=bar_start_instant(_DECISION - timedelta(days=1)),
        available_at=_AVAILABLE_AT,
        computed_at=computed_at,
        algorithm_version=(
            CLOSE_RETURN_ALGORITHM_VERSION if spec.metric_key is None else spec.algorithm_version
        ),
        unit=FEATURE_RATIO_UNIT,
        quality=DataQuality.VALID,
    )


def _features(*, computed_at: datetime | None = None) -> tuple[PredictiveFeatureEvidence, ...]:
    return (
        _feature(CLOSE_RETURN_COLUMN, Decimal("0.05"), computed_at=computed_at),
        _feature(RSI_COLUMN, Decimal("0.55"), computed_at=computed_at),
        _feature(MACD_HISTOGRAM_COLUMN, Decimal("0.02"), computed_at=computed_at),
    )


def _label(
    *, entry: Decimal = Decimal("100"), exit_close: Decimal = Decimal("105")
) -> PredictiveLabelEvidence:
    values = (
        entry,
        Decimal("101"),
        Decimal("102"),
        Decimal("103"),
        Decimal("104"),
        exit_close,
    )
    bars = []
    for offset, value in enumerate(values):
        start = bar_start_instant(_DECISION + timedelta(days=offset))
        bars.append(
            PredictiveLabelBarEvidence(
                bar_start=start,
                bar_end=start + timedelta(days=1),
                state=FeatureState.AVAILABLE,
                observation_id=_metric_id(f"bar-{offset}"),
                available_at=start + timedelta(days=1, hours=1),
                value=value,
            )
        )
    with localcontext(DECIMAL34):
        return_5d = exit_close / entry - 1
    exit_end = label_exit_bar_end(_DECISION)
    available_at = max([bar.available_at for bar in bars] + [exit_end])
    return PredictiveLabelEvidence(
        state=LabelState.AVAILABLE,
        decision_at=_DECISION_AT,
        entry_bar_start=bar_start_instant(_DECISION),
        exit_bar_end=exit_end,
        entry_close=entry,
        exit_close=exit_close,
        return_5d=return_5d,
        label=1 if return_5d > 0 else 0,
        label_available_at=available_at,
        bars=tuple(bars),
    )


def _sample(
    *,
    label: PredictiveLabelEvidence | None = None,
    computed_at: datetime | None = None,
) -> PredictiveSample:
    features = _features(computed_at=computed_at)
    resolved_label = label if label is not None else _label()
    sample_key = predictive_sample_key(
        experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        decision_at=_DECISION_AT,
    )
    feature_hash = predictive_feature_hash(
        sample_key=sample_key,
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        decision_at=_DECISION_AT,
        features=features,
    )
    row_id = predictive_row_id(
        sample_key=sample_key,
        feature_hash=feature_hash,
        features=features,
        label=resolved_label,
    )
    return PredictiveSample(
        sample_key=sample_key,
        feature_hash=feature_hash,
        row_id=row_id,
        decision_at=_DECISION_AT,
        features=features,
        label=resolved_label,
        feature_state=FeatureState.AVAILABLE,
        label_state=LabelState.AVAILABLE,
        split=SampleSplit.TRAIN,
        eligible=True,
    )


def test_preregistered_experiment_and_request_are_strict() -> None:
    assert PREREGISTERED_EXPERIMENT.experiment_spec_id == "crypto-daily-positive-return-5d-v1"
    assert PREREGISTERED_EXPERIMENT.target_version == "crypto-forward-close-return-5d-v1"
    assert PREREGISTERED_EXPERIMENT.feature_set_version == "crypto-market-baseline-3-v1"
    assert tuple(item.column_name for item in PREREGISTERED_EXPERIMENT.feature_columns) == (
        FEATURE_COLUMNS
    )
    assert PREREGISTERED_SPLIT_POLICY.embargo_days == 5

    leap_request = PredictiveDatasetRequest(
        asset_id=_ASSET_ID,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        dataset_known_at=datetime(2024, 12, 31, 23, 0, tzinfo=UTC),
    )
    assert len(leap_request.decision_dates()) == 366
    assert date(2024, 2, 29) in leap_request.decision_dates()

    with pytest.raises(ValidationError, match="limited to 366 dates"):
        PredictiveDatasetRequest(
            asset_id=_ASSET_ID,
            start_date=date(2024, 1, 1),
            end_date=date(2025, 1, 1),
            dataset_known_at=datetime(2025, 1, 1, 23, 0, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="must not be later"):
        PredictiveDatasetRequest(
            asset_id=_ASSET_ID,
            start_date=date(2026, 8, 11),
            end_date=date(2026, 8, 10),
            dataset_known_at=datetime(2026, 8, 12, 0, 0, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="must not precede"):
        PredictiveDatasetRequest(
            asset_id=_ASSET_ID,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            dataset_known_at=datetime(2026, 8, 10, 11, 59, tzinfo=UTC),
        )
    with pytest.raises(ValidationError, match="timezone"):
        PredictiveDatasetRequest(
            asset_id=_ASSET_ID,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 10),
            dataset_known_at=datetime(2026, 8, 11, 0, 0),
        )


def test_feature_hash_ignores_label_and_execution_clock() -> None:
    first = _sample(computed_at=datetime(2026, 8, 10, 13, 0, tzinfo=UTC))
    second = _sample(computed_at=datetime(2026, 8, 10, 18, 0, tzinfo=UTC))
    changed_label = _sample(label=_label(exit_close=Decimal("106")))

    assert first.feature_hash == second.feature_hash
    assert first.row_id == second.row_id
    assert first.feature_hash == changed_label.feature_hash
    assert first.row_id != changed_label.row_id
    assert predictive_dataset_content_hash(
        dataset_id=first.sample_key,
        rows=(first,),
    ) == predictive_dataset_content_hash(
        dataset_id=second.sample_key,
        rows=(second,),
    )


def test_label_rule_treats_zero_return_as_negative_class() -> None:
    zero = _label(entry=Decimal("100"), exit_close=Decimal("100"))
    positive = _label(entry=Decimal("100"), exit_close=Decimal("100.0001"))
    negative = _label(entry=Decimal("100"), exit_close=Decimal("99.9999"))
    assert zero.return_5d == Decimal("0")
    assert zero.label == 0
    assert positive.label == 1
    assert negative.label == 0


def test_financial_values_reject_float_and_non_finite() -> None:
    with pytest.raises(ValidationError, match="must use Decimal"):
        PredictiveFeatureEvidence(
            column_name=RSI_COLUMN,
            state=FeatureState.AVAILABLE,
            value=0.5,
            as_of=_AVAILABLE_AT,
            available_at=_AVAILABLE_AT,
            algorithm_version="market-rsi-incremental-v2-decimal34",
            unit="index",
        )
    with pytest.raises(ValidationError, match="finite"):
        _feature(RSI_COLUMN, Decimal("NaN"))


def test_split_boundaries_use_floors_over_requested_dates() -> None:
    assert split_boundaries(90) == (54, 72)
    assert split_boundaries(1) == (0, 0)
    assert split_boundaries(2) == (1, 1)
    assert split_boundaries(3) == (1, 2)
    assert split_boundaries(7) == (4, 5)


def test_purge_and_embargo_never_move_split_boundaries() -> None:
    from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder

    boundary = date(2026, 9, 26)
    overlap = boundary - timedelta(days=6)
    embargo = boundary - timedelta(days=7)
    clear = boundary - timedelta(days=12)
    assert (
        PredictiveDatasetBuilder._purge_reason(
            overlap,
            split=SampleSplit.TRAIN,
            boundary_date=boundary,
        )
        is PurgeReason.LABEL_INTERVAL_OVERLAP
    )
    assert (
        PredictiveDatasetBuilder._purge_reason(
            embargo,
            split=SampleSplit.TRAIN,
            boundary_date=boundary,
        )
        is PurgeReason.EMBARGO
    )
    assert (
        PredictiveDatasetBuilder._purge_reason(
            clear,
            split=SampleSplit.TRAIN,
            boundary_date=boundary,
        )
        is None
    )
    assert (
        PredictiveDatasetBuilder._purge_reason(
            overlap,
            split=SampleSplit.HOLDOUT,
            boundary_date=None,
        )
        is None
    )


def test_readiness_requires_every_preregistered_floor() -> None:
    insufficient = PredictiveReadiness.from_counts(
        eligible_before_split=299,
        train_eligible=150,
        validation_eligible=40,
        holdout_eligible=40,
        train_positive=20,
        train_negative=20,
    )
    assert insufficient.status is ReadinessStatus.INSUFFICIENT_PIT_SAMPLE
    assert "eligible_before_split 299 < 300" in insufficient.unmet_requirements

    ready = PredictiveReadiness.from_counts(
        eligible_before_split=300,
        train_eligible=150,
        validation_eligible=40,
        holdout_eligible=40,
        train_positive=20,
        train_negative=20,
    )
    assert ready.status is ReadinessStatus.RESEARCH_READY
    assert ready.unmet_requirements == ()

    with pytest.raises(ValidationError, match="status must follow"):
        PredictiveReadiness(
            status=ReadinessStatus.RESEARCH_READY,
            eligible_before_split=1,
            train_eligible=0,
            validation_eligible=0,
            holdout_eligible=0,
            train_positive=0,
            train_negative=0,
        )


def test_counts_partition_every_requested_decision() -> None:
    counts = PredictiveDatasetCounts(
        requested=2,
        feature_available=1,
        feature_missing=1,
        feature_not_yet_available=0,
        feature_incompatible=0,
        feature_ambiguous=0,
        label_available=1,
        label_missing=0,
        label_not_yet_available=1,
        label_incompatible=0,
        label_ambiguous=0,
        eligible_before_split=1,
        purged=0,
    )
    assert counts.requested == 2
    with pytest.raises(ValidationError, match="feature state counts"):
        PredictiveDatasetCounts(
            requested=2,
            feature_available=1,
            feature_missing=0,
            feature_not_yet_available=0,
            feature_incompatible=0,
            feature_ambiguous=0,
            label_available=2,
            label_missing=0,
            label_not_yet_available=0,
            label_incompatible=0,
            label_ambiguous=0,
            eligible_before_split=1,
            purged=0,
        )


def test_label_requires_continuous_six_bar_calendar() -> None:
    label = _label()
    shifted = label.bars[3].model_copy(
        update={
            "bar_start": label.bars[3].bar_start + timedelta(days=1),
            "bar_end": label.bars[3].bar_end + timedelta(days=1),
        }
    )
    bars = (*label.bars[:3], shifted, *label.bars[4:])
    with pytest.raises(ValidationError, match="continuous"):
        PredictiveLabelEvidence(
            state=label.state,
            decision_at=label.decision_at,
            entry_bar_start=label.entry_bar_start,
            exit_bar_end=label.exit_bar_end,
            entry_close=label.entry_close,
            exit_close=label.exit_close,
            return_5d=label.return_5d,
            label=label.label,
            label_available_at=label.label_available_at,
            bars=bars,
        )


def test_unavailable_evidence_requires_finite_reasons() -> None:
    with pytest.raises(ValidationError, match="at least one reason"):
        PredictiveFeatureEvidence(
            column_name=RSI_COLUMN,
            state=FeatureState.MISSING,
            algorithm_version="market-rsi-incremental-v2-decimal34",
            unit="index",
        )
    evidence = PredictiveFeatureEvidence(
        column_name=RSI_COLUMN,
        state=FeatureState.NOT_YET_AVAILABLE,
        algorithm_version="market-rsi-incremental-v2-decimal34",
        unit="index",
        reasons=(
            EvidenceReason(
                code=ReasonCode.METRIC_NOT_YET_AVAILABLE,
                detail="available after decision_at",
            ),
        ),
    )
    assert evidence.value is None


def test_dataset_identity_binds_request_policy_and_rows() -> None:
    sample = _sample()
    request = PredictiveDatasetRequest(
        asset_id=_ASSET_ID,
        start_date=_DECISION,
        end_date=_DECISION,
        dataset_known_at=datetime(2026, 8, 16, 0, 0, tzinfo=UTC),
    )
    first = predictive_dataset_id(
        experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
        workspace_uuid=_WORKSPACE_UUID,
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        request=request,
        split_policy=PREREGISTERED_SPLIT_POLICY,
        rows=(sample,),
    )
    second = predictive_dataset_id(
        experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
        workspace_uuid=_WORKSPACE_UUID,
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        request=request,
        split_policy=PREREGISTERED_SPLIT_POLICY,
        rows=(sample,),
    )
    other_workspace = predictive_dataset_id(
        experiment_spec_id=PREREGISTERED_EXPERIMENT.experiment_spec_id,
        workspace_uuid=UUID("00000000-0000-0000-0000-000000000001"),
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        request=request,
        split_policy=PREREGISTERED_SPLIT_POLICY,
        rows=(sample,),
    )
    assert first == second
    assert first != other_workspace
