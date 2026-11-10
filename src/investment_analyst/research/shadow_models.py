"""Strict contracts and deterministic identities for one bounded experimental shadow run.

A shadow run is an explicitly enabled, local observation of the existing test inference contract.
It is never a signal, candidate, recommendation, ranking or alert input, and it does not train,
promote or acquire anything.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, field_validator, model_validator

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.inference_models import (
    CONSTANT_TEST_PROBABILITY,
    MAX_MODEL_INFERENCE_BYTES,
    MODEL_TEST_LIMITATIONS,
    InferenceDecimal,
    InferenceStatus,
)
from investment_analyst.research.predictive_models import DECISION_HOUR_UTC, decision_instant

SHADOW_RUN_SCHEMA_VERSION = "research-model-shadow-run-v1"
SHADOW_VERSION = "research-model-shadow-v1"
SHADOW_RUN_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "investment-analyst/research-model-shadow-run-v1",
)
MAX_SHADOW_MANIFEST_BYTES = 16_384
MAX_SHADOW_RESULT_BYTES = MAX_MODEL_INFERENCE_BYTES
MAX_SHADOW_ASSETS = 2
MAX_SHADOW_RUNS = 32
MAX_SHADOW_STORE_BYTES = 8 * 1024 * 1024

SHADOW_LIMITATIONS: tuple[str, ...] = MODEL_TEST_LIMITATIONS + (
    "Observación experimental acotada; no es señal, candidato ni recomendación.",
    "No demuestra capacidad predictiva ni autoriza promoción o entrenamiento.",
    "El resultado no se publica en outbox, screening, alertas ni un ranking.",
)


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShadowRunRequest(_Strict):
    """One explicit, bounded shadow request with no clock or asset defaults."""

    workspace: Path
    model_artifact: Path
    asset_ids: tuple[NonEmptyStr, ...]
    decision_date: date
    known_at: UTCDateTime
    output_root: Path

    @field_validator("workspace", "model_artifact", "output_root")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("shadow paths must be absolute")
        if ".." in expanded.parts:
            raise ValueError("shadow paths must not contain traversal")
        return Path(os.path.abspath(expanded))

    @field_validator("decision_date", mode="before")
    @classmethod
    def plain_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("decision_date must not include a time")
        if isinstance(value, str) and (len(value) != 10 or value[4] != "-" or value[7] != "-"):
            raise ValueError("decision_date must use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def bounded_request(self) -> ShadowRunRequest:
        if not 1 <= len(self.asset_ids) <= MAX_SHADOW_ASSETS:
            raise ValueError("asset_ids must contain one or two values")
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("asset_ids must not repeat")
        if self.output_root == Path("/"):
            raise ValueError("output_root must be a dedicated shadow root")
        day_start = datetime.combine(self.decision_date, time(0), tzinfo=UTC)
        decision_at = datetime.combine(self.decision_date, time(hour=DECISION_HOUR_UTC), tzinfo=UTC)
        if self.known_at < day_start:
            raise ValueError("known_at must not precede the decision date 00:00 UTC")
        if self.known_at > decision_at:
            raise ValueError("known_at must not be later than the 12:00 UTC decision instant")
        return self


class ShadowAssetManifest(_Strict):
    """One asset result reference inside a shadow manifest."""

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    inference_id: UUID
    inference_sha256: NonEmptyStr
    result_file: NonEmptyStr
    status: InferenceStatus
    test_probability: InferenceDecimal | None = None

    @model_validator(mode="after")
    def valid_asset(self) -> ShadowAssetManifest:
        if len(self.inference_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.inference_sha256
        ):
            raise ValueError("inference_sha256 must be a lowercase SHA-256 digest")
        if self.result_file != f"{self.inference_id}.json":
            raise ValueError("result_file must derive from the inference UUID")
        if self.status is InferenceStatus.TEST_OUTPUT:
            if self.test_probability is None or self.test_probability != CONSTANT_TEST_PROBABILITY:
                raise ValueError("test_output requires the authorized test probability")
        elif self.test_probability is not None:
            raise ValueError("abstentions must not expose a probability")
        return self


class ShadowRunManifest(_Strict):
    """Complete, execution-clock-free manifest of one shadow run."""

    schema_version: Literal["research-model-shadow-run-v1"] = SHADOW_RUN_SCHEMA_VERSION
    shadow_version: Literal["research-model-shadow-v1"] = SHADOW_VERSION
    run_id: UUID
    workspace_uuid: UUID
    artifact_id: UUID
    artifact_sha256: NonEmptyStr
    decision_date: date
    known_at: UTCDateTime
    decision_at: UTCDateTime
    assets: tuple[ShadowAssetManifest, ...]
    purpose: Literal["integration_test"] = "integration_test"
    validation_status: Literal["test_only"] = "test_only"
    limitations: tuple[NonEmptyStr, ...] = SHADOW_LIMITATIONS

    @model_validator(mode="after")
    def valid_manifest(self) -> ShadowRunManifest:
        if not 1 <= len(self.assets) <= MAX_SHADOW_ASSETS:
            raise ValueError("manifest must reference one or two assets")
        if tuple(item.asset_id for item in self.assets) != tuple(
            sorted(item.asset_id for item in self.assets)
        ):
            raise ValueError("manifest assets must use canonical order")
        if len({item.asset_id for item in self.assets}) != len(self.assets):
            raise ValueError("manifest assets must not repeat")
        if self.decision_at != decision_instant(self.decision_date):
            raise ValueError("manifest decision_at must be the 12:00 UTC instant")
        if not datetime.combine(self.decision_date, time(0), tzinfo=UTC) <= self.known_at:
            raise ValueError("manifest cut must fall inside the decision date")
        if self.known_at > self.decision_at:
            raise ValueError("manifest cut must not follow the decision instant")
        if len(self.artifact_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.artifact_sha256
        ):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 digest")
        if not set(SHADOW_LIMITATIONS).issubset(self.limitations):
            raise ValueError("manifest must publish every mandatory shadow limitation")
        if self.run_id != shadow_run_id(self.material_payload()):
            raise ValueError("run_id does not match the material manifest")
        return self

    def material_payload(self) -> dict[str, object]:
        """Return every semantic field except the derived run identity."""
        return {
            "schema_version": self.schema_version,
            "shadow_version": self.shadow_version,
            "workspace_uuid": self.workspace_uuid,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "decision_date": self.decision_date.isoformat(),
            "known_at": self.known_at,
            "decision_at": self.decision_at,
            "assets": [item.model_dump(mode="json") for item in self.assets],
            "purpose": self.purpose,
            "validation_status": self.validation_status,
            "limitations": list(self.limitations),
        }

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def shadow_run_id(material_payload: dict[str, object]) -> UUID:
    """Derive the shadow identity from the canonical material manifest."""
    return uuid5(
        SHADOW_RUN_NAMESPACE,
        canonical_analytical_json(material_payload).decode("utf-8"),
    )


class ShadowRunSummary(_Strict):
    """Compact CLI result for one created or reused shadow run."""

    schema_version: Literal["research-model-shadow-run-v1"] = SHADOW_RUN_SCHEMA_VERSION
    status: Literal["created", "reused"]
    run_id: UUID
    manifest_path: NonEmptyStr
    manifest_sha256: NonEmptyStr
    assets: tuple[ShadowAssetManifest, ...]

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


__all__ = [
    "MAX_SHADOW_ASSETS",
    "MAX_SHADOW_MANIFEST_BYTES",
    "MAX_SHADOW_RESULT_BYTES",
    "MAX_SHADOW_RUNS",
    "MAX_SHADOW_STORE_BYTES",
    "SHADOW_LIMITATIONS",
    "SHADOW_RUN_NAMESPACE",
    "SHADOW_RUN_SCHEMA_VERSION",
    "SHADOW_VERSION",
    "ShadowAssetManifest",
    "ShadowRunManifest",
    "ShadowRunRequest",
    "ShadowRunSummary",
    "shadow_run_id",
]
