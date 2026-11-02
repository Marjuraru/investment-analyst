"""Causal funding features and the deterministic research complement bundle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from investment_analyst.research.funding_models import (
    DECIMAL34,
    FUNDING_AVAILABILITY_POLICY,
    FUNDING_COLUMN_WINDOWS,
    FUNDING_COLUMNS,
    FUNDING_FEATURE_SET_VERSION,
    FUNDING_LIMITATIONS,
    FundingArchive,
    FundingAssetIdentity,
    FundingBaseDatasetReference,
    FundingBundle,
    FundingBundleSummary,
    FundingFeatureEvidence,
    FundingFeatureManifest,
    FundingFeatureRequest,
    FundingPoint,
    FundingReason,
    FundingReasonCode,
    FundingSample,
    aggregate_funding_state,
    assumed_funding_available_instant,
    decision_instant,
    funding_admission,
    funding_anchor_instant,
    funding_archive_reference,
    funding_bundle_content_hash,
    funding_bundle_id,
    funding_bundle_summary,
    funding_counts,
    funding_feature_hash,
    funding_gap_counts,
    funding_input_digest,
    funding_split_coverage,
    funding_window_instants,
)
from investment_analyst.research.historical_models import FeatureState, SampleSplit


class FundingFeatureError(RuntimeError):
    """Base error for causal funding feature construction."""


class FundingFeatureRequestError(FundingFeatureError, ValueError):
    """The base dataset and archive cannot satisfy the requested complement."""


class FundingFeatureIntegrityError(FundingFeatureError):
    """The archive or decision grid contradicts the declared contract."""


@dataclass(frozen=True, slots=True)
class FundingBaseDecision:
    """The original decision metadata required by the funding complement.

    Only identity, eligibility and split metadata are retained. Base features,
    labels and prices remain in the original dataset and are never copied.
    """

    sample_key: UUID
    decision_at: datetime
    eligible: bool
    split: SampleSplit
    purged: bool


def _reason(code: FundingReasonCode, detail: str) -> FundingReason:
    return FundingReason(code=code, detail=detail)


def _validate_base(
    *,
    base_dataset: FundingBaseDatasetReference,
    decisions: tuple[FundingBaseDecision, ...],
    funding_asset: FundingAssetIdentity,
    archive: FundingArchive,
    known_at: datetime,
) -> None:
    if not decisions:
        raise FundingFeatureRequestError("a funding complement requires at least one decision")
    if len(decisions) != base_dataset.row_count:
        raise FundingFeatureRequestError("base decisions must match the dataset row count")
    if base_dataset.asset_id != funding_asset.asset_id:
        raise FundingFeatureRequestError("base dataset and funding asset differ")
    if archive.manifest.asset != funding_asset:
        raise FundingFeatureRequestError(
            "archive funding identity differs from the bundle identity"
        )
    if base_dataset.start_date != decisions[0].decision_at.date():
        raise FundingFeatureRequestError("base dataset start does not match its decisions")
    if base_dataset.end_date != decisions[-1].decision_at.date():
        raise FundingFeatureRequestError("base dataset end does not match its decisions")
    if known_at < base_dataset.known_at:
        raise FundingFeatureRequestError("known_at cannot precede the base dataset cut")
    if known_at < archive.manifest.retrieved_at:
        raise FundingFeatureRequestError("known_at cannot precede the funding archive reception")
    sample_keys = tuple(item.sample_key for item in decisions)
    if len(set(sample_keys)) != len(sample_keys):
        raise FundingFeatureRequestError("base decisions must not repeat a sample identity")
    decision_times = tuple(item.decision_at for item in decisions)
    if decision_times != tuple(sorted(decision_times)):
        raise FundingFeatureRequestError("base decisions must be ordered by decision_at")
    if any(
        current - previous != timedelta(days=1)
        for previous, current in zip(decision_times, decision_times[1:], strict=False)
    ):
        raise FundingFeatureRequestError("base decisions must follow a contiguous daily grid")
    if any(item.decision_at != decision_instant(item.decision_at.date()) for item in decisions):
        raise FundingFeatureRequestError("base decisions must use the 12:00 UTC instant")
    request = archive.manifest.request
    if request.decision_start_date != base_dataset.start_date:
        raise FundingFeatureRequestError("archive request start differs from the base dataset")
    if request.decision_end_date != base_dataset.end_date:
        raise FundingFeatureRequestError("archive request end differs from the base dataset")
    if request.decision_count != len(decisions):
        raise FundingFeatureRequestError("archive request decision count differs from the base")


def _points_by_timestamp(archive: FundingArchive) -> dict[datetime, FundingPoint]:
    points: dict[datetime, FundingPoint] = {}
    for point in archive.points:
        if point.timestamp in points:
            raise FundingFeatureIntegrityError("archive repeats a funding timestamp")
        points[point.timestamp] = point
    return points


def _window_evidence(
    *,
    points: dict[datetime, FundingPoint],
    funding_source_id: str,
    decision_at: datetime,
    column_name: str,
    window: int,
) -> FundingFeatureEvidence:
    anchor = funding_anchor_instant(decision_at.date())
    start, end = funding_window_instants(anchor, window)
    timestamps = tuple(start + timedelta(hours=offset) for offset in range(window))
    present = tuple(points[timestamp] for timestamp in timestamps if timestamp in points)
    missing = tuple(timestamp for timestamp in timestamps if timestamp not in points)
    observed_pairs = tuple((point.timestamp, point.interest_1h) for point in present)
    input_digest = (
        funding_input_digest(
            funding_source_id=funding_source_id,
            column_name=column_name,
            window=window,
            points=observed_pairs,
        )
        if observed_pairs
        else None
    )
    if missing:
        first = missing[0].isoformat()
        last = missing[-1].isoformat()
        return FundingFeatureEvidence(
            column_name=column_name,
            state=FeatureState.MISSING,
            window_start=start,
            window_end=end,
            expected_points=window,
            observed_points=len(present),
            input_digest=input_digest,
            reasons=(
                _reason(
                    FundingReasonCode.POINTS_ABSENT,
                    f"{len(missing)} of {window} hourly points are absent"
                    f" between {start.isoformat()} and {end.isoformat()};"
                    f" first_absent={first} last_absent={last}",
                ),
            ),
        )
    latest_available = max(assumed_funding_available_instant(point.timestamp) for point in present)
    if latest_available > decision_at:
        return FundingFeatureEvidence(
            column_name=column_name,
            state=FeatureState.NOT_YET_AVAILABLE,
            window_start=start,
            window_end=end,
            expected_points=window,
            observed_points=len(present),
            input_digest=input_digest,
            assumed_available_at=latest_available,
            reasons=(
                _reason(
                    FundingReasonCode.NOT_YET_AVAILABLE,
                    f"window ending {end.isoformat()} is assumed available at"
                    f" {latest_available.isoformat()} after decision {decision_at.isoformat()}",
                ),
            ),
        )
    with localcontext(DECIMAL34):
        value = sum((point.interest_1h for point in present), start=Decimal(0))
    return FundingFeatureEvidence(
        column_name=column_name,
        state=FeatureState.AVAILABLE,
        value=value,
        window_start=start,
        window_end=end,
        expected_points=window,
        observed_points=len(present),
        input_digest=input_digest,
        assumed_available_at=latest_available,
    )


def _sample(
    *,
    decision: FundingBaseDecision,
    points: dict[datetime, FundingPoint],
    funding_source_id: str,
) -> FundingSample:
    features = tuple(
        _window_evidence(
            points=points,
            funding_source_id=funding_source_id,
            decision_at=decision.decision_at,
            column_name=column,
            window=FUNDING_COLUMN_WINDOWS[column],
        )
        for column in FUNDING_COLUMNS
    )
    state = aggregate_funding_state(features)
    both_available = all(feature.state is FeatureState.AVAILABLE for feature in features)
    feature_hash = funding_feature_hash(
        sample_key=decision.sample_key,
        decision_at=decision.decision_at,
        features=features,
    )
    return FundingSample(
        sample_key=decision.sample_key,
        decision_at=decision.decision_at,
        features=features,
        funding_state=state,
        eligibility_base=decision.eligible,
        eligibility_joint=decision.eligible and both_available,
        split=decision.split,
        purged=decision.purged,
        funding_feature_hash=feature_hash,
    )


def build_funding_bundle(
    *,
    base_dataset: FundingBaseDatasetReference,
    decisions: tuple[FundingBaseDecision, ...],
    funding_asset: FundingAssetIdentity,
    archive: FundingArchive,
    known_at: datetime,
) -> FundingBundle:
    """Build the causal funding complement without network or operational writes."""
    normalized_known_at = known_at.astimezone(UTC) if known_at.tzinfo else None
    if normalized_known_at is None:
        raise FundingFeatureRequestError("known_at must include timezone information")
    _validate_base(
        base_dataset=base_dataset,
        decisions=decisions,
        funding_asset=funding_asset,
        archive=archive,
        known_at=normalized_known_at,
    )
    points = _points_by_timestamp(archive)
    rows = tuple(
        _sample(
            decision=decision,
            points=points,
            funding_source_id=funding_asset.funding_source_id,
        )
        for decision in decisions
    )
    counts = funding_counts(rows)
    splits = funding_split_coverage(rows)
    admission = funding_admission(splits)
    gaps = funding_gap_counts(rows)
    request = FundingFeatureRequest(
        decision_start_date=base_dataset.start_date,
        decision_end_date=base_dataset.end_date,
        decision_count=len(rows),
        known_at=normalized_known_at,
    )
    archive_reference = funding_archive_reference(archive)
    bundle_id = funding_bundle_id(
        base_dataset=base_dataset,
        funding_asset=funding_asset,
        archive=archive_reference,
        request=request,
        rows=rows,
    )
    content_hash = funding_bundle_content_hash(bundle_id=bundle_id, rows=rows)
    limitations = FUNDING_LIMITATIONS + (
        "El complemento referencia el dataset reconstruido por identidad y no copia"
        " sus features, labels, precios ni filas completas.",
        "La elegibilidad conjunta exige la elegibilidad original y ambas features"
        " completas; no se reinterpreta la muestra ni se traslada el holdout a"
        " matrices de entrenamiento.",
    )
    manifest = FundingFeatureManifest(
        bundle_id=bundle_id,
        bundle_content_sha256=content_hash,
        feature_set_version=FUNDING_FEATURE_SET_VERSION,
        availability_policy=FUNDING_AVAILABILITY_POLICY,
        base_dataset=base_dataset,
        funding_asset=funding_asset,
        archive=archive_reference,
        request=request,
        counts=counts,
        splits=splits,
        admission=admission,
        gaps=gaps,
        rows=rows,
        limitations=limitations,
    )
    return FundingBundle(manifest=manifest, rows=rows)


def funding_bundle_view_summary(bundle: FundingBundle) -> FundingBundleSummary:
    """Return the compact summary of one funding bundle."""
    return funding_bundle_summary(bundle.manifest)


__all__ = [
    "FundingBaseDecision",
    "FundingFeatureError",
    "FundingFeatureIntegrityError",
    "FundingFeatureRequestError",
    "build_funding_bundle",
    "funding_bundle_view_summary",
]
