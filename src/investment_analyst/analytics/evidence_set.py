"""Canonical, storage-free lineage representation for point-in-time evidence windows.

The module describes how one window of hourly observations is shared as lineage without
copying its identifiers on every row: complete UTC days become content-addressed
segments, and a window is a trim of those segments.

It is pure: it never reads storage or the filesystem, never writes, never uses a clock
and never imports the derivatives engine, pipeline or service. Nothing in production
imports it yet.

Policy ``evidence-segmentation-v1`` maps one hourly series to UTC days. The contract
leaves the extension point for other frequencies, but only the hourly rule is
implemented and any other frequency fails closed.

``available_at`` of a set is the maximum availability of the lineage it represents
(its segments and its inline tail). Because segments are day-granular, a window whose
last segment is trimmed by ``input_count`` can declare an availability later than the
exact maximum of its own inputs — never earlier, which is the conservative direction
for a point-in-time contract.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.crypto.derivatives_models import observation_time
from investment_analyst.core.models import NormalizedObservation
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency

SEGMENTATION_POLICY = "evidence-segmentation-v1"
_SEGMENT_LABEL = "evidence-segment-v1"
_SET_LABEL = "evidence-set-v1"
_HOURS_PER_DAY = 24
_MAX_INLINE = _HOURS_PER_DAY - 1


class EvidenceSetError(RuntimeError):
    """Base error for the canonical evidence-lineage contract."""


class EvidenceSegmentationError(EvidenceSetError):
    """Raised when a series cannot be segmented under the declared policy."""


class EvidenceSetConstructionError(EvidenceSetError):
    """Raised when a window cannot be represented by the canonical lineage."""


class EvidenceSetVerificationError(EvidenceSetError):
    """Raised when a resolved lineage contradicts its own contract."""


def canonical_lineage_hash(observation_ids: Sequence[UUID]) -> str:
    """Return SHA-256 of the canonical encoding of the ordered identifiers.

    The hash depends only on the complete ordered sequence, never on how it was
    segmented: two valid segmentations of the same window share this value.
    """
    encoded = json.dumps(
        [str(identifier) for identifier in observation_ids],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def segment_identity(
    *,
    asset_id: str,
    source_id: str,
    field_name: str,
    day: date,
    observation_ids: Sequence[UUID],
) -> UUID:
    """Return the content-addressed identity of one complete-day segment."""
    return _identity(
        _SEGMENT_LABEL,
        {
            "asset_id": asset_id,
            "day": day.isoformat(),
            "field_name": field_name,
            "observation_ids": [str(identifier) for identifier in observation_ids],
            "source_id": source_id,
        },
    )


def evidence_set_identity(
    *,
    asset_id: str,
    source_id: str,
    field_name: str,
    canonical_hash: str,
) -> UUID:
    """Return the identity of one window lineage, shared by every metric over it."""
    return _identity(
        _SET_LABEL,
        {
            "asset_id": asset_id,
            "canonical_hash": canonical_hash,
            "field_name": field_name,
            "source_id": source_id,
        },
    )


class EvidenceSegment(ContractModel):
    """Immutable, content-addressed block of one complete UTC day of hourly evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: UUID
    segmentation_policy: Literal["evidence-segmentation-v1"] = "evidence-segmentation-v1"
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    field_name: NonEmptyStr
    day: date
    observation_ids: tuple[UUID, ...]
    available_at: UTCDateTime
    canonical_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_segment(self) -> EvidenceSegment:
        """Require a complete, unique day whose identity and hash match its content."""
        if len(self.observation_ids) != _HOURS_PER_DAY:
            raise ValueError("an evidence segment covers exactly one complete UTC day")
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("evidence segment identifiers must be unique")
        if self.canonical_hash != canonical_lineage_hash(self.observation_ids):
            raise ValueError("evidence segment hash does not match its identifiers")
        if self.segment_id != segment_identity(
            asset_id=self.asset_id,
            source_id=self.source_id,
            field_name=self.field_name,
            day=self.day,
            observation_ids=self.observation_ids,
        ):
            raise ValueError("evidence segment identity is not deterministic")
        return self


class EvidenceSet(ContractModel):
    """Ordered lineage of one evidence window as a trim of complete-day segments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_set_id: UUID
    segmentation_policy: Literal["evidence-segmentation-v1"] = "evidence-segmentation-v1"
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    field_name: NonEmptyStr
    input_count: int = Field(ge=1)
    segment_ids: tuple[UUID, ...] = ()
    head_offset: int = Field(default=0, ge=0, le=_MAX_INLINE)
    inline_observation_ids: tuple[UUID, ...] = ()
    inline_available_at: UTCDateTime | None = None
    first_observed_at: UTCDateTime
    first_observation_id: UUID
    last_observed_at: UTCDateTime
    last_observation_id: UUID
    available_at: UTCDateTime
    canonical_hash: NonEmptyStr

    @model_validator(mode="after")
    def validate_evidence_set(self) -> EvidenceSet:
        """Keep the window shape, the inline tail and the identity internally consistent."""
        if not self.segment_ids and self.head_offset != 0:
            raise ValueError("a head offset requires at least one referenced segment")
        if len(self.inline_observation_ids) > _MAX_INLINE:
            raise ValueError("only an incomplete final day may be stored inline")
        if len(set(self.inline_observation_ids)) != len(self.inline_observation_ids):
            raise ValueError("inline identifiers must be unique")
        if bool(self.inline_observation_ids) is not (self.inline_available_at is not None):
            raise ValueError("inline identifiers and their availability require each other")
        reserved = len(self.inline_observation_ids)
        if self.segment_ids:
            reserved += (
                _HOURS_PER_DAY - self.head_offset + _HOURS_PER_DAY * (len(self.segment_ids) - 1)
            )
        if self.input_count > reserved:
            raise ValueError("window does not fit the referenced segments and inline tail")
        if self.evidence_set_id != evidence_set_identity(
            asset_id=self.asset_id,
            source_id=self.source_id,
            field_name=self.field_name,
            canonical_hash=self.canonical_hash,
        ):
            raise ValueError("evidence set identity is not deterministic")
        return self


def _identity(label: str, document: dict[str, object]) -> UUID:
    encoded = json.dumps(
        {"label": label, **document},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _uuid_v8(hashlib.sha256(encoded.encode("utf-8")).digest())


def _uuid_v8(digest: bytes) -> UUID:
    raw = bytearray(digest[:16])
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _ordered_rows(
    observations: Sequence[NormalizedObservation],
) -> tuple[tuple[datetime, UUID], ...]:
    """Return the canonically ordered (time, identifier) pairs of one hourly series."""
    if not observations:
        raise EvidenceSetConstructionError("evidence lineage requires at least one observation")
    scope = (
        observations[0].asset_id,
        observations[0].source.source_id,
        observations[0].field_name,
    )
    rows: list[tuple[datetime, UUID]] = []
    for observation in observations:
        if observation.frequency is not DataFrequency.HOUR_1:
            raise EvidenceSegmentationError(
                "evidence-segmentation-v1 only supports hourly observations"
            )
        if observation.observed_at is None:
            raise EvidenceSetConstructionError("hourly evidence requires observed_at")
        if (
            observation.asset_id,
            observation.source.source_id,
            observation.field_name,
        ) != scope:
            raise EvidenceSetConstructionError(
                "evidence lineage must cover one asset, one source and one field"
            )
        rows.append((observation_time(observation).astimezone(UTC), observation.observation_id))
    for previous, current in zip(rows, rows[1:], strict=False):
        if current[0] - previous[0] != timedelta(hours=1):
            raise EvidenceSetConstructionError(
                "evidence lineage requires contiguous hourly observations"
            )
    if len({identifier for _, identifier in rows}) != len(rows):
        raise EvidenceSetConstructionError("evidence lineage requires unique observations")
    return tuple(rows)


def build_evidence_segments(
    observations: Sequence[NormalizedObservation],
) -> tuple[EvidenceSegment, ...]:
    """Build one immutable segment per complete UTC day of an ordered hourly series."""
    rows = _ordered_rows(observations)
    by_day: dict[date, list[int]] = {}
    for position, (observed_at, _) in enumerate(rows):
        by_day.setdefault(observed_at.date(), []).append(position)
    segments: list[EvidenceSegment] = []
    for day in sorted(by_day):
        positions = by_day[day]
        if len(positions) != _HOURS_PER_DAY:
            continue
        members = tuple(observations[position] for position in positions)
        identifiers = tuple(item.observation_id for item in members)
        segments.append(
            EvidenceSegment(
                segment_id=segment_identity(
                    asset_id=members[0].asset_id,
                    source_id=members[0].source.source_id,
                    field_name=members[0].field_name,
                    day=day,
                    observation_ids=identifiers,
                ),
                asset_id=members[0].asset_id,
                source_id=members[0].source.source_id,
                field_name=members[0].field_name,
                day=day,
                observation_ids=identifiers,
                available_at=max(item.available_at for item in members),
                canonical_hash=canonical_lineage_hash(identifiers),
            )
        )
    return tuple(segments)


def build_evidence_set(
    window: Sequence[NormalizedObservation],
    *,
    segments: Sequence[EvidenceSegment],
) -> EvidenceSet:
    """Describe one window as a trim of complete-day segments plus an inline tail."""
    rows = _ordered_rows(window)
    asset_id, source_id, field_name = (
        window[0].asset_id,
        window[0].source.source_id,
        window[0].field_name,
    )
    positions: dict[UUID, tuple[EvidenceSegment, int]] = {}
    for segment in segments:
        if (
            segment.asset_id != asset_id
            or segment.source_id != source_id
            or segment.field_name != field_name
        ):
            raise EvidenceSetConstructionError("segments mix asset, source or field")
        for index, identifier in enumerate(segment.observation_ids):
            positions[identifier] = (segment, index)

    covered: list[tuple[EvidenceSegment, int]] = []
    inline: list[NormalizedObservation] = []
    for observation, (_, identifier) in zip(window, rows, strict=True):
        position = positions.get(identifier)
        if position is None:
            inline.append(observation)
            continue
        if inline:
            raise EvidenceSetConstructionError(
                "uncovered evidence must be the final incomplete day"
            )
        covered.append(position)

    ordered_segments: list[EvidenceSegment] = []
    head_offset = 0
    cursor: int | None = None
    for segment, index in covered:
        if cursor is None:
            head_offset = index
            cursor = index
            ordered_segments.append(segment)
            continue
        if index == 0:
            if cursor != _HOURS_PER_DAY - 1 or ordered_segments[-1] is segment:
                raise EvidenceSetConstructionError("segments are not contiguous hourly evidence")
            cursor = 0
            ordered_segments.append(segment)
            continue
        if index != cursor + 1 or ordered_segments[-1] is not segment:
            raise EvidenceSetConstructionError("segments are not contiguous hourly evidence")
        cursor = index
    if len(inline) > _MAX_INLINE:
        raise EvidenceSetConstructionError(
            "evidence-segmentation-v1 stores at most the final incomplete day inline"
        )
    identifiers = tuple(identifier for _, identifier in rows)
    inline_ids = tuple(item.observation_id for item in inline)
    availability = [segment.available_at for segment in ordered_segments]
    if inline:
        availability.append(max(item.available_at for item in inline))
    candidate = EvidenceSet(
        evidence_set_id=evidence_set_identity(
            asset_id=asset_id,
            source_id=source_id,
            field_name=field_name,
            canonical_hash=canonical_lineage_hash(identifiers),
        ),
        asset_id=asset_id,
        source_id=source_id,
        field_name=field_name,
        input_count=len(identifiers),
        segment_ids=tuple(segment.segment_id for segment in ordered_segments),
        head_offset=head_offset,
        inline_observation_ids=inline_ids,
        inline_available_at=max(item.available_at for item in inline) if inline else None,
        first_observed_at=rows[0][0],
        first_observation_id=rows[0][1],
        last_observed_at=rows[-1][0],
        last_observation_id=rows[-1][1],
        available_at=max(availability),
        canonical_hash=canonical_lineage_hash(identifiers),
    )
    try:
        verify_evidence_set(candidate, ordered_segments)
    except EvidenceSetVerificationError as error:
        raise EvidenceSetConstructionError(str(error)) from error
    return candidate


def _selected_segments(
    evidence_set: EvidenceSet,
    segments: Sequence[EvidenceSegment],
) -> tuple[EvidenceSegment, ...]:
    """Return the referenced segments in contract order, failing closed on any gap."""
    by_id: dict[UUID, EvidenceSegment] = {}
    for segment in segments:
        known = by_id.get(segment.segment_id)
        if known is not None and known != segment:
            raise EvidenceSetVerificationError("a segment identity maps to two contents")
        by_id[segment.segment_id] = segment
    if any(identifier not in by_id for identifier in evidence_set.segment_ids):
        raise EvidenceSetVerificationError("a referenced segment is missing or unresolvable")
    return tuple(by_id[identifier] for identifier in evidence_set.segment_ids)


def _covered_identifiers(
    evidence_set: EvidenceSet,
    selected: tuple[EvidenceSegment, ...],
) -> tuple[UUID, ...]:
    """Return every identifier the referenced segments can provide, without truncation."""
    identifiers: list[UUID] = []
    for position, segment in enumerate(selected):
        start = evidence_set.head_offset if position == 0 else 0
        identifiers.extend(segment.observation_ids[start:])
    return tuple(identifiers)


def _resolved_identifiers(
    evidence_set: EvidenceSet,
    selected: tuple[EvidenceSegment, ...],
) -> tuple[UUID, ...]:
    """Return the ordered identifiers the contract describes."""
    identifiers = list(_covered_identifiers(evidence_set, selected))
    identifiers.extend(evidence_set.inline_observation_ids)
    return tuple(identifiers[: evidence_set.input_count])


def _expected_times(
    evidence_set: EvidenceSet,
    selected: tuple[EvidenceSegment, ...],
) -> tuple[datetime, ...]:
    """Return the hourly times the contract implies for each described identifier."""
    times: list[datetime] = []
    for position, segment in enumerate(selected):
        start = evidence_set.head_offset if position == 0 else 0
        midnight = datetime(segment.day.year, segment.day.month, segment.day.day, tzinfo=UTC)
        for index in range(start, _HOURS_PER_DAY):
            times.append(midnight + timedelta(hours=index))
    if evidence_set.inline_observation_ids:
        first_hour = evidence_set.last_observed_at - timedelta(
            hours=len(evidence_set.inline_observation_ids) - 1
        )
        for offset in range(len(evidence_set.inline_observation_ids)):
            times.append(first_hour + timedelta(hours=offset))
    return tuple(times[: evidence_set.input_count])


def verify_evidence_set(
    evidence_set: EvidenceSet,
    segments: Sequence[EvidenceSegment],
) -> None:
    """Recompute every segment and the window hash, and fail closed on any contradiction."""
    selected = _selected_segments(evidence_set, segments)
    for segment in selected:
        if (
            segment.asset_id != evidence_set.asset_id
            or segment.source_id != evidence_set.source_id
            or segment.field_name != evidence_set.field_name
        ):
            raise EvidenceSetVerificationError("a segment mixes asset, source or field")
        if segment.canonical_hash != canonical_lineage_hash(segment.observation_ids):
            raise EvidenceSetVerificationError("a segment hash does not match its identifiers")
        if segment.segment_id != segment_identity(
            asset_id=segment.asset_id,
            source_id=segment.source_id,
            field_name=segment.field_name,
            day=segment.day,
            observation_ids=segment.observation_ids,
        ):
            raise EvidenceSetVerificationError("a segment identity does not match its content")
    for previous, current in zip(selected, selected[1:], strict=False):
        if current.day - previous.day != timedelta(days=1):
            raise EvidenceSetVerificationError("segments are not consecutive UTC days")

    covered = _covered_identifiers(evidence_set, selected)
    if evidence_set.inline_observation_ids:
        if len(covered) + len(evidence_set.inline_observation_ids) != evidence_set.input_count:
            raise EvidenceSetVerificationError("inline tail does not fit the window")
    elif len(covered) < evidence_set.input_count:
        raise EvidenceSetVerificationError("window exceeds the referenced segments")
    identifiers = _resolved_identifiers(evidence_set, selected)
    if len(identifiers) != evidence_set.input_count:
        raise EvidenceSetVerificationError("resolved lineage does not fit the declared window")
    if len(set(identifiers)) != len(identifiers):
        raise EvidenceSetVerificationError("resolved lineage repeats an observation")
    if identifiers[0] != evidence_set.first_observation_id:
        raise EvidenceSetVerificationError("first identifier does not match the window")
    if identifiers[-1] != evidence_set.last_observation_id:
        raise EvidenceSetVerificationError("last identifier does not match the window")

    times = _expected_times(evidence_set, selected)
    if len(times) != len(identifiers):
        raise EvidenceSetVerificationError("resolved lineage has no coherent hourly shape")
    for previous, current in zip(times, times[1:], strict=False):
        if current - previous != timedelta(hours=1):
            raise EvidenceSetVerificationError("resolved lineage is not contiguous hourly")
    if times[0] != evidence_set.first_observed_at:
        raise EvidenceSetVerificationError("first observation time does not match the window")
    if times[-1] != evidence_set.last_observed_at:
        raise EvidenceSetVerificationError("last observation time does not match the window")

    declared = [segment.available_at for segment in selected]
    if evidence_set.inline_available_at is not None:
        declared.append(evidence_set.inline_available_at)
    if not declared or max(declared) != evidence_set.available_at:
        raise EvidenceSetVerificationError("available_at is not the maximum of the lineage")
    if canonical_lineage_hash(identifiers) != evidence_set.canonical_hash:
        raise EvidenceSetVerificationError("canonical hash does not match the lineage")


def resolve_evidence_set(
    evidence_set: EvidenceSet,
    segments: Sequence[EvidenceSegment],
) -> tuple[UUID, ...]:
    """Return exactly the ordered identifiers of one verified evidence window."""
    verify_evidence_set(evidence_set, segments)
    return _resolved_identifiers(evidence_set, _selected_segments(evidence_set, segments))


__all__ = [
    "SEGMENTATION_POLICY",
    "EvidenceSegment",
    "EvidenceSegmentationError",
    "EvidenceSet",
    "EvidenceSetConstructionError",
    "EvidenceSetError",
    "EvidenceSetVerificationError",
    "build_evidence_segments",
    "build_evidence_set",
    "canonical_lineage_hash",
    "evidence_set_identity",
    "resolve_evidence_set",
    "segment_identity",
    "verify_evidence_set",
]
