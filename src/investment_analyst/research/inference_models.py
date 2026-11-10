"""Strict contracts and deterministic identities for one isolated inference step.

The only admitted engine is a synthetic constant test double. These contracts do
not train, serialize or promote a real model, and their output is never a signal,
candidate, recommendation or evidence of predictive performance.
"""

from __future__ import annotations

import json
import stat
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from investment_analyst.analytics.analytical_access_models import (
    analytical_content_hash,
    canonical_analytical_json,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import AssetClass, DataFrequency
from investment_analyst.research.feature_preview_models import (
    FeaturePreviewCloseReference,
    ResearchFeaturePreview,
    feature_preview_id,
)
from investment_analyst.research.predictive_models import (
    EXPERIMENT_SPEC_ID,
    FEATURE_COLUMNS,
    FEATURE_SET_VERSION,
    PREDICTIVE_FEATURE_COLUMNS,
    TARGET_VERSION,
    PredictiveAssetIdentity,
    PredictiveFeatureColumnSpec,
    PredictiveFeatureEvidence,
    decision_instant,
    predictive_feature_hash,
    predictive_sample_key,
)

MODEL_ARTIFACT_SCHEMA_VERSION = "research-model-artifact-v1"
MODEL_INFERENCE_SCHEMA_VERSION = "research-model-inference-v1"
MODEL_EXPLANATION_SCHEMA_VERSION = "model-explanation-packet-v1"

MODEL_ARTIFACT_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "investment-analyst/research-model-artifact-v1",
)
MODEL_INFERENCE_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "investment-analyst/research-model-inference-v1",
)

MAX_MODEL_ARTIFACT_BYTES = 16_384
MAX_MODEL_INFERENCE_BYTES = 65_536

CONSTANT_TEST_MODEL_ID = "constant-test-v1"
CONSTANT_TEST_MODEL_VERSION = "v1"
CONSTANT_TEST_ADAPTER_ID = "constant-test-v1"
CONSTANT_TEST_ADAPTER_VERSION = "v1"
CONSTANT_TEST_PROBABILITY = Decimal("0.5")
CONSTANT_TEST_METHOD = "constant_test_no_attribution"

MODEL_PURPOSE = "integration_test"
MODEL_VALIDATION_STATUS = "test_only"
MODEL_PROVENANCE = "synthetic_fixture_not_trained"

MODEL_TEST_LIMITATIONS: tuple[str, ...] = (
    "Modelo de prueba.",
    "No entrenado ni calibrado.",
    "Sin validación predictiva.",
    "No es una recomendación.",
)


def _require_inference_decimal(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("model inference financial values must use Decimal")
    return value


InferenceDecimal = Annotated[Decimal, BeforeValidator(_require_inference_decimal)]


def _artifact_probability(value: object) -> Decimal:
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("test_probability must be a decimal string") from error
    else:
        raise ValueError("test_probability must be a decimal string")
    if not candidate.is_finite():
        raise ValueError("test_probability must be finite")
    return candidate


ArtifactProbability = Annotated[Decimal, BeforeValidator(_artifact_probability)]


def _require_sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("artifact_sha256 must be a lowercase SHA-256 digest")
    return value


Sha256Hex = Annotated[str, AfterValidator(_require_sha256)]


class InferenceStatus(StrEnum):
    """Closed outcome of one inference request."""

    TEST_OUTPUT = "test_output"
    INPUTS_UNAVAILABLE = "inputs_unavailable"
    ARTIFACT_NOT_AVAILABLE = "artifact_not_available"
    INCOMPATIBLE = "incompatible"


class InferenceReasonCode(StrEnum):
    """Finite reasons attached to an abstention or incompatibility."""

    ARTIFACT_NOT_YET_AVAILABLE = "artifact_not_yet_available"
    ASSET_IDENTITY_MISMATCH = "asset_identity_mismatch"
    ASSET_PROFILE_INCOMPATIBLE = "asset_profile_incompatible"
    SOURCE_MISMATCH = "source_mismatch"
    EXPERIMENT_MISMATCH = "experiment_mismatch"
    TARGET_VERSION_MISMATCH = "target_version_mismatch"
    FEATURE_SET_VERSION_MISMATCH = "feature_set_version_mismatch"
    FEATURE_COLUMNS_MISMATCH = "feature_columns_mismatch"
    FEATURE_CONTRACT_MISMATCH = "feature_contract_mismatch"
    FEATURE_INPUT_UNAVAILABLE = "feature_input_unavailable"
    FEATURE_EVIDENCE_AFTER_CUT = "feature_evidence_after_cut"
    PREVIEW_IDENTITY_MISMATCH = "preview_identity_mismatch"


class InferenceReason(ContractModel):
    """One finite inference reason with compact human-readable detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: InferenceReasonCode
    detail: NonEmptyStr


class ModelArtifactError(RuntimeError):
    """Base error for one local research model artifact."""


class ModelArtifactParseError(ModelArtifactError, ValueError):
    """The artifact file is not a bounded, strict UTF-8 JSON document."""


class ModelArtifactValidationError(ModelArtifactError, ValueError):
    """The artifact document does not satisfy the isolated contract."""


class ConstantTestParameters(ContractModel):
    """The single authorized test parametrization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["constant-test-v1"] = CONSTANT_TEST_MODEL_ID
    test_probability: ArtifactProbability = CONSTANT_TEST_PROBABILITY

    @model_validator(mode="after")
    def validate_constant(self) -> ConstantTestParameters:
        """Reject any probability other than the preregistered test double."""
        if self.test_probability != CONSTANT_TEST_PROBABILITY:
            raise ValueError("the constant test probability is fixed at 0.5")
        return self


class ModelArtifact(ContractModel):
    """One bounded, asset-agnostic test artifact with derived identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-model-artifact-v1"] = MODEL_ARTIFACT_SCHEMA_VERSION
    artifact_id: UUID
    artifact_sha256: Sha256Hex
    model_id: Literal["constant-test-v1"] = CONSTANT_TEST_MODEL_ID
    model_version: Literal["v1"] = CONSTANT_TEST_MODEL_VERSION
    adapter_id: Literal["constant-test-v1"] = CONSTANT_TEST_ADAPTER_ID
    adapter_version: Literal["v1"] = CONSTANT_TEST_ADAPTER_VERSION
    purpose: Literal["integration_test"] = MODEL_PURPOSE
    validation_status: Literal["test_only"] = MODEL_VALIDATION_STATUS
    experiment_spec_id: NonEmptyStr = EXPERIMENT_SPEC_ID
    target_version: NonEmptyStr = TARGET_VERSION
    feature_set_version: NonEmptyStr = FEATURE_SET_VERSION
    asset_class: AssetClass = AssetClass.CRYPTO
    quote_currency: NonEmptyStr = "USD"
    frequency: DataFrequency = DataFrequency.DAY_1
    feature_columns: tuple[PredictiveFeatureColumnSpec, ...] = PREDICTIVE_FEATURE_COLUMNS
    parameters: ConstantTestParameters = Field(default_factory=ConstantTestParameters)
    available_at: UTCDateTime
    provenance: Literal["synthetic_fixture_not_trained"] = MODEL_PROVENANCE

    @model_validator(mode="after")
    def validate_artifact(self) -> ModelArtifact:
        """Bind the artifact to the preregistered experiment and derived identity."""
        if self.experiment_spec_id != EXPERIMENT_SPEC_ID:
            raise ValueError("artifact belongs to another experiment")
        if self.target_version != TARGET_VERSION:
            raise ValueError("artifact belongs to another target")
        if self.feature_set_version != FEATURE_SET_VERSION:
            raise ValueError("artifact belongs to another feature set version")
        if self.asset_class is not AssetClass.CRYPTO:
            raise ValueError("the admitted artifact profile is crypto spot")
        if self.quote_currency != "USD":
            raise ValueError("the admitted artifact quote currency is USD")
        if self.frequency is not DataFrequency.DAY_1:
            raise ValueError("the admitted artifact frequency is daily")
        if self.feature_columns != PREDICTIVE_FEATURE_COLUMNS:
            raise ValueError("artifact feature columns must match the preregistered contract")
        payload = self.material_payload()
        if self.artifact_sha256 != analytical_content_hash(payload):
            raise ValueError("artifact_sha256 does not match the semantic payload")
        if self.artifact_id != model_artifact_id(payload):
            raise ValueError("artifact_id does not match the semantic payload")
        return self

    def material_payload(self) -> dict[str, object]:
        """Return every semantic field except the two derived identities."""
        return {
            "schema_version": self.schema_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "purpose": self.purpose,
            "validation_status": self.validation_status,
            "experiment_spec_id": self.experiment_spec_id,
            "target_version": self.target_version,
            "feature_set_version": self.feature_set_version,
            "asset_class": self.asset_class,
            "quote_currency": self.quote_currency,
            "frequency": self.frequency,
            "feature_columns": self.feature_columns,
            "parameters": self.parameters,
            "available_at": self.available_at,
            "provenance": self.provenance,
        }


def model_artifact_id(payload: dict[str, object]) -> UUID:
    """Derive the artifact identity from its canonical semantic payload."""
    return uuid5(MODEL_ARTIFACT_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


def _reject_json_float(value: str) -> object:
    raise ValueError(f"floating-point JSON number is not allowed: {value}")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def parse_model_artifact_json(data: bytes) -> ModelArtifact:
    """Parse one bounded UTF-8 artifact document without executing content."""
    if len(data) > MAX_MODEL_ARTIFACT_BYTES:
        raise ModelArtifactParseError(f"model artifact exceeds {MAX_MODEL_ARTIFACT_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ModelArtifactParseError("model artifact must be UTF-8") from error
    try:
        parsed = json.loads(
            text,
            parse_float=_reject_json_float,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ModelArtifactParseError("model artifact JSON is invalid") from error
    if not isinstance(parsed, dict):
        raise ModelArtifactParseError("model artifact must be one JSON object")
    try:
        return ModelArtifact.model_validate(parsed)
    except ValidationError as error:
        first = error.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(item) for item in first["loc"])
        raise ModelArtifactValidationError(
            f"model artifact is invalid at {location}: {first['msg']}"
        ) from error


def load_model_artifact(path: Path) -> ModelArtifact:
    """Read one regular artifact file with a hard byte cap before parsing."""
    try:
        information = path.lstat()
    except OSError as error:
        raise ModelArtifactParseError("model artifact is not readable") from error
    if not stat.S_ISREG(information.st_mode):
        raise ModelArtifactParseError("model artifact must be a regular file")
    if information.st_size > MAX_MODEL_ARTIFACT_BYTES:
        raise ModelArtifactParseError(f"model artifact exceeds {MAX_MODEL_ARTIFACT_BYTES} bytes")
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_MODEL_ARTIFACT_BYTES + 1)
    except OSError as error:
        raise ModelArtifactParseError("model artifact is not readable") from error
    return parse_model_artifact_json(data)


class ModelInferenceContext(ContractModel):
    """Compatibility context handed to the isolated adapter, without storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: UUID
    artifact_sha256: Sha256Hex
    preview_id: UUID
    workspace_uuid: UUID
    asset: PredictiveAssetIdentity
    experiment_spec_id: NonEmptyStr
    target_version: NonEmptyStr
    feature_set_version: NonEmptyStr
    decision_at: UTCDateTime
    known_at: UTCDateTime
    feature_columns: tuple[PredictiveFeatureColumnSpec, ...]

    @model_validator(mode="after")
    def validate_context(self) -> ModelInferenceContext:
        """Keep the adapter input inside the preregistered feature contract."""
        if self.feature_columns != PREDICTIVE_FEATURE_COLUMNS:
            raise ValueError("adapter context must preserve the preregistered columns")
        if self.known_at > self.decision_at:
            raise ValueError("adapter context cut must not follow the decision instant")
        return self


class ModelInferenceInput(ContractModel):
    """Closed adapter input: exactly three ordered Decimal feature values plus context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    feature_values: tuple[InferenceDecimal, InferenceDecimal, InferenceDecimal]
    context: ModelInferenceContext

    @model_validator(mode="after")
    def validate_values(self) -> ModelInferenceInput:
        """Require exactly three finite ordered feature values."""
        if len(self.feature_values) != len(FEATURE_COLUMNS):
            raise ValueError("adapter input requires exactly three feature values")
        if any(not value.is_finite() for value in self.feature_values):
            raise ValueError("adapter input feature values must be finite")
        return self


class ModelAdapterOutput(ContractModel):
    """Closed adapter output for the admitted binary-probability contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    output_kind: Literal["binary_probability"]
    probability: InferenceDecimal

    @model_validator(mode="after")
    def validate_probability(self) -> ModelAdapterOutput:
        """Keep an adapter probability finite and inside the closed interval."""
        if not self.probability.is_finite():
            raise ValueError("adapter probability must be finite")
        if not Decimal("0") <= self.probability <= Decimal("1"):
            raise ValueError("adapter probability must stay inside [0, 1]")
        return self


class ModelExplanationPacket(ContractModel):
    """Reproducible explanation linked to one container inference result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["model-explanation-packet-v1"] = MODEL_EXPLANATION_SCHEMA_VERSION
    inference_id: UUID
    artifact_id: UUID
    artifact_sha256: Sha256Hex
    model_id: NonEmptyStr
    model_version: NonEmptyStr
    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    preview_id: UUID
    workspace_uuid: UUID
    asset: PredictiveAssetIdentity
    source_id: NonEmptyStr
    experiment_spec_id: NonEmptyStr
    target_version: NonEmptyStr
    feature_set_version: NonEmptyStr
    decision_at: UTCDateTime
    known_at: UTCDateTime
    status: InferenceStatus
    features: tuple[PredictiveFeatureEvidence, ...]
    close_references: tuple[FeaturePreviewCloseReference, ...] = Field(default=(), max_length=2)
    output_interpretation: NonEmptyStr
    limitations: tuple[NonEmptyStr, ...]
    method: Literal["constant_test_no_attribution"] = CONSTANT_TEST_METHOD

    @model_validator(mode="after")
    def validate_packet(self) -> ModelExplanationPacket:
        """Keep identity references, evidence order and mandatory limits explicit."""
        if self.source_id != self.asset.source_id:
            raise ValueError("explanation source must match the asset identity")
        if (
            self.status is not InferenceStatus.INCOMPATIBLE
            and tuple(item.column_name for item in self.features) != FEATURE_COLUMNS
        ):
            raise ValueError("explanation features must preserve the preregistered order")
        if self.decision_at != decision_instant(self.decision_at.date()):
            raise ValueError("explanation decision_at must be the 12:00 UTC instant")
        if self.known_at > self.decision_at:
            raise ValueError("explanation cut must not follow the decision instant")
        periods = tuple(item.period_start for item in self.close_references)
        if periods != tuple(sorted(periods)):
            raise ValueError("explanation close references must be ordered by period")
        if len(set(periods)) != len(periods):
            raise ValueError("explanation close references must not repeat a period")
        for reference in self.close_references:
            if (
                self.status is not InferenceStatus.INCOMPATIBLE
                and reference.available_at > self.known_at
            ):
                raise ValueError("explanation close references must be available at the cut")
        for feature in self.features:
            if (
                self.status is not InferenceStatus.INCOMPATIBLE
                and feature.available_at is not None
                and feature.available_at > self.known_at
            ):
                raise ValueError("explanation features must be available at the cut")
        if not set(MODEL_TEST_LIMITATIONS).issubset(self.limitations):
            raise ValueError("explanation must publish every mandatory test limitation")
        return self

    def material_payload(self) -> dict[str, object]:
        """Return every explanation field except the container-owned inference id."""
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "preview_id": self.preview_id,
            "workspace_uuid": self.workspace_uuid,
            "asset": self.asset,
            "source_id": self.source_id,
            "experiment_spec_id": self.experiment_spec_id,
            "target_version": self.target_version,
            "feature_set_version": self.feature_set_version,
            "decision_at": self.decision_at,
            "known_at": self.known_at,
            "status": self.status,
            "features": self.features,
            "close_references": self.close_references,
            "output_interpretation": self.output_interpretation,
            "limitations": self.limitations,
            "method": self.method,
        }


def model_inference_id(
    *,
    artifact_sha256: str,
    adapter_version: str,
    preview_id: UUID,
    material_payload: dict[str, object],
) -> UUID:
    """Derive the inference identity from artifact, adapter, preview and material."""
    payload = {
        "schema_version": MODEL_INFERENCE_SCHEMA_VERSION,
        "artifact_sha256": artifact_sha256,
        "adapter_version": adapter_version,
        "preview_id": preview_id,
        "material_sha256": analytical_content_hash(material_payload),
    }
    return uuid5(MODEL_INFERENCE_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


class ModelInferenceRequest(ContractModel):
    """One validated artifact applied to one existing pre-decision preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: ModelArtifact
    preview: ResearchFeaturePreview

    @model_validator(mode="after")
    def validate_request(self) -> ModelInferenceRequest:
        """Recompute the preview identity instead of trusting a supplied identifier."""
        expected_sample_key = predictive_sample_key(
            experiment_spec_id=self.preview.experiment_spec_id,
            asset_id=self.preview.request.asset_id,
            source_id=self.preview.asset.source_id,
            decision_at=self.preview.decision_at,
        )
        if self.preview.sample_key != expected_sample_key:
            raise ValueError("preview sample_key does not match the request")
        expected_feature_hash = predictive_feature_hash(
            sample_key=self.preview.sample_key,
            asset_id=self.preview.request.asset_id,
            source_id=self.preview.asset.source_id,
            decision_at=self.preview.decision_at,
            features=self.preview.features,
        )
        if self.preview.feature_hash != expected_feature_hash:
            raise ValueError("preview feature_hash does not match its features")
        expected_preview_id = feature_preview_id(
            workspace_uuid=self.preview.workspace_uuid,
            asset=self.preview.asset,
            request=self.preview.request,
            decision_at=self.preview.decision_at,
            material_payload=self.preview.material_payload(),
        )
        if self.preview.preview_id != expected_preview_id:
            raise ValueError("preview_id does not match the material projection")
        return self


class ModelInferenceResult(ContractModel):
    """Deterministic inference projection with explicit abstention or test output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-model-inference-v1"] = MODEL_INFERENCE_SCHEMA_VERSION
    inference_id: UUID
    artifact_id: UUID
    artifact_sha256: Sha256Hex
    model_id: NonEmptyStr
    model_version: NonEmptyStr
    adapter_id: NonEmptyStr
    adapter_version: NonEmptyStr
    purpose: Literal["integration_test"]
    validation_status: Literal["test_only"]
    workspace_uuid: UUID
    asset: PredictiveAssetIdentity
    source_id: NonEmptyStr
    experiment_spec_id: NonEmptyStr
    target_version: NonEmptyStr
    feature_set_version: NonEmptyStr
    preview_id: UUID
    sample_key: UUID
    feature_hash: UUID
    decision_at: UTCDateTime
    known_at: UTCDateTime
    status: InferenceStatus
    reasons: tuple[InferenceReason, ...] = ()
    test_probability: InferenceDecimal | None = None
    explanation: ModelExplanationPacket

    @model_validator(mode="after")
    def validate_result(self) -> ModelInferenceResult:
        """Bind status, probability, explanation and the recomputed identity."""
        if self.source_id != self.asset.source_id:
            raise ValueError("inference source must match the asset identity")
        if self.decision_at != decision_instant(self.decision_at.date()):
            raise ValueError("inference decision_at must be the 12:00 UTC instant")
        if self.known_at > self.decision_at:
            raise ValueError("inference cut must not follow the decision instant")
        if self.explanation.inference_id != self.inference_id:
            raise ValueError("explanation must link the container inference_id")
        if self.explanation.artifact_id != self.artifact_id:
            raise ValueError("explanation artifact_id must match the result")
        if self.explanation.artifact_sha256 != self.artifact_sha256:
            raise ValueError("explanation artifact hash must match the result")
        if self.explanation.model_id != self.model_id:
            raise ValueError("explanation model_id must match the result")
        if self.explanation.model_version != self.model_version:
            raise ValueError("explanation model_version must match the result")
        if self.explanation.adapter_id != self.adapter_id:
            raise ValueError("explanation adapter_id must match the result")
        if self.explanation.adapter_version != self.adapter_version:
            raise ValueError("explanation adapter_version must match the result")
        if self.explanation.preview_id != self.preview_id:
            raise ValueError("explanation preview_id must match the result")
        if self.explanation.workspace_uuid != self.workspace_uuid:
            raise ValueError("explanation workspace must match the result")
        if self.explanation.asset != self.asset:
            raise ValueError("explanation asset must match the result")
        if self.explanation.source_id != self.source_id:
            raise ValueError("explanation source must match the result")
        if self.explanation.experiment_spec_id != self.experiment_spec_id:
            raise ValueError("explanation experiment must match the result")
        if self.explanation.target_version != self.target_version:
            raise ValueError("explanation target must match the result")
        if self.explanation.feature_set_version != self.feature_set_version:
            raise ValueError("explanation feature set must match the result")
        if self.explanation.decision_at != self.decision_at:
            raise ValueError("explanation decision_at must match the result")
        if self.explanation.known_at != self.known_at:
            raise ValueError("explanation known_at must match the result")
        if self.explanation.status is not self.status:
            raise ValueError("explanation status must match the result")
        if self.status is InferenceStatus.TEST_OUTPUT:
            if self.test_probability is None:
                raise ValueError("test_output requires a test probability")
            if not self.test_probability.is_finite():
                raise ValueError("test probability must be finite")
            if self.test_probability != CONSTANT_TEST_PROBABILITY:
                raise ValueError("the only authorized test probability is 0.5")
            if self.reasons:
                raise ValueError("test_output must not carry abstention reasons")
        else:
            if self.test_probability is not None:
                raise ValueError("abstentions must not expose a probability")
            if not self.reasons:
                raise ValueError("abstentions require at least one reason")
        expected_inference_id = model_inference_id(
            artifact_sha256=self.artifact_sha256,
            adapter_version=self.adapter_version,
            preview_id=self.preview_id,
            material_payload=self.material_payload(),
        )
        if self.inference_id != expected_inference_id:
            raise ValueError("inference_id does not match the material result")
        return self

    def material_payload(self) -> dict[str, object]:
        """Return every semantic field except the derived inference identity."""
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "purpose": self.purpose,
            "validation_status": self.validation_status,
            "workspace_uuid": self.workspace_uuid,
            "asset": self.asset,
            "source_id": self.source_id,
            "experiment_spec_id": self.experiment_spec_id,
            "target_version": self.target_version,
            "feature_set_version": self.feature_set_version,
            "preview_id": self.preview_id,
            "sample_key": self.sample_key,
            "feature_hash": self.feature_hash,
            "decision_at": self.decision_at,
            "known_at": self.known_at,
            "status": self.status,
            "reasons": self.reasons,
            "test_probability": self.test_probability,
            "explanation": self.explanation.material_payload(),
        }

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection served by the CLI."""
        return self.model_dump(mode="json")


__all__ = [
    "CONSTANT_TEST_ADAPTER_ID",
    "CONSTANT_TEST_ADAPTER_VERSION",
    "CONSTANT_TEST_METHOD",
    "CONSTANT_TEST_MODEL_ID",
    "CONSTANT_TEST_MODEL_VERSION",
    "CONSTANT_TEST_PROBABILITY",
    "MAX_MODEL_ARTIFACT_BYTES",
    "MAX_MODEL_INFERENCE_BYTES",
    "MODEL_ARTIFACT_NAMESPACE",
    "MODEL_ARTIFACT_SCHEMA_VERSION",
    "MODEL_EXPLANATION_SCHEMA_VERSION",
    "MODEL_INFERENCE_NAMESPACE",
    "MODEL_INFERENCE_SCHEMA_VERSION",
    "MODEL_PURPOSE",
    "MODEL_TEST_LIMITATIONS",
    "MODEL_VALIDATION_STATUS",
    "ConstantTestParameters",
    "InferenceReason",
    "InferenceReasonCode",
    "InferenceStatus",
    "ModelAdapterOutput",
    "ModelArtifact",
    "ModelArtifactError",
    "ModelArtifactParseError",
    "ModelArtifactValidationError",
    "ModelExplanationPacket",
    "ModelInferenceContext",
    "ModelInferenceInput",
    "ModelInferenceRequest",
    "ModelInferenceResult",
    "load_model_artifact",
    "model_artifact_id",
    "model_inference_id",
    "parse_model_artifact_json",
]
