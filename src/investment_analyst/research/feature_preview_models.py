"""Isolated pre-decision feature preview contract without labels or training.

The preview answers one narrow question: which predictive inputs were already
available at an explicit cut inside decision date D, before the preregistered
12:00 UTC decision. It reuses the feature evidence and deterministic identities
of the predictive dataset, but it is not a dataset, a sample admission status or
a predictive signal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.analytical_access_models import (
    analytical_content_hash,
    canonical_analytical_json,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.predictive_models import (
    DECISION_HOUR_UTC,
    EXPERIMENT_SPEC_ID,
    FEATURE_COLUMNS,
    FEATURE_SET_VERSION,
    FeatureState,
    PredictiveAssetIdentity,
    PredictiveFeatureEvidence,
    aggregate_feature_state,
    predictive_feature_hash,
    predictive_sample_key,
)

FEATURE_PREVIEW_SCHEMA_VERSION = "research-feature-preview-v1"
FEATURE_PREVIEW_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/research-feature-preview-v1")
MAX_FEATURE_PREVIEW_BYTES = 32_768


class FeaturePreviewError(RuntimeError):
    """Base error for pre-decision feature preview construction."""


class FeaturePreviewAssetUnsupportedError(FeaturePreviewError):
    """The requested asset is outside the supported crypto spot profile."""


class FeaturePreviewIntegrityError(FeaturePreviewError):
    """Persisted evidence contradicts the preview scope or selector contract."""


class FeaturePreviewBudgetError(FeaturePreviewError):
    """The material projection exceeds the bounded response budget."""


class FeaturePreviewStatus(StrEnum):
    """Whether every preregistered input column is available at the cut."""

    FEATURES_AVAILABLE = "features_available"
    INPUTS_UNAVAILABLE = "inputs_unavailable"


class FeaturePreviewRequest(ContractModel):
    """One decision date and one explicit cut inside that UTC day."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    decision_date: date
    known_at: UTCDateTime

    @field_validator("decision_date", mode="before")
    @classmethod
    def require_plain_date(cls, value: object) -> object:
        """Reject a datetime or an ambiguous date spelling before coercion."""
        if isinstance(value, datetime):
            raise ValueError("decision_date must not include a time")
        if isinstance(value, str) and (len(value) != 10 or value[4] != "-" or value[7] != "-"):
            raise ValueError("decision_date must use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def validate_cut(self) -> FeaturePreviewRequest:
        """Keep the cut before the preregistered decision instant of the same day."""
        day_start = datetime.combine(self.decision_date, time(0), tzinfo=UTC)
        decision_at = datetime.combine(
            self.decision_date,
            time(hour=DECISION_HOUR_UTC),
            tzinfo=UTC,
        )
        if self.known_at < day_start:
            raise ValueError("known_at must not precede the decision date 00:00 UTC")
        if self.known_at > decision_at:
            raise ValueError("known_at must not be later than the 12:00 UTC decision instant")
        return self


class FeaturePreviewCloseReference(ContractModel):
    """Compact reference to one close selected for the preview features."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: UUID
    period_start: UTCDateTime
    period_end: UTCDateTime
    available_at: UTCDateTime

    @model_validator(mode="after")
    def validate_period(self) -> FeaturePreviewCloseReference:
        """Require one exact daily period."""
        if self.period_end != self.period_start + timedelta(days=1):
            raise ValueError("close references must span exactly one UTC day")
        return self


def feature_preview_id(
    *,
    workspace_uuid: UUID,
    asset: PredictiveAssetIdentity,
    request: FeaturePreviewRequest,
    decision_at: datetime,
    material_payload: dict[str, object],
) -> UUID:
    """Derive the isolated preview identity from the material projection digest."""
    payload = {
        "workspace_uuid": workspace_uuid,
        "asset_id": asset.asset_id,
        "source_id": asset.source_id,
        "experiment_spec_id": EXPERIMENT_SPEC_ID,
        "feature_set_version": FEATURE_SET_VERSION,
        "decision_date": request.decision_date.isoformat(),
        "known_at": request.known_at,
        "projection_sha256": analytical_content_hash(material_payload),
    }
    return uuid5(FEATURE_PREVIEW_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


class ResearchFeaturePreview(ContractModel):
    """Immutable pre-decision projection with three ordered feature columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-feature-preview-v1"] = FEATURE_PREVIEW_SCHEMA_VERSION
    preview_id: UUID
    workspace_uuid: UUID
    asset: PredictiveAssetIdentity
    experiment_spec_id: NonEmptyStr
    feature_set_version: NonEmptyStr
    request: FeaturePreviewRequest
    decision_at: UTCDateTime
    status: FeaturePreviewStatus
    sample_key: UUID
    feature_hash: UUID
    features: tuple[PredictiveFeatureEvidence, ...]
    close_references: tuple[FeaturePreviewCloseReference, ...] = Field(max_length=2)
    limitations: tuple[NonEmptyStr, ...] = ()

    def material_payload(self) -> dict[str, object]:
        """Return every semantic field except the derived preview identity."""
        return {
            "schema_version": self.schema_version,
            "workspace_uuid": self.workspace_uuid,
            "asset": self.asset,
            "experiment_spec_id": self.experiment_spec_id,
            "feature_set_version": self.feature_set_version,
            "request": self.request.model_dump(mode="json"),
            "decision_at": self.decision_at,
            "status": self.status,
            "sample_key": self.sample_key,
            "feature_hash": self.feature_hash,
            "features": self.features,
            "close_references": self.close_references,
            "limitations": self.limitations,
        }

    @model_validator(mode="after")
    def validate_preview(self) -> ResearchFeaturePreview:
        """Bind identity, ordered evidence, status and closed references."""
        expected_decision = datetime.combine(
            self.request.decision_date,
            time(hour=DECISION_HOUR_UTC),
            tzinfo=UTC,
        )
        if self.decision_at != expected_decision:
            raise ValueError("decision_at must be the preregistered 12:00 UTC instant")
        if self.experiment_spec_id != EXPERIMENT_SPEC_ID:
            raise ValueError("preview belongs to another experiment")
        if self.feature_set_version != FEATURE_SET_VERSION:
            raise ValueError("preview belongs to another feature set version")
        if tuple(item.column_name for item in self.features) != FEATURE_COLUMNS:
            raise ValueError("preview features must preserve the preregistered order")
        expected_sample_key = predictive_sample_key(
            experiment_spec_id=self.experiment_spec_id,
            asset_id=self.request.asset_id,
            source_id=self.asset.source_id,
            decision_at=self.decision_at,
        )
        if self.sample_key != expected_sample_key:
            raise ValueError("sample_key does not match the preview scope")
        expected_feature_hash = predictive_feature_hash(
            sample_key=self.sample_key,
            asset_id=self.request.asset_id,
            source_id=self.asset.source_id,
            decision_at=self.decision_at,
            features=self.features,
        )
        if self.feature_hash != expected_feature_hash:
            raise ValueError("feature_hash does not match the preview features")
        aggregate = aggregate_feature_state(self.features)
        expected_status = (
            FeaturePreviewStatus.FEATURES_AVAILABLE
            if aggregate is FeatureState.AVAILABLE
            else FeaturePreviewStatus.INPUTS_UNAVAILABLE
        )
        if self.status is not expected_status:
            raise ValueError("preview status must follow the three feature states")
        periods = tuple(item.period_start for item in self.close_references)
        if periods != tuple(sorted(periods)):
            raise ValueError("close references must be ordered by period")
        if len(set(periods)) != len(periods):
            raise ValueError("close references must not repeat a period")
        for reference in self.close_references:
            if reference.available_at > self.request.known_at:
                raise ValueError("close references must be available at the cut")
        expected_preview_id = feature_preview_id(
            workspace_uuid=self.workspace_uuid,
            asset=self.asset,
            request=self.request,
            decision_at=self.decision_at,
            material_payload=self.material_payload(),
        )
        if self.preview_id != expected_preview_id:
            raise ValueError("preview_id does not match the material projection")
        if not self.limitations:
            raise ValueError("preview must publish its interpretation limits")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection served by CLI and API."""
        return self.model_dump(mode="json")


__all__ = [
    "FEATURE_PREVIEW_NAMESPACE",
    "FEATURE_PREVIEW_SCHEMA_VERSION",
    "MAX_FEATURE_PREVIEW_BYTES",
    "FeaturePreviewAssetUnsupportedError",
    "FeaturePreviewBudgetError",
    "FeaturePreviewCloseReference",
    "FeaturePreviewError",
    "FeaturePreviewIntegrityError",
    "FeaturePreviewRequest",
    "FeaturePreviewStatus",
    "ResearchFeaturePreview",
    "feature_preview_id",
]
