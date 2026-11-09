#!/usr/bin/env python3
"""Preview the preregistered inputs available before one decision, without labels."""

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
            "Project the three preregistered predictive inputs available at one"
            " explicit cut before the 12:00 UTC decision. Read-only, without"
            " labels, training, acquisition or persistence."
        )
    )
    parser.add_argument("--workspace", type=Path, required=True)
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


def _print_human(preview: ResearchFeaturePreview) -> None:
    print(f"preview_id={preview.preview_id}")
    print(f"status={preview.status.value}")
    print(
        f"asset={preview.asset.asset_id} source={preview.asset.source_id} "
        f"decision_at={preview.decision_at.isoformat()} "
        f"known_at={preview.request.known_at.isoformat()}"
    )
    print(f"sample_key={preview.sample_key} feature_hash={preview.feature_hash}")
    for feature in preview.features:
        value = str(feature.value) if feature.value is not None else "-"
        reasons = "; ".join(f"{item.code.value}: {item.detail}" for item in feature.reasons)
        print(
            f"{feature.column_name}: state={feature.state.value} value={value} "
            f"as_of={feature.as_of.isoformat() if feature.as_of else '-'} "
            f"available_at={feature.available_at.isoformat() if feature.available_at else '-'}"
            + (f" reasons={reasons}" if reasons else "")
        )
    for reference in preview.close_references:
        print(
            f"close_reference={reference.observation_id} "
            f"period={reference.period_start.isoformat()} "
            f"available_at={reference.available_at.isoformat()}"
        )


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        request = FeaturePreviewRequest(
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
            preview = PredictiveDatasetBuilder(
                storage,
                provider_resolver=runtime.provider_resolver,
                catalog=runtime.catalog,
            ).preview_features(request)
        if arguments.as_json:
            print(
                json.dumps(
                    preview.to_json_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            _print_human(preview)
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        ProviderAssetContextError,
        FeaturePreviewError,
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
