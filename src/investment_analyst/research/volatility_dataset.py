"""Decimal-exact construction of the volatility study's causal decisions.

Features use only closes strictly before the decision instant; labels require
the five daily returns that follow it and never reach the reserved period.
Missing bars, non-positive prices and non-consecutive windows are excluded with
an explicit reason instead of being filled, interpolated or skipped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.research.historical_models import HistoricalArchive
from investment_analyst.research.volatility_models import (
    DECIMAL34,
    DECISION_END,
    DECISION_START,
    EPSILON,
    FEATURE_WINDOWS,
    MAX_CANDLES,
    MIN_SEGMENT_ROWS,
    REFERENCE_WINDOW,
    RESERVED_START_INDEX,
    TARGET_HORIZON,
    VolatilityFoldBoundary,
    VolatilityProtocol,
    VolatilityReportCounts,
    decision_instant,
    fold_label_cut,
    label_maturity_instant,
    reserved_label_cut,
    volatility_row_id,
    volatility_sample_key,
)

FEATURE_NAMES: dict[int, str] = {
    1: "log_mean_squared_return_1d",
    5: "log_mean_squared_return_5d",
    7: "log_mean_squared_return_7d",
    30: "log_mean_squared_return_30d",
}


class VolatilityDatasetError(RuntimeError):
    """The archive or its declared scope contradicts the sealed protocol."""


class VolatilityInsufficientSampleError(VolatilityDatasetError):
    """A declared floor, window or segment condition is not satisfied."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class VolatilityFeature:
    """One log mean-squared return feature with its exact bar evidence."""

    window: int
    name: str
    value: Decimal
    bar_content_ids: tuple[UUID, ...]
    available_at: datetime


@dataclass(frozen=True, slots=True)
class VolatilityDecisionRow:
    """One reconstructed decision, its features, label and explicit gaps."""

    index: int
    decision_date: date
    decision_at: datetime
    sample_key: UUID
    features: tuple[VolatilityFeature, ...] | None
    reference_5d: VolatilityFeature | None
    target: Decimal | None
    target_bar_ids: tuple[UUID, ...]
    label_maturity: datetime | None
    evidence_available_at: datetime | None
    feature_exclusion: str | None
    target_exclusion: str | None
    row_id: UUID | None

    @property
    def feature_ready(self) -> bool:
        return self.features is not None and self.reference_5d is not None

    @property
    def target_ready(self) -> bool:
        return self.feature_ready and self.target is not None

    @property
    def feature_values(self) -> tuple[Decimal, Decimal, Decimal]:
        if self.features is None:
            raise VolatilityDatasetError("feature values are unavailable")
        return tuple(feature.value for feature in self.features)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class VolatilityFoldPlan:
    """One preregistered fold after eligibility, embargo and segment checks."""

    fold: int
    boundary: VolatilityFoldBoundary
    train: tuple[VolatilityDecisionRow, ...]
    validation: tuple[VolatilityDecisionRow, ...]
    segments: tuple[tuple[VolatilityDecisionRow, ...], ...]
    train_embargo_excluded: int
    train_ineligible_excluded: int
    validation_embargo_excluded: int
    validation_ineligible_excluded: int


@dataclass(frozen=True, slots=True)
class VolatilityDataset:
    """The constructed development decisions and honest reading counts."""

    protocol: VolatilityProtocol
    decisions: tuple[VolatilityDecisionRow, ...]
    counts: VolatilityReportCounts


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_analytical_json(value)).hexdigest()


def _validate_archive(archive: HistoricalArchive, protocol: VolatilityProtocol) -> None:
    manifest = archive.manifest
    if manifest.asset.asset_id != protocol.asset_id:
        raise VolatilityDatasetError("archive asset differs from the sealed protocol")
    if manifest.asset.source_id != protocol.source_id:
        raise VolatilityDatasetError("archive source differs from the sealed protocol")
    if manifest.archive_id != protocol.archive_id:
        raise VolatilityDatasetError("archive identity differs from the sealed protocol")
    if manifest.semantic_sha256 != protocol.archive_semantic_sha256:
        raise VolatilityDatasetError("archive semantic digest differs from the sealed protocol")
    if manifest.candles_sha256 != protocol.archive_candles_sha256:
        raise VolatilityDatasetError("archive candles digest differs from the sealed protocol")
    if manifest.retrieved_at != protocol.archive_retrieved_at:
        raise VolatilityDatasetError("archive retrieval instant differs from the sealed protocol")
    if manifest.request.start_date != protocol.archive_request_start:
        raise VolatilityDatasetError("archive request start differs from the sealed protocol")
    if manifest.request.end_date != protocol.archive_request_end:
        raise VolatilityDatasetError("archive request end differs from the sealed protocol")
    if len(archive.candles) > MAX_CANDLES:
        raise VolatilityDatasetError("archive exceeds the declared candle budget")


def _close_map(
    archive: HistoricalArchive,
) -> tuple[dict[date, Decimal], dict[date, UUID], dict[date, datetime]]:
    closes: dict[date, Decimal] = {}
    bar_ids: dict[date, UUID] = {}
    available: dict[date, datetime] = {}
    for candle in archive.candles:
        day = candle.bar_start.date()
        closes[day] = candle.close
        bar_ids[day] = candle.bar_content_id
        available[day] = candle.assumed_available_at
    return closes, bar_ids, available


def _return_evidence(
    day: date,
    *,
    closes: dict[date, Decimal],
    bar_ids: dict[date, UUID],
    available: dict[date, datetime],
) -> tuple[Decimal, tuple[UUID, UUID], datetime] | str:
    previous = day - timedelta(days=1)
    current_value = closes.get(day)
    previous_value = closes.get(previous)
    if current_value is None or previous_value is None:
        return "missing_close"
    if current_value <= 0 or previous_value <= 0:
        return "nonpositive_close"
    if not current_value.is_finite() or not previous_value.is_finite():
        return "nonfinite_close"
    with localcontext(DECIMAL34):
        value = (current_value / previous_value).ln()
    return (
        value,
        (bar_ids[previous], bar_ids[day]),
        max(available[previous], available[day]),
    )


def _window_feature(
    *,
    window: int,
    decision_date: date,
    closes: dict[date, Decimal],
    bar_ids: dict[date, UUID],
    available: dict[date, datetime],
) -> VolatilityFeature | str:
    with localcontext(DECIMAL34):
        total = Decimal(0)
        evidence_ids: list[UUID] = []
        latest = None
        for offset in range(window, 0, -1):
            day = decision_date - timedelta(days=offset)
            evidence = _return_evidence(
                day,
                closes=closes,
                bar_ids=bar_ids,
                available=available,
            )
            if isinstance(evidence, str):
                return evidence
            value, ids, available_at = evidence
            total += value * value
            latest = available_at if latest is None else max(latest, available_at)
            for bar_id in ids:
                if bar_id not in evidence_ids:
                    evidence_ids.append(bar_id)
        if latest is None:
            return "missing_close"
        mean = total / Decimal(window)
        transformed = (mean + EPSILON).ln()
    return VolatilityFeature(
        window=window,
        name=FEATURE_NAMES[window],
        value=transformed,
        bar_content_ids=tuple(evidence_ids),
        available_at=latest,
    )


def _feature_hash(
    features: tuple[VolatilityFeature, ...],
    reference: VolatilityFeature,
) -> str:
    payload = {
        "features": [
            {
                "bar_content_ids": [str(value) for value in feature.bar_content_ids],
                "name": feature.name,
                "value": str(feature.value),
                "window": feature.window,
            }
            for feature in features
        ],
        "reference_5d": {
            "bar_content_ids": [str(value) for value in reference.bar_content_ids],
            "name": reference.name,
            "value": str(reference.value),
            "window": reference.window,
        },
    }
    return _sha256(payload)


def _target_hash(
    target: Decimal,
    *,
    bar_ids: tuple[UUID, ...],
    label_maturity: datetime,
    evidence_available_at: datetime,
) -> str:
    return _sha256(
        {
            "bar_content_ids": [str(value) for value in bar_ids],
            "evidence_available_at": evidence_available_at.astimezone(UTC).isoformat(),
            "label_maturity": label_maturity.astimezone(UTC).isoformat(),
            "value": str(target),
        }
    )


def _target(
    *,
    decision_date: date,
    closes: dict[date, Decimal],
    bar_ids: dict[date, UUID],
) -> tuple[Decimal, tuple[UUID, ...]] | str:
    with localcontext(DECIMAL34):
        total = Decimal(0)
        evidence: list[UUID] = []
        for offset in range(1, TARGET_HORIZON + 1):
            day = decision_date + timedelta(days=offset)
            previous = day - timedelta(days=1)
            current_value = closes.get(day)
            previous_value = closes.get(previous)
            if current_value is None or previous_value is None:
                return "label_history_incomplete"
            if current_value <= 0 or previous_value <= 0:
                return "label_nonpositive_close"
            if not current_value.is_finite() or not previous_value.is_finite():
                return "label_nonfinite_close"
            value = (current_value / previous_value).ln()
            total += value * value
            for bar_id in (bar_ids[previous], bar_ids[day]):
                if bar_id not in evidence:
                    evidence.append(bar_id)
        mean = total / Decimal(TARGET_HORIZON)
        transformed = (mean + EPSILON).ln()
    return transformed, tuple(evidence)


def _decision_row(
    index: int,
    *,
    closes: dict[date, Decimal],
    bar_ids: dict[date, UUID],
    available: dict[date, datetime],
    protocol: VolatilityProtocol,
) -> VolatilityDecisionRow:
    decision_date = DECISION_START + timedelta(days=index)
    decision_at = decision_instant(decision_date)
    sample_key = volatility_sample_key(
        asset_id=protocol.asset_id,
        source_id=protocol.source_id,
        decision_at=decision_at,
    )
    built: list[VolatilityFeature] = []
    feature_exclusion: str | None = None
    for window in FEATURE_WINDOWS:
        outcome = _window_feature(
            window=window,
            decision_date=decision_date,
            closes=closes,
            bar_ids=bar_ids,
            available=available,
        )
        if isinstance(outcome, str):
            feature_exclusion = outcome
            break
        built.append(outcome)
    reference_outcome = _window_feature(
        window=REFERENCE_WINDOW,
        decision_date=decision_date,
        closes=closes,
        bar_ids=bar_ids,
        available=available,
    )
    reference = reference_outcome if isinstance(reference_outcome, VolatilityFeature) else None
    if reference is None and feature_exclusion is None:
        feature_exclusion = reference_outcome if isinstance(reference_outcome, str) else None
    features = (
        tuple(built) if len(built) == len(FEATURE_WINDOWS) and reference is not None else None
    )
    if features is None and feature_exclusion is None:
        feature_exclusion = "missing_close"
    feature_available_at = (
        max(feature.available_at for feature in (*features, reference))
        if features is not None and reference is not None
        else None
    )
    if feature_available_at is not None and feature_available_at > decision_at:
        raise VolatilityDatasetError("feature availability must not follow the decision instant")

    label_maturity = label_maturity_instant(decision_date)
    evidence_available_at: datetime | None = None
    target: Decimal | None = None
    target_bar_ids: tuple[UUID, ...] = ()
    target_exclusion: str | None = None
    if label_maturity >= reserved_label_cut():
        target_exclusion = "label_period_reserved"
    else:
        outcome = _target(
            decision_date=decision_date,
            closes=closes,
            bar_ids=bar_ids,
        )
        if isinstance(outcome, str):
            target_exclusion = outcome
        else:
            target, target_bar_ids = outcome
            evidence_available_at = max(label_maturity, protocol.archive_retrieved_at)
            if evidence_available_at > protocol.study_cut_at:
                raise VolatilityDatasetError("label evidence is not available at the study cut")

    row_id: UUID | None = None
    if features is not None and reference is not None and target is not None:
        feature_hash = _feature_hash(features, reference)
        assert evidence_available_at is not None
        target_hash = _target_hash(
            target,
            bar_ids=target_bar_ids,
            label_maturity=label_maturity,
            evidence_available_at=evidence_available_at,
        )
        feature_bar_ids = tuple(
            dict.fromkeys(
                bar_id for feature in (*features, reference) for bar_id in feature.bar_content_ids
            )
        )
        row_id = volatility_row_id(
            sample_key=sample_key,
            feature_hash=feature_hash,
            target_hash=target_hash,
            feature_bar_ids=feature_bar_ids,
            target_bar_ids=target_bar_ids,
        )
    return VolatilityDecisionRow(
        index=index,
        decision_date=decision_date,
        decision_at=decision_at,
        sample_key=sample_key,
        features=features,
        reference_5d=reference,
        target=target,
        target_bar_ids=target_bar_ids,
        label_maturity=label_maturity,
        evidence_available_at=evidence_available_at,
        feature_exclusion=feature_exclusion,
        target_exclusion=target_exclusion,
        row_id=row_id,
    )


def build_volatility_decisions(
    archive: HistoricalArchive,
    protocol: VolatilityProtocol,
) -> VolatilityDataset:
    """Construct only the development decisions declared by the sealed protocol."""
    _validate_archive(archive, protocol)
    closes, bar_ids, available = _close_map(archive)
    decisions = tuple(
        _decision_row(
            index,
            closes=closes,
            bar_ids=bar_ids,
            available=available,
            protocol=protocol,
        )
        for index in range(RESERVED_START_INDEX)
    )
    feature_ready = sum(1 for row in decisions if row.feature_ready)
    target_ready = sum(1 for row in decisions if row.target_ready)
    counts = VolatilityReportCounts(
        planned_decisions=protocol.decision_count,
        constructed_decisions=len(decisions),
        reserved_decisions_not_constructed=protocol.decision_count - len(decisions),
        feature_ready=feature_ready,
        target_ready=target_ready,
        candles_read=len(archive.candles),
    )
    return VolatilityDataset(protocol=protocol, decisions=decisions, counts=counts)


def _segments(
    rows: tuple[VolatilityDecisionRow, ...],
) -> tuple[tuple[VolatilityDecisionRow, ...], ...]:
    if not rows:
        return ()
    segments: list[list[VolatilityDecisionRow]] = [[rows[0]]]
    for previous, current in zip(rows, rows[1:], strict=False):
        if current.decision_date - previous.decision_date == timedelta(days=1):
            segments[-1].append(current)
        else:
            segments.append([current])
    return tuple(tuple(segment) for segment in segments)


def plan_folds(
    dataset: VolatilityDataset,
    protocol: VolatilityProtocol,
) -> tuple[VolatilityFoldPlan, ...]:
    """Assign every decision to its fold before applying eligibility and embargo."""
    if dataset.protocol != protocol:
        raise VolatilityDatasetError("dataset and fold planning protocols differ")
    if len(dataset.decisions) != RESERVED_START_INDEX:
        raise VolatilityDatasetError("dataset length disagrees with the reserved boundary")
    if dataset.decisions[0].decision_date != DECISION_START:
        raise VolatilityDatasetError("dataset does not start on the preregistered date")
    if dataset.decisions[-1].decision_date != DECISION_END - timedelta(
        days=protocol.decision_count - RESERVED_START_INDEX
    ):
        raise VolatilityDatasetError("dataset does not end before the reserved period")
    plans: list[VolatilityFoldPlan] = []
    for boundary in protocol.folds:
        start = boundary.validation_start_index
        end = boundary.validation_end_index
        train_cut = fold_label_cut(boundary.validation_start_date)
        validation_cut = fold_label_cut(boundary.validation_end_date)
        train: list[VolatilityDecisionRow] = []
        train_embargo = 0
        train_ineligible = 0
        for row in dataset.decisions[:start]:
            if not row.target_ready:
                train_ineligible += 1
                continue
            assert row.label_maturity is not None
            if row.label_maturity < train_cut:
                train.append(row)
            else:
                train_embargo += 1
        validation: list[VolatilityDecisionRow] = []
        validation_embargo = 0
        validation_ineligible = 0
        for row in dataset.decisions[start:end]:
            if not row.target_ready:
                validation_ineligible += 1
                continue
            assert row.label_maturity is not None
            if row.label_maturity < validation_cut:
                validation.append(row)
            else:
                validation_embargo += 1
        if len(train) < protocol.minimum_train_rows:
            raise VolatilityInsufficientSampleError(
                f"fold {boundary.fold} training rows below the declared floor"
            )
        if len(validation) < protocol.minimum_validation_rows:
            raise VolatilityInsufficientSampleError(
                f"fold {boundary.fold} validation rows below the declared floor"
            )
        segments = _segments(tuple(validation))
        if not segments or min(len(segment) for segment in segments) < MIN_SEGMENT_ROWS:
            raise VolatilityInsufficientSampleError(
                f"fold {boundary.fold} has no resampleable contiguous segment"
            )
        plans.append(
            VolatilityFoldPlan(
                fold=boundary.fold,
                boundary=boundary,
                train=tuple(train),
                validation=tuple(validation),
                segments=segments,
                train_embargo_excluded=train_embargo,
                train_ineligible_excluded=train_ineligible,
                validation_embargo_excluded=validation_embargo,
                validation_ineligible_excluded=validation_ineligible,
            )
        )
    return tuple(plans)


__all__ = [
    "FEATURE_NAMES",
    "VolatilityDataset",
    "VolatilityDatasetError",
    "VolatilityDecisionRow",
    "VolatilityFeature",
    "VolatilityFoldPlan",
    "VolatilityInsufficientSampleError",
    "build_volatility_decisions",
    "plan_folds",
]
