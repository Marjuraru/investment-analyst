"""One explicit, bounded shadow observation over the existing preview and inference contracts.

The service opens the workspace read-only once for one or two previews, closes it before
inference, and publishes a complete bundle atomically. It never trains, acquires, promotes or
writes to the workspace or to the outbox.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.catalog.provider_configuration import resolve_coinbase_configuration
from investment_analyst.catalog.provider_context import ProviderAssetContextError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research.feature_preview_models import (
    FeaturePreviewRequest,
    ResearchFeaturePreview,
)
from investment_analyst.research.inference_models import (
    ModelArtifact,
    ModelInferenceRequest,
    ModelInferenceResult,
    load_model_artifact,
)
from investment_analyst.research.inference_service import ModelInferenceService
from investment_analyst.research.predictive_dataset import PredictiveDatasetBuilder
from investment_analyst.research.predictive_models import decision_instant
from investment_analyst.research.shadow_models import (
    MAX_SHADOW_RESULT_BYTES,
    SHADOW_LIMITATIONS,
    ShadowAssetManifest,
    ShadowRunManifest,
    ShadowRunRequest,
    ShadowRunSummary,
    shadow_run_id,
)
from investment_analyst.research.shadow_store import ShadowStore
from investment_analyst.workspace.models import WorkspaceAccessMode


class ModelShadowServiceError(RuntimeError):
    """Base error for one bounded shadow run."""


class ModelShadowRequestError(ModelShadowServiceError):
    """The request, artifact or asset profile is outside the admitted shadow scope."""


class ModelShadowIntegrityError(ModelShadowServiceError):
    """A preview, inference or manifest contradicts its own identity."""


class ModelShadowService:
    """Run one explicit shadow observation and publish its complete bundle."""

    def __init__(
        self,
        *,
        runtime: ApplicationRuntime | None = None,
        inference_service: ModelInferenceService | None = None,
        protected_roots: tuple[Path, ...] = (),
    ) -> None:
        self._runtime = runtime
        self._inference = inference_service or ModelInferenceService()
        self._protected_roots = protected_roots

    def run(self, request: ShadowRunRequest) -> ShadowRunSummary:
        runtime = self._runtime or ApplicationRuntime.create_default()
        artifact = load_model_artifact(request.model_artifact)
        asset_ids = tuple(sorted(request.asset_ids))
        self._validate_profiles(runtime, asset_ids)
        store = ShadowStore(
            request.output_root,
            workspace_root=request.workspace,
            protected_roots=self._protected_roots or (Path(__file__).resolve().parents[3],),
        )
        previews, workspace_uuid = self._previews(runtime, request, asset_ids)
        results = self._infer(artifact, previews, workspace_uuid)
        manifest = self._manifest(
            request=request,
            artifact=artifact,
            workspace_uuid=workspace_uuid,
            results=results,
        )
        receipt = store.publish(manifest, {result.inference_id: data for result, data in results})
        return ShadowRunSummary(
            status="created" if receipt.created else "reused",
            run_id=manifest.run_id,
            manifest_path=str(receipt.manifest_path),
            manifest_sha256=receipt.manifest_sha256,
            assets=manifest.assets,
        )

    @staticmethod
    def _validate_profiles(runtime: ApplicationRuntime, asset_ids: tuple[str, ...]) -> None:
        for asset_id in asset_ids:
            try:
                asset = runtime.catalog.get(asset_id)
            except AssetCatalogError as error:
                raise ModelShadowRequestError(f"asset is not catalogued: {asset_id}") from error
            if (
                asset.asset_class is not AssetClass.CRYPTO
                or asset.quote_currency != "USD"
                or asset.crypto_profile is None
            ):
                raise ModelShadowRequestError(
                    f"asset {asset_id} is outside the crypto spot USD profile"
                )
            try:
                configuration = resolve_coinbase_configuration(
                    runtime.provider_resolver,
                    asset_id=asset_id,
                )
            except (ProviderAssetContextError, ValueError) as error:
                raise ModelShadowRequestError(
                    f"asset {asset_id} has no admitted Coinbase daily configuration"
                ) from error
            if (
                configuration.asset_class is not AssetClass.CRYPTO
                or configuration.quote_unit != "USD"
            ):
                raise ModelShadowRequestError(f"asset {asset_id} resolved outside crypto spot USD")

    @staticmethod
    def _previews(
        runtime: ApplicationRuntime,
        request: ShadowRunRequest,
        asset_ids: tuple[str, ...],
    ) -> tuple[tuple[ResearchFeaturePreview, ...], UUID]:
        location = StorageLocationRequest(workspace=request.workspace)
        with runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            workspace_uuid = storage.paths.workspace_id
            if not isinstance(workspace_uuid, UUID):
                raise ModelShadowIntegrityError("shadow requires one identified workspace")
            builder = PredictiveDatasetBuilder(
                storage,
                provider_resolver=runtime.provider_resolver,
                catalog=runtime.catalog,
            )
            previews = tuple(
                builder.preview_features(
                    FeaturePreviewRequest(
                        asset_id=asset_id,
                        decision_date=request.decision_date,
                        known_at=request.known_at,
                    )
                )
                for asset_id in asset_ids
            )
        for asset_id, preview in zip(asset_ids, previews, strict=True):
            if (
                preview.workspace_uuid != workspace_uuid
                or preview.request.asset_id != asset_id
                or preview.request.decision_date != request.decision_date
                or preview.request.known_at != request.known_at
            ):
                raise ModelShadowIntegrityError(
                    "shadow preview contradicts the opened workspace or request"
                )
        return previews, workspace_uuid

    def _infer(
        self,
        artifact: ModelArtifact,
        previews: tuple[ResearchFeaturePreview, ...],
        workspace_uuid: UUID,
    ) -> tuple[tuple[ModelInferenceResult, bytes], ...]:
        results: list[tuple[ModelInferenceResult, bytes]] = []
        for preview in previews:
            result = self._inference.infer(
                ModelInferenceRequest(artifact=artifact, preview=preview)
            )
            if (
                result.workspace_uuid != workspace_uuid
                or result.preview_id != preview.preview_id
                or result.asset.asset_id != preview.request.asset_id
            ):
                raise ModelShadowIntegrityError(
                    "shadow inference contradicts its preview or workspace"
                )
            encoded = canonical_analytical_json(result.to_json_dict())
            if len(encoded) > MAX_SHADOW_RESULT_BYTES:
                raise ModelShadowIntegrityError(
                    f"shadow result exceeds {MAX_SHADOW_RESULT_BYTES} bytes"
                )
            results.append((result, encoded))
        return tuple(results)

    @staticmethod
    def _manifest(
        *,
        request: ShadowRunRequest,
        artifact: ModelArtifact,
        workspace_uuid: UUID,
        results: tuple[tuple[ModelInferenceResult, bytes], ...],
    ) -> ShadowRunManifest:
        ordered = sorted(results, key=lambda item: item[0].asset.asset_id)
        assets = tuple(
            ShadowAssetManifest(
                asset_id=result.asset.asset_id,
                source_id=result.source_id,
                inference_id=result.inference_id,
                inference_sha256=hashlib.sha256(encoded).hexdigest(),
                result_file=f"{result.inference_id}.json",
                status=result.status,
                test_probability=result.test_probability,
            )
            for result, encoded in ordered
        )
        draft = ShadowRunManifest.model_construct(
            run_id=UUID(int=0),
            workspace_uuid=workspace_uuid,
            artifact_id=artifact.artifact_id,
            artifact_sha256=artifact.artifact_sha256,
            decision_date=request.decision_date,
            known_at=request.known_at,
            decision_at=decision_instant(request.decision_date),
            assets=assets,
            limitations=SHADOW_LIMITATIONS,
        )
        run_id = shadow_run_id(draft.material_payload())
        return ShadowRunManifest.model_validate(
            {**draft.model_dump(mode="python"), "run_id": run_id}
        )


__all__ = [
    "ModelShadowIntegrityError",
    "ModelShadowRequestError",
    "ModelShadowService",
    "ModelShadowServiceError",
]
