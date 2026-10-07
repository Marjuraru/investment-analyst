"""Strict, deterministic projections for descriptive analytical reads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, model_validator

from investment_analyst.analytics.analysis_domain import (
    require_authorized_domain,
    validate_metric_key_for_domain,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import (
    DataQuality,
    DiagnosticMode,
    DiagnosticVerdict,
    EvidenceDirection,
)


def _canonical_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical analytical timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, ContractModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("canonical analytical object keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        raise TypeError("floating-point values are not valid canonical analytical content")
    raise TypeError(f"unsupported canonical analytical value: {type(value).__name__}")


def canonical_analytical_json(value: object) -> bytes:
    """Encode stable UTF-8 JSON with exact Decimal strings and normalized UTC."""
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def analytical_content_hash(value: object) -> str:
    return hashlib.sha256(canonical_analytical_json(value)).hexdigest()


class MetricPageCursor(ContractModel):
    """Stable metric-series cursor over (available_at, result_id)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available_at: UTCDateTime
    result_id: UUID


class MetricSeriesQuery(ContractModel):
    """One bounded descriptive metric query within a single domain and cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    domain: NonEmptyStr
    known_at: UTCDateTime
    metric_keys: tuple[NonEmptyStr, ...]
    source_id: NonEmptyStr | None = None
    frequency: NonEmptyStr | None = None
    as_of_from: UTCDateTime | None = None
    as_of_before: UTCDateTime | None = None
    after: MetricPageCursor | None = None
    limit: int = Field(default=256, ge=1, le=256)

    @model_validator(mode="after")
    def validate_query(self) -> MetricSeriesQuery:
        require_authorized_domain(self.domain)
        if not self.metric_keys:
            raise ValueError("metric_keys must be an explicit non-empty selection")
        if len(set(self.metric_keys)) != len(self.metric_keys):
            raise ValueError("metric_keys must be unique")
        for key in self.metric_keys:
            validate_metric_key_for_domain(key, self.domain)
        if (
            self.as_of_from is not None
            and self.as_of_before is not None
            and self.as_of_from >= self.as_of_before
        ):
            raise ValueError("as_of range must be non-empty and half-open")
        if self.after is not None and self.after.available_at > self.known_at:
            raise ValueError("metric page cursor must not be after known_at")
        return self


class MetricIndexEntry(ContractModel):
    """Small indexed projection used to verify scope before model hydration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID
    asset_id: NonEmptyStr
    metric_key: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    source_id: NonEmptyStr | None = None
    frequency: NonEmptyStr | None = None
    legacy_known_at: str | None = None


class MetricSeriesPoint(ContractModel):
    """Descriptive metric content with execution-clock metadata excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID
    asset_id: NonEmptyStr
    metric_key: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue]
    input_observation_ids: tuple[UUID, ...]
    input_metric_result_ids: tuple[UUID, ...]
    algorithm_version: NonEmptyStr
    quality: DataQuality


class MetricSeriesPage(ContractModel):
    """One bounded page; a cursor is supplied whenever the page may continue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MetricSeriesPoint, ...]
    next_cursor: MetricPageCursor | None = None
    truncated: bool
    content_hash: str

    @model_validator(mode="after")
    def validate_page(self) -> MetricSeriesPage:
        _validate_hash(self.content_hash, "content_hash")
        if self.truncated != (self.next_cursor is not None):
            raise ValueError("truncated pages must carry exactly one continuation cursor")
        return self


class DiagnosticComponentAccess(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component_key: NonEmptyStr
    score: Decimal
    weight: Decimal
    weighted_contribution: Decimal
    metric_result_ids: tuple[UUID, ...]
    explanation: NonEmptyStr


class DiagnosticEvidenceAccess(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_result_id: UUID
    direction: EvidenceDirection
    contribution: Decimal
    reason: NonEmptyStr


class DiagnosticAccessRecord(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostic_id: UUID
    asset_id: NonEmptyStr
    mode: DiagnosticMode
    verdict: DiagnosticVerdict
    final_score: Decimal
    confidence: Decimal
    as_of: UTCDateTime
    available_at: UTCDateTime
    components: tuple[DiagnosticComponentAccess, ...]
    evidence: tuple[DiagnosticEvidenceAccess, ...]
    algorithm_version: NonEmptyStr
    summary: NonEmptyStr
    quality: DataQuality


class SnapshotEvidence(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    asset_id: NonEmptyStr
    domain: NonEmptyStr
    known_at: UTCDateTime
    metrics: tuple[MetricSeriesPoint, ...]
    diagnostics: tuple[DiagnosticAccessRecord, ...]
    content_hash: str

    @model_validator(mode="after")
    def validate_hash(self) -> SnapshotEvidence:
        _validate_hash(self.content_hash, "content_hash")
        return self


class FeatureSetSpec(ContractModel):
    """Versioned explicit projection over already-persisted metric keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_set_id: NonEmptyStr
    version: NonEmptyStr
    domain: NonEmptyStr
    metric_keys: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_feature_set(self) -> FeatureSetSpec:
        require_authorized_domain(self.domain)
        if not self.metric_keys or len(set(self.metric_keys)) != len(self.metric_keys):
            raise ValueError("feature set must contain unique, explicit metric keys")
        for key in self.metric_keys:
            validate_metric_key_for_domain(key, self.domain)
        return self


class FeatureObservation(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    input_observation_ids: tuple[UUID, ...]
    input_metric_result_ids: tuple[UUID, ...]
    algorithm_version: NonEmptyStr
    quality: DataQuality


class FeatureValue(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    status: Literal["available", "missing"]
    values: tuple[FeatureObservation, ...]
    reason: str | None = None

    @model_validator(mode="after")
    def validate_status(self) -> FeatureValue:
        if self.status == "available" and (not self.values or self.reason is not None):
            raise ValueError("available features require values and no missing reason")
        if self.status == "missing" and (self.values or not self.reason):
            raise ValueError("missing features require a reason and no values")
        return self


class FeatureSetResult(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    feature_set_id: NonEmptyStr
    feature_set_version: NonEmptyStr
    domain: NonEmptyStr
    values: tuple[FeatureValue, ...]
    schema_hash: str
    content_hash: str

    @model_validator(mode="after")
    def validate_hashes(self) -> FeatureSetResult:
        _validate_hash(self.schema_hash, "schema_hash")
        _validate_hash(self.content_hash, "content_hash")
        return self


class MetricExplanation(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID
    metric_key: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue]
    input_observation_ids: tuple[UUID, ...]
    input_metric_result_ids: tuple[UUID, ...]
    algorithm_version: NonEmptyStr
    quality: DataQuality
    display_name: NonEmptyStr
    description: NonEmptyStr
    formula: NonEmptyStr
    definition_version: NonEmptyStr
    limitations: tuple[NonEmptyStr, ...]
    references: tuple[NonEmptyStr, ...]


class ExplanationDocument(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    domain: NonEmptyStr
    known_at: UTCDateTime
    items: tuple[MetricExplanation, ...]
    content_hash: str

    @model_validator(mode="after")
    def validate_hash(self) -> ExplanationDocument:
        _validate_hash(self.content_hash, "content_hash")
        return self


def _validate_hash(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def metric_series_point_content(point: MetricSeriesPoint) -> Mapping[str, object]:
    """Return the stable semantic fields represented by a metric series point."""
    return point.model_dump(mode="python")


def ordered_metric_series_points(
    points: Sequence[MetricSeriesPoint],
) -> tuple[MetricSeriesPoint, ...]:
    return tuple(sorted(points, key=lambda item: (item.available_at, str(item.result_id))))


__all__ = [
    "DiagnosticAccessRecord",
    "DiagnosticComponentAccess",
    "DiagnosticEvidenceAccess",
    "ExplanationDocument",
    "FeatureObservation",
    "FeatureSetResult",
    "FeatureSetSpec",
    "FeatureValue",
    "MetricExplanation",
    "MetricIndexEntry",
    "MetricPageCursor",
    "MetricSeriesPage",
    "MetricSeriesPoint",
    "MetricSeriesQuery",
    "SnapshotEvidence",
    "analytical_content_hash",
    "canonical_analytical_json",
    "metric_series_point_content",
    "ordered_metric_series_points",
]
