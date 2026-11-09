"""Compatibility, abstention and explanation for one isolated inference step."""

from __future__ import annotations

import json
from decimal import Decimal

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.feature_preview_models import (
    FeaturePreviewStatus,
    ResearchFeaturePreview,
    feature_preview_id,
)
from investment_analyst.research.inference_adapter import (
    ModelInferenceAdapter,
    ModelInferenceAdapterError,
    resolve_model_inference_adapter,
)
from investment_analyst.research.inference_models import (
    CONSTANT_TEST_PROBABILITY,
    MAX_MODEL_INFERENCE_BYTES,
    MODEL_TEST_LIMITATIONS,
    InferenceReason,
    InferenceReasonCode,
    InferenceStatus,
    ModelAdapterOutput,
    ModelArtifact,
    ModelExplanationPacket,
    ModelInferenceContext,
    ModelInferenceInput,
    ModelInferenceRequest,
    ModelInferenceResult,
    model_artifact_id,
    model_inference_id,
)
from investment_analyst.research.predictive_models import (
    CLOSE_RETURN_ALGORITHM_VERSION,
    EXPERIMENT_SPEC_ID,
    FEATURE_RATIO_UNIT,
    PREDICTIVE_FEATURE_COLUMNS,
    TARGET_VERSION,
    FeatureState,
    aggregate_feature_state,
    decision_instant,
    predictive_feature_hash,
    predictive_sample_key,
)


class ModelInferenceServiceError(RuntimeError):
    """Base error for the isolated inference service."""


class ModelInferenceIntegrityError(ModelInferenceServiceError):
    """A formally typed input contradicts its own evidence or identity."""


class ModelInferenceExecutionError(ModelInferenceServiceError):
    """The adapter failed or returned an output outside the closed contract."""


class ModelInferenceBudgetError(ModelInferenceServiceError):
    """The deterministic result projection exceeds the bounded response budget."""


def _reason(code: InferenceReasonCode, detail: str) -> InferenceReason:
    return InferenceReason(code=code, detail=detail)


class ModelInferenceService:
    """Apply one validated test artifact to one existing pre-decision preview."""

    def __init__(self, adapter: ModelInferenceAdapter | None = None) -> None:
        self._adapter = adapter

    def infer(self, request: ModelInferenceRequest) -> ModelInferenceResult:
        """Return deterministic output or an explicit, adapter-free abstention."""
        artifact = request.artifact
        preview = request.preview
        self._verify_artifact(artifact)
        self._verify_preview_structure(preview)
        incompatibilities = self._compatibility_reasons(artifact, preview)
        if incompatibilities:
            status = InferenceStatus.INCOMPATIBLE
            reasons = incompatibilities
        elif artifact.available_at > preview.request.known_at:
            status = InferenceStatus.ARTIFACT_NOT_AVAILABLE
            reasons = (
                _reason(
                    InferenceReasonCode.ARTIFACT_NOT_YET_AVAILABLE,
                    "the test artifact was not available at the requested cut",
                ),
            )
        else:
            unavailable = tuple(
                feature
                for feature in preview.features
                if feature.state is not FeatureState.AVAILABLE
            )
            if unavailable:
                status = InferenceStatus.INPUTS_UNAVAILABLE
                reasons = self._unavailable_reasons(preview)
            else:
                status = InferenceStatus.TEST_OUTPUT
                reasons = ()

        probability: Decimal | None = None
        if status is InferenceStatus.TEST_OUTPUT:
            output = self._invoke_adapter(artifact, preview)
            probability = output.probability

        return self._build_result(
            artifact=artifact,
            preview=preview,
            status=status,
            reasons=reasons,
            probability=probability,
        )

    @staticmethod
    def _verify_artifact(artifact: ModelArtifact) -> None:
        payload = artifact.material_payload()
        if artifact.artifact_sha256 != analytical_content_hash(payload):
            raise ModelInferenceIntegrityError("artifact hash does not match its semantic payload")
        if artifact.artifact_id != model_artifact_id(payload):
            raise ModelInferenceIntegrityError("artifact id does not match its semantic payload")
        if artifact.feature_columns != PREDICTIVE_FEATURE_COLUMNS:
            raise ModelInferenceIntegrityError(
                "artifact feature columns are not the preregistered set"
            )

    @staticmethod
    def _verify_preview_structure(preview: ResearchFeaturePreview) -> None:
        if preview.decision_at != decision_instant(preview.request.decision_date):
            raise ModelInferenceIntegrityError("preview decision_at contradicts its decision date")
        if preview.request.known_at > preview.decision_at:
            raise ModelInferenceIntegrityError("preview cut follows its decision instant")
        expected_status = (
            FeaturePreviewStatus.FEATURES_AVAILABLE
            if aggregate_feature_state(preview.features) is FeatureState.AVAILABLE
            else FeaturePreviewStatus.INPUTS_UNAVAILABLE
        )
        if preview.status is not expected_status:
            raise ModelInferenceIntegrityError("preview status contradicts its feature states")
        for feature in preview.features:
            if feature.state is FeatureState.AVAILABLE:
                if feature.value is None or feature.as_of is None or feature.available_at is None:
                    raise ModelInferenceIntegrityError(
                        "available preview feature lacks value, period or availability"
                    )
                if not feature.value.is_finite():
                    raise ModelInferenceIntegrityError("preview feature value must be finite")
                if feature.reasons:
                    raise ModelInferenceIntegrityError(
                        "available preview feature carries failure reasons"
                    )
            else:
                if feature.value is not None:
                    raise ModelInferenceIntegrityError(
                        "unavailable preview feature exposes a numeric value"
                    )
                if not feature.reasons:
                    raise ModelInferenceIntegrityError(
                        "unavailable preview feature lacks a finite reason"
                    )

    @staticmethod
    def _compatibility_reasons(
        artifact: ModelArtifact,
        preview: ResearchFeaturePreview,
    ) -> tuple[InferenceReason, ...]:
        reasons: list[InferenceReason] = []

        def add(code: InferenceReasonCode, detail: str) -> None:
            reasons.append(_reason(code, detail))

        if preview.request.asset_id != preview.asset.asset_id:
            add(
                InferenceReasonCode.ASSET_IDENTITY_MISMATCH,
                "preview request asset differs from its resolved asset identity",
            )
        if (
            preview.asset.asset_class is not AssetClass.CRYPTO
            or preview.asset.asset_class is not artifact.asset_class
        ):
            add(
                InferenceReasonCode.ASSET_PROFILE_INCOMPATIBLE,
                "resolved asset class "
                f"{preview.asset.asset_class.value} is outside the artifact profile",
            )
        if preview.asset.quote_currency != artifact.quote_currency:
            add(
                InferenceReasonCode.ASSET_PROFILE_INCOMPATIBLE,
                f"resolved quote currency {preview.asset.quote_currency} differs from the artifact",
            )
        if preview.experiment_spec_id != artifact.experiment_spec_id:
            add(
                InferenceReasonCode.EXPERIMENT_MISMATCH,
                "preview experiment differs from the artifact experiment",
            )
        if preview.experiment_spec_id != EXPERIMENT_SPEC_ID:
            add(
                InferenceReasonCode.EXPERIMENT_MISMATCH,
                "preview experiment is not the preregistered experiment",
            )
        if artifact.target_version != TARGET_VERSION:
            add(
                InferenceReasonCode.TARGET_VERSION_MISMATCH,
                "artifact target version is not the preregistered target",
            )
        if preview.feature_set_version != artifact.feature_set_version:
            add(
                InferenceReasonCode.FEATURE_SET_VERSION_MISMATCH,
                "preview feature set differs from the artifact feature set",
            )
        if artifact.feature_columns != PREDICTIVE_FEATURE_COLUMNS:
            add(
                InferenceReasonCode.FEATURE_COLUMNS_MISMATCH,
                "artifact feature columns are not the preregistered ordered set",
            )
        expected_columns = tuple(item.column_name for item in PREDICTIVE_FEATURE_COLUMNS)
        if tuple(feature.column_name for feature in preview.features) != expected_columns:
            add(
                InferenceReasonCode.FEATURE_COLUMNS_MISMATCH,
                "preview feature columns are missing, reordered or foreign",
            )
        else:
            for specification, feature in zip(
                PREDICTIVE_FEATURE_COLUMNS,
                preview.features,
                strict=True,
            ):
                expected_algorithm = (
                    CLOSE_RETURN_ALGORITHM_VERSION
                    if specification.transform == "close_ratio_1d"
                    else specification.algorithm_version
                )
                if feature.algorithm_version != expected_algorithm:
                    add(
                        InferenceReasonCode.FEATURE_CONTRACT_MISMATCH,
                        f"{feature.column_name} uses another algorithm version",
                    )
                if feature.unit != FEATURE_RATIO_UNIT:
                    add(
                        InferenceReasonCode.FEATURE_CONTRACT_MISMATCH,
                        f"{feature.column_name} uses unit {feature.unit} instead of ratio",
                    )
        for feature in preview.features:
            if feature.available_at is not None and feature.available_at > preview.request.known_at:
                add(
                    InferenceReasonCode.FEATURE_EVIDENCE_AFTER_CUT,
                    f"{feature.column_name} was available after the requested cut",
                )
        for reference in preview.close_references:
            if reference.available_at > preview.request.known_at:
                add(
                    InferenceReasonCode.FEATURE_EVIDENCE_AFTER_CUT,
                    "a selected close reference was available after the requested cut",
                )
        expected_sample_key = predictive_sample_key(
            experiment_spec_id=preview.experiment_spec_id,
            asset_id=preview.request.asset_id,
            source_id=preview.asset.source_id,
            decision_at=preview.decision_at,
        )
        if preview.sample_key != expected_sample_key:
            add(
                InferenceReasonCode.PREVIEW_IDENTITY_MISMATCH,
                "preview sample_key does not match its scope",
            )
        expected_feature_hash = predictive_feature_hash(
            sample_key=preview.sample_key,
            asset_id=preview.request.asset_id,
            source_id=preview.asset.source_id,
            decision_at=preview.decision_at,
            features=preview.features,
        )
        if preview.feature_hash != expected_feature_hash:
            add(
                InferenceReasonCode.PREVIEW_IDENTITY_MISMATCH,
                "preview feature_hash does not match its ordered evidence",
            )
        expected_preview_id = feature_preview_id(
            workspace_uuid=preview.workspace_uuid,
            asset=preview.asset,
            request=preview.request,
            decision_at=preview.decision_at,
            material_payload=preview.material_payload(),
        )
        if preview.preview_id != expected_preview_id:
            add(
                InferenceReasonCode.PREVIEW_IDENTITY_MISMATCH,
                "preview_id does not match its material projection",
            )
        return tuple(reasons)

    @staticmethod
    def _unavailable_reasons(preview: ResearchFeaturePreview) -> tuple[InferenceReason, ...]:
        reasons: list[InferenceReason] = []
        for feature in preview.features:
            if feature.state is FeatureState.AVAILABLE:
                continue
            details = "; ".join(f"{item.code.value}: {item.detail}" for item in feature.reasons)
            detail = f"{feature.column_name} is {feature.state.value}"
            if details:
                detail = f"{detail}: {details}"
            reasons.append(_reason(InferenceReasonCode.FEATURE_INPUT_UNAVAILABLE, detail))
        if not reasons:
            raise ModelInferenceIntegrityError("inputs_unavailable requires unavailable features")
        return tuple(reasons)

    def _invoke_adapter(
        self,
        artifact: ModelArtifact,
        preview: ResearchFeaturePreview,
    ) -> ModelAdapterOutput:
        if self._adapter is None:
            try:
                adapter = resolve_model_inference_adapter(artifact)
            except ModelInferenceAdapterError as error:
                raise ModelInferenceExecutionError(str(error)) from error
        else:
            adapter = self._adapter
            if (
                adapter.adapter_id != artifact.adapter_id
                or adapter.adapter_version != artifact.adapter_version
            ):
                raise ModelInferenceExecutionError(
                    "injected adapter identity differs from the artifact"
                )
        values = tuple(feature.value for feature in preview.features)
        if len(values) != 3 or any(value is None for value in values):
            raise ModelInferenceIntegrityError("test output requires three available values")
        inference_input = ModelInferenceInput(
            feature_values=values,
            context=ModelInferenceContext(
                artifact_id=artifact.artifact_id,
                artifact_sha256=artifact.artifact_sha256,
                preview_id=preview.preview_id,
                workspace_uuid=preview.workspace_uuid,
                asset=preview.asset,
                experiment_spec_id=preview.experiment_spec_id,
                target_version=artifact.target_version,
                feature_set_version=preview.feature_set_version,
                decision_at=preview.decision_at,
                known_at=preview.request.known_at,
                feature_columns=artifact.feature_columns,
            ),
        )
        try:
            output = adapter.predict(inference_input)
        except Exception as error:
            raise ModelInferenceExecutionError("the model adapter failed") from error
        if not isinstance(output, ModelAdapterOutput):
            raise ModelInferenceExecutionError("the model adapter returned an invalid output")
        if (
            output.adapter_id != artifact.adapter_id
            or output.adapter_version != artifact.adapter_version
        ):
            raise ModelInferenceExecutionError("adapter output identity differs from the artifact")
        probability = output.probability
        if (
            not isinstance(probability, Decimal)
            or not probability.is_finite()
            or not Decimal("0") <= probability <= Decimal("1")
            or probability != CONSTANT_TEST_PROBABILITY
        ):
            raise ModelInferenceExecutionError(
                "adapter output is outside the authorized constant test contract"
            )
        return output

    @staticmethod
    def _output_interpretation(status: InferenceStatus) -> str:
        if status is InferenceStatus.TEST_OUTPUT:
            return (
                "Probabilidad constante de prueba 0.5; el doble no atribuye importancia "
                "a las features ni estima el mercado."
            )
        if status is InferenceStatus.INPUTS_UNAVAILABLE:
            return (
                "Sin probabilidad: faltan inputs disponibles al corte y el adaptador "
                "no fue invocado."
            )
        if status is InferenceStatus.ARTIFACT_NOT_AVAILABLE:
            return (
                "Sin probabilidad: el artefacto no estaba disponible al corte y el "
                "adaptador no fue invocado."
            )
        return (
            "Sin probabilidad: la solicitud, el contrato o la evidencia no son "
            "compatibles y el adaptador no fue invocado."
        )

    def _build_result(
        self,
        *,
        artifact: ModelArtifact,
        preview: ResearchFeaturePreview,
        status: InferenceStatus,
        reasons: tuple[InferenceReason, ...],
        probability: Decimal | None,
    ) -> ModelInferenceResult:
        known_at = preview.request.known_at
        explanation = ModelExplanationPacket(
            inference_id=preview.preview_id,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.artifact_sha256,
            model_id=artifact.model_id,
            model_version=artifact.model_version,
            adapter_id=artifact.adapter_id,
            adapter_version=artifact.adapter_version,
            preview_id=preview.preview_id,
            workspace_uuid=preview.workspace_uuid,
            asset=preview.asset,
            source_id=preview.asset.source_id,
            experiment_spec_id=preview.experiment_spec_id,
            target_version=artifact.target_version,
            feature_set_version=preview.feature_set_version,
            decision_at=preview.decision_at,
            known_at=known_at,
            status=status,
            features=preview.features,
            close_references=preview.close_references,
            output_interpretation=self._output_interpretation(status),
            limitations=MODEL_TEST_LIMITATIONS,
        )
        fields: dict[str, object] = {
            "artifact_id": artifact.artifact_id,
            "artifact_sha256": artifact.artifact_sha256,
            "model_id": artifact.model_id,
            "model_version": artifact.model_version,
            "adapter_id": artifact.adapter_id,
            "adapter_version": artifact.adapter_version,
            "purpose": artifact.purpose,
            "validation_status": artifact.validation_status,
            "workspace_uuid": preview.workspace_uuid,
            "asset": preview.asset,
            "source_id": preview.asset.source_id,
            "experiment_spec_id": preview.experiment_spec_id,
            "target_version": artifact.target_version,
            "feature_set_version": preview.feature_set_version,
            "preview_id": preview.preview_id,
            "sample_key": preview.sample_key,
            "feature_hash": preview.feature_hash,
            "decision_at": preview.decision_at,
            "known_at": known_at,
            "status": status,
            "reasons": reasons,
            "test_probability": probability,
        }
        draft = ModelInferenceResult.model_construct(
            inference_id=preview.preview_id,
            explanation=explanation,
            **fields,
        )
        inference_id = model_inference_id(
            artifact_sha256=artifact.artifact_sha256,
            adapter_version=artifact.adapter_version,
            preview_id=preview.preview_id,
            material_payload=draft.material_payload(),
        )
        explanation = explanation.model_copy(update={"inference_id": inference_id})
        result = ModelInferenceResult.model_validate(
            {
                **fields,
                "inference_id": inference_id,
                "explanation": explanation,
            }
        )
        encoded = json.dumps(
            result.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_MODEL_INFERENCE_BYTES:
            raise ModelInferenceBudgetError(
                f"model inference projection exceeds {MAX_MODEL_INFERENCE_BYTES} bytes"
            )
        return result


__all__ = [
    "ModelInferenceBudgetError",
    "ModelInferenceExecutionError",
    "ModelInferenceIntegrityError",
    "ModelInferenceService",
    "ModelInferenceServiceError",
]
