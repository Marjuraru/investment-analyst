"""Deterministic reconstructed dataset with causal features and an exact forward label."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import localcontext
from uuid import UUID

from investment_analyst.research.historical_features import (
    HistoricalFeaturePoint,
    HistoricalFeatureSeries,
    reconstruct_feature_series,
)
from investment_analyst.research.historical_models import (
    DATASET_LIMITATIONS,
    DECIMAL34,
    LABEL_BAR_COUNT,
    LABEL_EXIT_OFFSET_DAYS,
    PREREGISTERED_SPLIT_POLICY,
    AvailabilityBasis,
    EvidenceReason,
    FeatureState,
    HistoricalArchive,
    HistoricalArchiveReference,
    HistoricalArtifactSchema,
    HistoricalDataset,
    HistoricalDatasetCounts,
    HistoricalDatasetManifest,
    HistoricalDatasetSummary,
    HistoricalDecisionRequest,
    HistoricalFeatureEvidence,
    HistoricalLabelBarEvidence,
    HistoricalLabelEvidence,
    HistoricalReadiness,
    HistoricalSample,
    HistoricalSplitPolicy,
    HistoricalSplitSummary,
    LabelState,
    PurgeReason,
    ReasonCode,
    SampleSplit,
    aggregate_feature_state,
    bar_start_instant,
    decision_instant,
    historical_dataset_content_hash,
    historical_dataset_id,
    historical_feature_hash,
    historical_row_id,
    historical_sample_key,
    label_exit_bar_end,
    split_boundaries,
)


class HistoricalDatasetError(RuntimeError):
    """Base error for reconstructed dataset construction."""


class HistoricalDatasetRequestError(HistoricalDatasetError, ValueError):
    """The request cannot be satisfied by the sealed archive."""


class HistoricalDatasetIntegrityError(HistoricalDatasetError):
    """The archive or reconstruction contradicts the declared contract."""


def _reason(code: ReasonCode, detail: str) -> EvidenceReason:
    return EvidenceReason(code=code, detail=detail)


def _validate_request(request: HistoricalDecisionRequest, archive: HistoricalArchive) -> None:
    manifest = archive.manifest
    if request.asset_id != manifest.asset.asset_id:
        raise HistoricalDatasetRequestError("request asset does not match the sealed archive")
    if request.start_date < manifest.request.start_date:
        raise HistoricalDatasetRequestError("requested decisions start before the sealed archive")
    if request.end_date > manifest.request.end_date:
        raise HistoricalDatasetRequestError("requested decisions end after the sealed archive")
    if request.end_date + timedelta(days=LABEL_EXIT_OFFSET_DAYS) > manifest.request.end_date:
        raise HistoricalDatasetRequestError(
            "the forward label window exceeds the sealed archive range"
        )
    if request.known_at < manifest.retrieved_at:
        raise HistoricalDatasetRequestError("known_at must not precede the real archive reception")


def _label_bar(
    point: HistoricalFeaturePoint | None,
    *,
    day: date,
) -> HistoricalLabelBarEvidence:
    start = bar_start_instant(day)
    end = start + timedelta(days=1)
    if point is None:
        return HistoricalLabelBarEvidence(
            bar_start=start,
            bar_end=end,
            state=FeatureState.MISSING,
            reasons=(_reason(ReasonCode.LABEL_BAR_ABSENT, f"no bar exists for {day.isoformat()}"),),
        )
    candle = point.candle
    if not candle.close.is_finite() or candle.close <= 0:
        return HistoricalLabelBarEvidence(
            bar_content_id=candle.bar_content_id,
            bar_start=start,
            bar_end=end,
            state=FeatureState.INCOMPATIBLE,
            assumed_available_at=candle.assumed_available_at,
            reasons=(
                _reason(
                    ReasonCode.LABEL_BAR_INVALID,
                    f"bar {day.isoformat()} lacks a strictly positive finite close",
                ),
            ),
        )
    return HistoricalLabelBarEvidence(
        bar_content_id=candle.bar_content_id,
        bar_start=start,
        bar_end=end,
        state=FeatureState.AVAILABLE,
        value=candle.close,
        assumed_available_at=candle.assumed_available_at,
    )


def _label(
    series: HistoricalFeatureSeries,
    *,
    decision_date: date,
    known_at: datetime,
    archive_retrieved_at: datetime,
) -> HistoricalLabelEvidence:
    bars = tuple(
        _label_bar(
            series.point(decision_date + timedelta(days=offset)),
            day=decision_date + timedelta(days=offset),
        )
        for offset in range(LABEL_BAR_COUNT)
    )
    exit_end = label_exit_bar_end(decision_date)
    reasons: list[EvidenceReason] = []
    if exit_end > known_at:
        state = LabelState.NOT_YET_AVAILABLE
        reasons.append(
            _reason(
                ReasonCode.LABEL_NOT_MATURED,
                f"exit bar ends at {exit_end.isoformat()} after known_at",
            )
        )
    elif any(bar.state is FeatureState.INCOMPATIBLE for bar in bars):
        state = LabelState.INCOMPATIBLE
        reasons.extend(reason for bar in bars for reason in bar.reasons)
    elif any(bar.state is FeatureState.MISSING for bar in bars):
        state = LabelState.MISSING
        reasons.extend(reason for bar in bars for reason in bar.reasons)
    else:
        state = LabelState.AVAILABLE
    if state is not LabelState.AVAILABLE:
        return HistoricalLabelEvidence(
            state=state,
            decision_at=decision_instant(decision_date),
            entry_bar_start=bar_start_instant(decision_date),
            exit_bar_end=exit_end,
            archive_retrieved_at=archive_retrieved_at,
            bars=bars,
            reasons=tuple(reasons),
        )
    entry = bars[0].value
    exit_value = bars[-1].value
    if entry is None or exit_value is None:
        raise HistoricalDatasetIntegrityError("available label lacks an entry or exit close")
    with localcontext(DECIMAL34):
        return_5d = exit_value / entry - 1
    return HistoricalLabelEvidence(
        state=LabelState.AVAILABLE,
        decision_at=decision_instant(decision_date),
        entry_bar_start=bar_start_instant(decision_date),
        exit_bar_end=exit_end,
        entry_close=entry,
        exit_close=exit_value,
        return_5d=return_5d,
        label=1 if return_5d > 0 else 0,
        evidence_available_at=max(exit_end, archive_retrieved_at),
        archive_retrieved_at=archive_retrieved_at,
        bars=bars,
    )


def _purge_reason(
    decision_date: date,
    *,
    split: SampleSplit,
    boundary_date: date | None,
    embargo_days: int,
) -> PurgeReason | None:
    if boundary_date is None or split is SampleSplit.HOLDOUT:
        return None
    boundary = bar_start_instant(boundary_date)
    exit_end = label_exit_bar_end(decision_date)
    if exit_end >= boundary:
        return PurgeReason.LABEL_INTERVAL_OVERLAP
    if exit_end >= boundary - timedelta(days=embargo_days):
        return PurgeReason.EMBARGO
    return None


def _assign_splits(
    drafts: tuple[_Draft, ...],
    *,
    policy: HistoricalSplitPolicy,
) -> tuple[HistoricalSample, ...]:
    decision_dates = tuple(item.decision_date for item in drafts)
    train_end, validation_end = split_boundaries(len(decision_dates))
    samples: list[HistoricalSample] = []
    for index, draft in enumerate(drafts):
        if index < train_end:
            split = SampleSplit.TRAIN
            boundary_date = decision_dates[train_end] if train_end < len(decision_dates) else None
        elif index < validation_end:
            split = SampleSplit.VALIDATION
            boundary_date = (
                decision_dates[validation_end] if validation_end < len(decision_dates) else None
            )
        else:
            split = SampleSplit.HOLDOUT
            boundary_date = None
        purge_reason = _purge_reason(
            draft.decision_date,
            split=split,
            boundary_date=boundary_date,
            embargo_days=policy.embargo_days,
        )
        feature_available = draft.feature_state is FeatureState.AVAILABLE
        label_available = draft.label_state is LabelState.AVAILABLE
        samples.append(
            HistoricalSample(
                sample_key=draft.sample_key,
                feature_hash=draft.feature_hash,
                row_id=draft.row_id,
                decision_at=draft.decision_at,
                features=draft.features,
                label=draft.label,
                feature_state=draft.feature_state,
                label_state=draft.label_state,
                split=split,
                purged=purge_reason is not None,
                purge_reason=purge_reason,
                eligible=feature_available and label_available and purge_reason is None,
            )
        )
    return tuple(samples)


def _counts(samples: tuple[HistoricalSample, ...]) -> HistoricalDatasetCounts:
    def feature_count(state: FeatureState) -> int:
        return sum(1 for item in samples if item.feature_state is state)

    def label_count(state: LabelState) -> int:
        return sum(1 for item in samples if item.label_state is state)

    return HistoricalDatasetCounts(
        requested=len(samples),
        feature_available=feature_count(FeatureState.AVAILABLE),
        feature_missing=feature_count(FeatureState.MISSING),
        feature_not_yet_available=feature_count(FeatureState.NOT_YET_AVAILABLE),
        feature_incompatible=feature_count(FeatureState.INCOMPATIBLE),
        label_available=label_count(LabelState.AVAILABLE),
        label_missing=label_count(LabelState.MISSING),
        label_not_yet_available=label_count(LabelState.NOT_YET_AVAILABLE),
        label_incompatible=label_count(LabelState.INCOMPATIBLE),
        eligible_before_split=sum(
            1
            for item in samples
            if item.feature_state is FeatureState.AVAILABLE
            and item.label_state is LabelState.AVAILABLE
        ),
        purged=sum(1 for item in samples if item.purged),
    )


def _split_summaries(
    samples: tuple[HistoricalSample, ...],
) -> tuple[HistoricalSplitSummary, ...]:
    summaries: list[HistoricalSplitSummary] = []
    for split in (SampleSplit.TRAIN, SampleSplit.VALIDATION, SampleSplit.HOLDOUT):
        assigned = tuple(item for item in samples if item.split is split)
        purged = sum(1 for item in assigned if item.purged)
        remaining = len(assigned) - purged
        eligible = sum(1 for item in assigned if item.eligible)
        if split is SampleSplit.TRAIN:
            positive = sum(1 for item in assigned if item.eligible and item.label.label == 1)
            negative = sum(1 for item in assigned if item.eligible and item.label.label == 0)
        else:
            positive = None
            negative = None
        summaries.append(
            HistoricalSplitSummary(
                split=split,
                proposed=len(assigned),
                purged=purged,
                remaining=remaining,
                eligible_after_purge=eligible,
                positive=positive,
                negative=negative,
            )
        )
    return tuple(summaries)


@dataclass(frozen=True, slots=True)
class _Draft:
    decision_date: date
    decision_at: datetime
    sample_key: UUID
    feature_hash: UUID
    row_id: UUID
    features: tuple[HistoricalFeatureEvidence, ...]
    label: HistoricalLabelEvidence
    feature_state: FeatureState
    label_state: LabelState


def build_historical_dataset(
    request: HistoricalDecisionRequest,
    archive: HistoricalArchive,
    *,
    split_policy: HistoricalSplitPolicy = PREREGISTERED_SPLIT_POLICY,
) -> HistoricalDataset:
    """Reconstruct one deterministic dataset from a sealed archive without network access."""
    _validate_request(request, archive)
    series = reconstruct_feature_series(archive.candles)
    asset = archive.manifest.asset
    retrieved_at = archive.manifest.retrieved_at
    drafts: list[_Draft] = []
    for decision_date in request.decision_dates():
        features = series.features(decision_date)
        label = _label(
            series,
            decision_date=decision_date,
            known_at=request.known_at,
            archive_retrieved_at=retrieved_at,
        )
        decision_at = decision_instant(decision_date)
        sample_key = historical_sample_key(
            asset_id=asset.asset_id,
            source_id=asset.source_id,
            decision_at=decision_at,
        )
        segment_seed_start, prefix_digest = series.feature_identity_inputs(decision_date)
        feature_hash = historical_feature_hash(
            sample_key=sample_key,
            decision_at=decision_at,
            segment_seed_start=segment_seed_start,
            prefix_digest=prefix_digest,
            features=features,
        )
        row_id = historical_row_id(
            sample_key=sample_key,
            feature_hash=feature_hash,
            features=features,
            label=label,
        )
        drafts.append(
            _Draft(
                decision_date=decision_date,
                decision_at=decision_at,
                sample_key=sample_key,
                feature_hash=feature_hash,
                row_id=row_id,
                features=features,
                label=label,
                feature_state=aggregate_feature_state(features),
                label_state=label.state,
            )
        )
    samples = _assign_splits(tuple(drafts), policy=split_policy)
    counts = _counts(samples)
    splits = _split_summaries(samples)
    readiness = HistoricalReadiness.from_counts(
        eligible_before_split=counts.eligible_before_split,
        train_eligible=splits[0].eligible_after_purge,
        validation_eligible=splits[1].eligible_after_purge,
        holdout_eligible=splits[2].eligible_after_purge,
        train_positive=splits[0].positive or 0,
        train_negative=splits[0].negative or 0,
    )
    archive_reference = HistoricalArchiveReference(
        archive_id=archive.archive_id,
        semantic_sha256=archive.manifest.semantic_sha256,
        candles_sha256=archive.manifest.candles_sha256,
        retrieved_at=retrieved_at,
        requested_start=archive.manifest.requested_start,
        requested_end=archive.manifest.requested_end,
        bar_count=archive.manifest.counts.bar_count,
    )
    dataset_id = historical_dataset_id(
        asset_id=asset.asset_id,
        source_id=asset.source_id,
        archive_id=archive.archive_id,
        archive_semantic_sha256=archive.manifest.semantic_sha256,
        request=request,
        split_policy=split_policy,
        rows=samples,
    )
    content_hash = historical_dataset_content_hash(dataset_id=dataset_id, rows=samples)
    limitations = DATASET_LIMITATIONS + (
        "La reconstruccion reutiliza primitivas decimales vigentes y reinicia el"
        " calentamiento de RSI y MACD en cada segmento diario contiguo.",
        "El archivo fuente unico se conserva aparte y se referencia por identidad;"
        " no se copia dentro del dataset.",
    )
    manifest = HistoricalDatasetManifest(
        dataset_id=dataset_id,
        dataset_content_sha256=content_hash,
        request=request,
        asset=asset,
        archive=archive_reference,
        availability_basis=AvailabilityBasis.HISTORICAL_RECONSTRUCTION_ASSUMED,
        feature_read_start=bar_start_instant(request.start_date + timedelta(days=-2)),
        feature_read_end=bar_start_instant(request.end_date),
        label_read_start=bar_start_instant(request.start_date),
        label_read_end=bar_start_instant(
            request.end_date + timedelta(days=LABEL_EXIT_OFFSET_DAYS + 1)
        ),
        artifact_schema=HistoricalArtifactSchema(),
        counts=counts,
        split_policy=split_policy,
        splits=splits,
        readiness=readiness,
        rows=samples,
        limitations=limitations,
    )
    summary = HistoricalDatasetSummary(
        dataset_id=dataset_id,
        dataset_content_sha256=content_hash,
        experiment_spec_id=manifest.experiment_spec_id,
        target_version=manifest.target_version,
        feature_set_version=manifest.feature_set_version,
        availability_policy=manifest.availability_policy,
        availability_basis=manifest.availability_basis,
        strict_pit_verified=False,
        asset=asset,
        archive=archive_reference,
        request=request,
        counts=counts,
        split_policy=split_policy,
        splits=splits,
        readiness=readiness,
        limitations=limitations,
    )
    return HistoricalDataset(
        dataset_id=dataset_id,
        dataset_content_sha256=content_hash,
        manifest=manifest,
        summary=summary,
        samples=samples,
    )


__all__ = [
    "HistoricalDatasetError",
    "HistoricalDatasetIntegrityError",
    "HistoricalDatasetRequestError",
    "build_historical_dataset",
]
