#!/usr/bin/env python3
"""Apply one bounded test artifact to one pre-decision feature preview."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
    StorageLocationRequest,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.research.feature_preview_models import (
    FeaturePreviewError,
    FeaturePreviewRequest,
    ResearchFeaturePreview,
)
from investment_analyst.research.inference_models import (
    MAX_MODEL_INFERENCE_BYTES,
    ModelArtifactError,
    ModelInferenceRequest,
    ModelInferenceResult,
    load_model_artifact,
)
from investment_analyst.research.inference_service import (
    ModelInferenceService,
    ModelInferenceServiceError,
)
from investment_analyst.research.predictive_dataset import (
    PredictiveDatasetBuilder,
    PredictiveDatasetError,
)
from investment_analyst.storage.errors import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

_EXIT_INVALID = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply the isolated constant test artifact to the three preregistered"
            " inputs available at one explicit cut. Read-only, without training,"
            " acquisition, persistence or promotion."
        )
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model-artifact", type=Path, required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--decision-date", type=_date_value, required=True)
    parser.add_argument("--known-at", type=_known_at, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _date_value(value: str) -> date:
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise argparse.ArgumentTypeError("--decision-date must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--decision-date must use YYYY-MM-DD") from error


def _known_at(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--known-at must include timezone information")
    return parsed.astimezone(UTC)


def _print_human(result: ModelInferenceResult) -> None:
    print(f"status={result.status.value}")
    print(f"inference_id={result.inference_id}")
    print(
        f"asset={result.asset.asset_id} source={result.source_id} "
        f"decision_at={result.decision_at.isoformat()} "
        f"known_at={result.known_at.isoformat()}"
    )
    print(f"artifact_id={result.artifact_id} preview_id={result.preview_id}")
    if result.test_probability is not None:
        print(f"test_probability={result.test_probability}")
    for reason in result.reasons:
        print(f"reason={reason.code.value}: {reason.detail}")
    print(f"interpretation={result.explanation.output_interpretation}")
    for limitation in result.explanation.limitations:
        print(f"limitation={limitation}")


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        artifact = load_model_artifact(arguments.model_artifact)
        preview_request = FeaturePreviewRequest(
            asset_id=arguments.asset_id,
            decision_date=arguments.decision_date,
            known_at=arguments.known_at,
        )
        runtime = ApplicationRuntime.create_default()
        location = StorageLocationRequest(workspace=arguments.workspace)
        with runtime.open_storage(
            location,
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            preview: ResearchFeaturePreview = PredictiveDatasetBuilder(
                storage,
                provider_resolver=runtime.provider_resolver,
                catalog=runtime.catalog,
            ).preview_features(preview_request)
        result = ModelInferenceService().infer(
            ModelInferenceRequest(artifact=artifact, preview=preview)
        )
        if arguments.as_json:
            encoded = json.dumps(
                result.to_json_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(encoded) > MAX_MODEL_INFERENCE_BYTES:
                raise ModelInferenceServiceError(
                    f"model inference projection exceeds {MAX_MODEL_INFERENCE_BYTES} bytes"
                )
            print(encoded.decode("utf-8"))
        else:
            _print_human(result)
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        ProviderAssetContextError,
        FeaturePreviewError,
        ModelArtifactError,
        ModelInferenceServiceError,
        PredictiveDatasetError,
        StorageError,
        WorkspaceError,
        ValueError,
        OSError,
    ) as error:
        print(
            json.dumps(
                {"error": str(error)},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return _EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
