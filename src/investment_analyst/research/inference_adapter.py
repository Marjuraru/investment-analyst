"""Closed adapter interface and the single admitted constant test engine."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from investment_analyst.research.inference_models import (
    CONSTANT_TEST_ADAPTER_ID,
    CONSTANT_TEST_ADAPTER_VERSION,
    CONSTANT_TEST_MODEL_ID,
    CONSTANT_TEST_PROBABILITY,
    ModelAdapterOutput,
    ModelArtifact,
    ModelInferenceInput,
)


class ModelInferenceAdapterError(RuntimeError):
    """The requested artifact names an adapter outside the closed allowlist."""


@runtime_checkable
class ModelInferenceAdapter(Protocol):
    """Provider- and storage-independent inference engine."""

    adapter_id: str
    adapter_version: str

    def predict(self, inference_input: ModelInferenceInput) -> ModelAdapterOutput:
        """Return one closed adapter output for the ordered feature values."""
        ...


class ConstantTestAdapter:
    """Synthetic constant double used only to verify the integration boundary."""

    adapter_id = CONSTANT_TEST_ADAPTER_ID
    adapter_version = CONSTANT_TEST_ADAPTER_VERSION

    def predict(self, inference_input: ModelInferenceInput) -> ModelAdapterOutput:
        """Return the preregistered constant without reading the feature values."""
        if len(inference_input.feature_values) != 3:
            raise ModelInferenceAdapterError("constant adapter requires three feature values")
        return ModelAdapterOutput(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            output_kind="binary_probability",
            probability=CONSTANT_TEST_PROBABILITY,
        )


def resolve_model_inference_adapter(artifact: ModelArtifact) -> ModelInferenceAdapter:
    """Select the only production adapter through an exact internal allowlist."""
    if (
        artifact.model_id != CONSTANT_TEST_MODEL_ID
        or artifact.adapter_id != CONSTANT_TEST_ADAPTER_ID
        or artifact.adapter_version != CONSTANT_TEST_ADAPTER_VERSION
        or artifact.parameters.family != CONSTANT_TEST_MODEL_ID
    ):
        raise ModelInferenceAdapterError("artifact names an adapter outside the closed allowlist")
    return ConstantTestAdapter()


__all__ = [
    "ConstantTestAdapter",
    "ModelInferenceAdapter",
    "ModelInferenceAdapterError",
    "resolve_model_inference_adapter",
]
